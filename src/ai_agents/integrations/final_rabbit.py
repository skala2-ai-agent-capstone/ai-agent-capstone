"""사전 선언된 최종 평가 큐 소비. 브로커의 publish confirm 이후에만 원본 ack."""

import asyncio
import json

from ..orchestration.final_worker import LifecycleEventRequiresBackend
from ..shared.final_job_contracts import FINAL_QUEUE_CONTRACTS


async def consume_final_evaluations(url, worker):
    from aio_pika import DeliveryMode, Message, connect_robust
    from pamqp.commands import Basic

    queues = getattr(worker, "queues", FINAL_QUEUE_CONTRACTS["agent-v1"])
    connection = await connect_robust(url, timeout=15)
    async with connection:
        channel = await connection.channel(publisher_confirms=True, on_return_raises=True)
        await channel.set_qos(prefetch_count=1)
        queue = await channel.get_queue(queues.requests, ensure=True)
        for name in (queues.retry, queues.dlq, queues.results):
            await channel.get_queue(name, ensure=True)

        async def publish(destination, payload, code):
            confirmation = await channel.default_exchange.publish(
                Message(
                    json.dumps(payload, ensure_ascii=False).encode(),
                    content_type="application/json", content_encoding="utf-8",
                    delivery_mode=DeliveryMode.PERSISTENT, message_id=payload["message_id"],
                    headers={"error_code": code},
                ), routing_key=destination, mandatory=True, timeout=15,
            )
            if not isinstance(confirmation, Basic.Ack):
                raise RuntimeError("publish was not confirmed")

        async with queue.iterator(no_ack=False) as incoming:
            async for message in incoming:
                try:
                    await worker.handle(message.body, publish=publish, ack=message.ack)
                except LifecycleEventRequiresBackend:
                    # 업무 실패로 바꾸거나 재평가하지 않는다. 연결 종료 후 원본은 큐에 남는다.
                    if not message.processed:
                        await message.nack(requeue=True)
                    raise
                except Exception:
                    if not message.processed:
                        await message.nack(requeue=True)
                    await asyncio.sleep(5)
