"""저장 도구 공통 멱등 처리 (7장 기본안 3).

모든 쓰기는 idempotency_key가 필수이고, 같은 도구·키로 다시 부르면 처음 결과를 돌려준다(meta.duplicate).
키 확인(replay)과 기록(remember)은 저장과 같은 트랜잭션에서 부르므로 같은 키가 동시에 와도 한 번만 저장된다.
"""
from __future__ import annotations

from psycopg.types.json import Jsonb

from common.runtime import Result, validation


def need_key(key) -> None:
    if not key or not isinstance(key, str):
        raise validation("idempotency_key가 필요합니다")


def replay(c, tool: str, key: str) -> Result | None:
    """같은 키로 저장한 적이 있으면 그때 결과를 duplicate로 돌려준다."""
    r = c.execute("SELECT result FROM idempotency_keys WHERE tool=%s AND key=%s", (tool, key)).fetchone()
    return Result(r["result"], duplicate=True, code="DUPLICATE_REQUEST") if r else None


def remember(c, tool: str, key: str, result: dict, evaluation_id: str | None = None) -> None:
    c.execute("INSERT INTO idempotency_keys (tool, key, evaluation_id, result) VALUES (%s,%s,%s,%s)",
              (tool, key, evaluation_id, Jsonb(result)))
