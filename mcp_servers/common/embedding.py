"""문서·질의 임베딩 (ChromaDB에 넣고 질의할 벡터).

기본은 외부 모델 없이 동작하는 해시 임베딩이다: 어절과 한글 글자 2-gram을 해시해 고정 차원 벡터로 만든다.
모델 다운로드가 필요 없어 폐쇄망·CI에서도 같은 결과가 나오고, 키워드 겹침을 코사인 유사도로 잡는다.
의미 검색이 필요하면 이 함수만 다국어 임베딩 모델(예: multilingual-e5)로 바꾸고 적재기를 다시 돌린다.
적재기와 rag-mcp가 반드시 같은 함수를 써야 하므로 컬렉션 metadata의 embedding 값으로 일치 여부를 확인한다.
"""
from __future__ import annotations

import hashlib
import math
import re

EMBEDDING_ID = "hash-ngram-512-v1"
DIM = 512
WORD = re.compile(r"[0-9A-Za-z가-힣§.%\-]+")
HANGUL = re.compile(r"[가-힣]")


def _features(text: str):
    for w in WORD.findall((text or "").lower()):
        w = w.strip(".-")
        if not w:
            continue
        yield "w:" + w, 1.0
        if HANGUL.search(w) and len(w) > 1:
            for i in range(len(w) - 1):
                yield "b:" + w[i : i + 2], 0.6


def embed(text: str) -> list[float]:
    v = [0.0] * DIM
    for feat, wt in _features(text):
        h = int.from_bytes(hashlib.blake2b(feat.encode(), digest_size=8).digest(), "big")
        v[h % DIM] += wt if (h >> 63) & 1 else -wt
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def embed_many(texts: list[str]) -> list[list[float]]:
    return [embed(t) for t in texts]
