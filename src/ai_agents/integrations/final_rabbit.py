"""사전 선언된 최종 평가 큐 소비. 브로커의 publish confirm 이후에만 원본 ack."""

import asyncio
import json

from ..orchestration.final_worker import DLQ, REQUEST_QUEUE, RESULT_QUEUE, RETRY_QUEUE


async def consume_final_evaluations(url, worker):
    from aio_pika import DeliveryMode, Message, connect_robust
    from pamqp.commands import Basic

    connection = await connect_robust(url, timeout=15)
    async with connection:
        channel = await connection.channel(publisher_confirms=True, on_return_raises=True)
        await channel.set_qos(prefetch_count=1)
        queue = await channel.get_queue(REQUEST_QUEUE, ensure=True)
        for name in (RETRY_QUEUE, DLQ, RESULT_QUEUE):
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
                except Exception:
                    if not message.processed:
                        await message.nack(requeue=True)
                    await asyncio.sleep(5)
