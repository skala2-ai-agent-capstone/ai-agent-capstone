"""독립적인 읽기만 제한 병렬 실행한다. 결과는 입력 순서를 유지한다."""

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from typing import TypeVar, cast

T = TypeVar("T")
R = TypeVar("R")
MAX_PARALLEL_READS = 4


async def read_in_parallel(read: Callable[[T], Awaitable[R]], items: Iterable[T]) -> list[R]:
    items = list(items)
    results: list[R | None] = [None] * len(items)
    pending = iter(enumerate(items))

    async def worker():
        for index, item in pending:
            results[index] = await read(item)

    tasks = [asyncio.create_task(worker()) for _ in range(min(MAX_PARALLEL_READS, len(items)))]
    try:
        await asyncio.gather(*tasks)
    except BaseException:
        # 오류·상위 취소 모두 남은 조회를 정리하고 원래 예외 형식을 유지한다.
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    return cast(list[R], results)
