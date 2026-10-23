"""ChromaDB 연결과 컬렉션 이름 (적재기·rag-mcp 공용).

이 프로젝트는 chromadb 클라이언트의 안정적인 메서드만 쓴다:
HttpClient, heartbeat, get_or_create_collection, get_collection, delete_collection,
Collection.upsert / get / query / count / delete(평가 기준 초안 폐기). 임베딩은 클라이언트에서 계산해 넘긴다(embedding_function=None).
"""
from __future__ import annotations

import json
import os
import time

COLLECTIONS = ("references", "policies", "rubrics", "coaching_examples")
# rubrics 컬렉션: 기준 항목 하나가 문서 하나이고 이 칸들을 metadata로 둔다 (적재기와 business-mcp save_rubric 공용)
RUBRIC_ITEM_FIELDS = ("item_id", "item", "seq", "criterion", "score_2", "score_1", "score_0", "unobserved", "needs_review",
                      "evidence_sources", "related_clauses")


def collection_name(kind: str) -> str:
    return os.environ.get("CHROMA_COLLECTION_PREFIX", "axeval_") + kind


def clean_metadata(m: dict) -> dict:
    """Chroma metadata는 str·int·float·bool만 된다. None은 빼고 목록·객체는 JSON 문자열로 바꾼다."""
    out = {}
    for k, v in m.items():
        if v is None:
            continue
        if isinstance(v, (list, dict)):
            v = json.dumps(v, ensure_ascii=False)
        elif not isinstance(v, (str, int, float, bool)):
            v = str(v)
        out[k] = v
    return out


def connect(wait_sec: int = 60):
    fake = os.environ.get("CHROMA_FAKE_PATH")
    if fake:  # 로컬 개발·CI 전용 대역 (tests/fake_chroma.py)
        from tests.fake_chroma import FakeHttpClient

        return FakeHttpClient(fake)
    import chromadb
    from chromadb.config import Settings

    host = os.environ.get("CHROMA_HOST", "chroma")
    port = int(os.environ.get("CHROMA_PORT", "8000"))
    headers = {}
    if os.environ.get("CHROMA_AUTH_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['CHROMA_AUTH_TOKEN']}"
    deadline = time.time() + wait_sec
    while True:
        try:
            client = chromadb.HttpClient(host=host, port=port, headers=headers or None,
                                         settings=Settings(anonymized_telemetry=False))
            client.heartbeat()
            return client
        except Exception:
            if time.time() > deadline:
                raise
            time.sleep(2)
