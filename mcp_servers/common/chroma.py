"""ChromaDB 연결과 컬렉션 이름 (적재기·rag-mcp 공용).

이 프로젝트는 chromadb 클라이언트의 안정적인 메서드만 쓴다:
HttpClient, heartbeat, get_or_create_collection, get_collection, delete_collection,
Collection.upsert / get / query / count. 임베딩은 클라이언트에서 계산해 넘긴다(embedding_function=None).
"""
from __future__ import annotations

import os
import time

COLLECTIONS = ("references", "policies", "rubrics", "coaching_examples")


def collection_name(kind: str) -> str:
    return os.environ.get("CHROMA_COLLECTION_PREFIX", "axeval_") + kind


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
