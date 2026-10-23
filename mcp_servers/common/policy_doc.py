"""내규 파일(PDF·TXT)을 조·항 구간으로 나눈다 (business-mcp 내규 적재용).

분할 규칙은 평가 기준 생성 Agent의 src/ai_agents/criteria_agent/policy.py(split_policy_lines·load_policy_pdf)와 같다.
Agent가 search_policy로 받는 구간 ID(ART-02-1·APX-4 등)가 적재 결과와 맞아야 하므로, 한쪽을 바꾸면 다른 쪽도 같이 고친다
(루트 tests/test_policy_ingest_parity.py가 두 구현을 대조한다).
MCP 이미지에는 src/ai_agents가 들어가지 않아 import 대신 같은 규칙을 여기 둔다.
"""
from __future__ import annotations

import io
import re

ARTICLE = re.compile(r"^제(\d+)조\s*\((.+?)\)")
APPENDIX = re.compile(r"^별표\s*(\d+)\.\s*(.+)$")
CHAPTER = re.compile(r"^제\d+장\s+.+$")
CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩"
NOISE = [re.compile(r"^가상 문서 · 교육용 시연 자료.*$"), re.compile(r"^\d+\s*/\s*\d+$")]

MAX_PDF_BYTES = 30_000_000
MAX_PDF_PAGES = 100

# 평가 기준 생성 Agent가 내규에서 반드시 찾는 조항: required_sections(업무 유형 4개 합집합) + fetch_policy가 더 읽는 APX-4·ART-04·ART-14.
# 이 중 하나라도 없으면 Agent가 "필수 내규 구간이 없습니다"로 멈추므로 적재 단계에서 거절한다.
AGENT_REQUIRED_CLAUSES = frozenset({
    "APX-1", "APX-2", "APX-3", "APX-4", "APX-5",
    "ART-04", "ART-06", "ART-07", "ART-09", "ART-11", "ART-12", "ART-13", "ART-14", "ART-15",
    "ART-17", "ART-18", "ART-19", "ART-20", "ART-21", "ART-23",
})


def split_policy_lines(lines: list[tuple[int | None, str]]) -> list[dict]:
    """(쪽, 줄) 목록 → [{chunk_id, clause_id, label, chapter, page, text}]. 표지·목차(첫 조항 앞)와 쪽 번호는 버린다."""
    chunks: list[dict] = []
    chapter = None
    clause_id = None
    clause_label = ""
    current = None

    def flush():
        nonlocal current
        if current and current["lines"]:
            chunks.append({"chunk_id": current["id"], "clause_id": current["clause"], "label": current["label"],
                           "chapter": current["chapter"], "page": current["page"], "text": "\n".join(current["lines"])})
        current = None

    for page, raw in lines:
        line = raw.strip()
        if not line or any(pattern.match(line) for pattern in NOISE):
            continue
        if CHAPTER.match(line):
            flush()
            chapter = line
            clause_id = None
            continue
        article, appendix = ARTICLE.match(line), APPENDIX.match(line)
        if article or appendix or line == "부칙":
            flush()
            if article:
                clause_id = f"ART-{int(article.group(1)):02d}"
                clause_label = line[:article.end()]
                remainder = line[article.end():].strip()
            elif appendix:
                clause_id = f"APX-{int(appendix.group(1))}"
                clause_label, chapter, remainder = line, "별표", ""
            else:
                clause_id, clause_label, chapter, remainder = "SUPP", "부칙", "부칙", ""
            if not remainder:
                continue
            line = remainder
        if clause_id is None:
            continue  # 표지·목차 제외
        if line[0] in CIRCLED:
            flush()
            index = CIRCLED.index(line[0]) + 1
            chunk_id, label = f"{clause_id}-{index}", f"{clause_label} 제{index}항"
        elif current:
            current["lines"].append(line)
            continue
        else:
            chunk_id, label = clause_id, clause_label
        current = {"id": chunk_id, "clause": clause_id, "label": label, "chapter": chapter, "page": page, "lines": [line]}
    flush()
    return chunks


def read_policy_lines(data: bytes, filename: str) -> list[tuple[int | None, str]]:
    """파일 내용 → (쪽, 줄) 목록. PDF는 쪽 번호를, TXT(UTF-8)는 None을 붙인다. 형식·크기가 맞지 않으면 ValueError."""
    name = filename.lower()
    if name.endswith(".pdf"):
        import pdfplumber

        if len(data) > MAX_PDF_BYTES:
            raise ValueError("내규 PDF는 30MB 이하여야 합니다")
        lines: list[tuple[int | None, str]] = []
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            if len(pdf.pages) > MAX_PDF_PAGES:
                raise ValueError("내규 PDF는 100페이지 이하여야 합니다")
            for page, content in enumerate(pdf.pages, start=1):
                lines.extend((page, line) for line in (content.extract_text() or "").splitlines())
        if not lines:
            raise ValueError("PDF에서 텍스트를 추출할 수 없습니다. OCR이 필요한지 확인하세요")
        return lines
    if name.endswith(".txt"):
        if len(data) > MAX_PDF_BYTES:
            raise ValueError("내규 TXT는 30MB 이하여야 합니다")
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as e:
            raise ValueError("내규 TXT는 UTF-8이어야 합니다") from e
        return [(None, line) for line in text.splitlines()]
    raise ValueError("내규는 PDF 또는 TXT 파일만 올릴 수 있습니다")
