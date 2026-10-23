"""기록 저장소. 저장 실패 처리는 호출하는 쪽(app.save_quietly)이 맡는다."""

from typing import Any, Protocol

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

COLUMNS = (
    "member", "endpoint", "stage", "work_id", "user_id", "model", "stream", "status_code", "error",
    "latency_ms", "input_tokens", "output_tokens", "request", "response", "response_text",
)
JSON_COLUMNS = {"request", "response"}
INSERT_SQL = (
    f"INSERT INTO llm_exchanges ({', '.join(COLUMNS)}) "
    f"VALUES ({', '.join(['%s'] * len(COLUMNS))})"
)


class Recorder(Protocol):
    async def save(self, record: dict[str, Any]) -> None: ...

    async def known_refs(self, work_id: str | None, user_id: str | None) -> tuple[bool, bool]: ...


class PostgresRecorder:
    def __init__(self, database_url: str):
        self.pool = AsyncConnectionPool(database_url, min_size=1, max_size=4, open=False)

    async def open(self) -> None:
        await self.pool.open()

    async def close(self) -> None:
        await self.pool.close()

    async def save(self, record: dict[str, Any]) -> None:
        values = []
        for column in COLUMNS:
            value = record.get(column)
            values.append(Jsonb(value) if column in JSON_COLUMNS and value is not None else value)
        async with self.pool.connection() as conn:
            await conn.execute(INSERT_SQL, values)

    async def known_refs(self, work_id: str | None, user_id: str | None) -> tuple[bool, bool]:
        """헤더의 업무·사용자가 업무 DB에 있는지. 없는 값(None)은 확인할 것이 없으므로 True."""
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT %(w)s::text IS NULL"
                " OR EXISTS (SELECT 1 FROM work_cases WHERE work_id = %(w)s),"
                " %(u)s::text IS NULL OR EXISTS (SELECT 1 FROM users WHERE user_id = %(u)s)",
                {"w": work_id, "u": user_id},
            )
            work_ok, user_ok = await cur.fetchone()
        return bool(work_ok), bool(user_ok)
