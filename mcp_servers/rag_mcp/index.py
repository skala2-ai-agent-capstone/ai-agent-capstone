"""키워드 점수 (명세 7장 기본안 6: 한글 키워드 매칭 BM25 계열). rag-mcp 하이브리드 검색의 키워드 쪽.

외부 라이브러리 없이 BM25를 구현한다. 토큰은 어절 + 한글 어절의 글자 2-gram이라
조사가 붙은 어절('승인은', '승인이')도 같은 어근으로 걸린다.
"""
from __future__ import annotations

import math
import re
from collections import Counter

WORD = re.compile(r"[0-9A-Za-z가-힣§.%\-]+")
HANGUL = re.compile(r"[가-힣]")


def tokenize(text: str) -> list[str]:
    toks: list[str] = []
    for w in WORD.findall((text or "").lower()):
        w = w.strip(".-")
        if not w:
            continue
        toks.append(w)
        if HANGUL.search(w) and len(w) > 2:
            toks.extend(w[i : i + 2] for i in range(len(w) - 1))
    return toks


class BM25:
    def __init__(self, docs: list[str], k1: float = 1.2, b: float = 0.75):
        self.k1, self.b = k1, b
        self.tf = [Counter(tokenize(d)) for d in docs]
        self.len = [sum(t.values()) for t in self.tf]
        self.avg = (sum(self.len) / len(self.len)) if self.len else 0.0
        df: Counter = Counter()
        for t in self.tf:
            df.update(t.keys())
        n = len(docs)
        self.idf = {w: math.log(1 + (n - c + 0.5) / (c + 0.5)) for w, c in df.items()}

    def scores(self, query: str) -> list[float]:
        q = set(tokenize(query))
        out = []
        for tf, ln in zip(self.tf, self.len):
            s = 0.0
            for w in q:
                f = tf.get(w)
                if not f:
                    continue
                s += self.idf.get(w, 0.0) * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * ln / (self.avg or 1)))
            out.append(s)
        return out
