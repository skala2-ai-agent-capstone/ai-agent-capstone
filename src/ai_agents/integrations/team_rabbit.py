"""백엔드가 선언한 팀 평가 큐를 소비한다. 큐 설정을 암묵적으로 바꾸지 않는다."""

import asyncio
import json

from ..shared.team_job_contracts import DLQ, REQUEST_QUEUE, RETRY_QUEUE


async def consume_team_evaluations(url: str, worker):
    from aio_pika import DeliveryMode, Message, connect_robust
    from pamqp.commands import Basic

    connection = await connect_robust(url, timeout=15)
    async with connection:
        channel = await connection.channel(publisher_confirms=True, on_return_raises=True)
        await channel.set_qos(prefetch_count=1)
        queue = await channel.get_queue(REQUEST_QUEUE, ensure=True)
        for name in (RETRY_QUEUE, DLQ):
            await channel.get_queue(name, ensure=True)

        async def publish(destination, payload, error_code):
            confirmation = await channel.default_exchange.publish(
                Message(
                    json.dumps(payload, ensure_ascii=False).encode(),
                    content_type="application/json",
                    content_encoding="utf-8",
                    delivery_mode=DeliveryMode.PERSISTENT,
                    message_id=payload["message_id"],
                    headers={"error_code": error_code} if error_code else {},
                ),
                routing_key=destination,
                mandatory=True,
                timeout=15,
            )
            if not isinstance(confirmation, Basic.Ack):
                raise RuntimeError("publish was not confirmed")

        async with queue.iterator(no_ack=False) as incoming:
            async for message in incoming:
                try:
                    await worker.handle(message.body, publish=publish, ack=message.ack)
                except Exception:
                    # 재발행/추적 기록 실패 시 예외 원문을 로그에 출력하지 않는다.
                    if not message.processed:
                        await message.nack(requeue=True)
                    await asyncio.sleep(5)
