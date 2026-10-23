"""산출물 본문 → 구간(span). 앱이 저장한 산출물은 artifact_spans 행이 없으므로 조회할 때 이 규칙으로 나눈다.

규칙은 적재 데이터의 구간과 같다 (시드 산출물 83개 버전 전부 일치, tests/verify_e2e.py가 확인):
- 빈 줄과 표 구분선(| --- |)은 구간이 아니다.
- 표 행의 첫 칸이 REQ-01·ASK-01 같은 키면 그 키, 그런 표의 머리글은 HEADER.
- 나머지 줄은 원래 줄 번호로 Lnn.
"""
from __future__ import annotations

import re

SEP_RE = re.compile(r"^\s*\|?\s*:?-{3,}")
ROW_KEY_RE = re.compile(r"^\s*\|\s*([A-Z][A-Z0-9]*-\d+)\s*\|")


def split_spans(artifact_id: str, version: str, content: str, shown: str | None = None) -> list[dict]:
    """content(원문)로 구간 키를 정하고 텍스트는 shown(마스킹본)에서 같은 줄을 쓴다."""
    lines = content.split("\n")
    texts = lines if shown is None else shown.split("\n")
    if len(texts) != len(lines):   # 마스킹이 줄 수를 바꾼 경우: 원문이 새지 않게 통째로 한 구간
        return [{"span_id": f"{artifact_id}:{version}:L01", "text": shown}]
    keyed = any(ROW_KEY_RE.match(line) for line in lines)
    out, header = [], False
    for n, (line, text) in enumerate(zip(lines, texts), 1):
        if not line.strip() or SEP_RE.match(line):
            continue
        m = ROW_KEY_RE.match(line)
        if m:
            key = m.group(1)
        elif keyed and not header and line.lstrip().startswith("|"):
            key, header = "HEADER", True
        else:
            key = f"L{n:02d}"
        out.append({"span_id": f"{artifact_id}:{version}:{key}", "text": text})
    return out
