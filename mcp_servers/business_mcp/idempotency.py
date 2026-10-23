"""저장 도구 공통 멱등 처리 (7장 기본안 3).

같은 도구·범위·키로 다시 부르면 처음 결과를 돌려준다(meta.duplicate). 범위(scope)는 키가 유효한 대상이다
(최종 평가 저장은 평가 건 ID, 평가 기준 저장은 회사 ID). 호출부는 그 대상에 대한 권한을 먼저 확인한 뒤
replay를 부르므로, 다른 평가 건·회사에서 같은 키를 써도 남의 결과가 나가지 않는다.
키 확인(replay)과 기록(remember)은 저장과 같은 트랜잭션에서 부르므로 같은 키가 동시에 와도 한 번만 저장된다.
"""
from __future__ import annotations

from psycopg.types.json import Jsonb

from common.runtime import Result, validation


def need_key(key) -> None:
    if not key or not isinstance(key, str):
        raise validation("idempotency_key가 필요합니다")


def replay(c, tool: str, scope: str, key: str) -> Result | None:
    """같은 범위·키로 저장한 적이 있으면 그때 결과를 duplicate로 돌려준다."""
    r = c.execute("SELECT result FROM idempotency_keys WHERE tool=%s AND scope=%s AND key=%s", (tool, scope, key)).fetchone()
    return Result(r["result"], duplicate=True, code="DUPLICATE_REQUEST") if r else None


def remember(c, tool: str, scope: str, key: str, result: dict, evaluation_id: str | None = None) -> None:
    c.execute("INSERT INTO idempotency_keys (tool, scope, key, evaluation_id, result) VALUES (%s,%s,%s,%s,%s)",
              (tool, scope, key, evaluation_id, Jsonb(result)))
