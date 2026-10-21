"""팀원 노트북의 조·항 분할을 모듈화. 원문 ID와 실제 시작 페이지를 보존합니다."""

import re
from pathlib import Path
from typing import Self

from pydantic import Field, TypeAdapter, model_validator

from .models import NonEmpty, StrictModel

ARTICLE = re.compile(r"^제(\d+)조\s*\((.+?)\)")
APPENDIX = re.compile(r"^별표\s*(\d+)\.\s*(.+)$")
CHAPTER = re.compile(r"^제\d+장\s+.+$")
CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩"
NOISE = [re.compile(r"^가상 문서 · 교육용 시연 자료.*$"), re.compile(r"^\d+\s*/\s*\d+$")]
SECTION = re.compile(r"^(ART|APX)-0*(\d+)(-\d+)?$")


def normalize_section(value: str) -> str:
    """ART-6-2 → ART-06-2. 항 번호를 버리지 않습니다."""
    normalized = value.strip().upper()
    match = SECTION.fullmatch(normalized)
    if not match:
        return normalized
    prefix, number, suffix = match.groups()
    number = f"{int(number):02d}" if prefix == "ART" else str(int(number))
    return f"{prefix}-{number}{suffix or ''}"


class PolicyChunk(StrictModel):
    doc_id: NonEmpty
    version: NonEmpty
    chunk_id: NonEmpty
    clause_id: NonEmpty
    label: NonEmpty
    chapter: str | None
    # RAG MCP는 페이지를 반환하지 않는다. 존재하지 않는 페이지를 만들어 넣지 않는다.
    page: int | None = Field(default=None, ge=1, strict=True)
    text: NonEmpty


class PolicyBundle(StrictModel):
    chunks: list[PolicyChunk] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_snapshot(self) -> Self:
        if len({(c.doc_id, c.version) for c in self.chunks}) != 1:
            raise ValueError("현재 생성기는 한 내규 문서의 한 버전만 입력받습니다.")
        ids = [c.chunk_id for c in self.chunks]
        if len(ids) != len(set(ids)):
            raise ValueError("내규 구간 ID가 중복되었습니다.")
        for chunk in self.chunks:
            if (chunk.chunk_id != normalize_section(chunk.chunk_id)
                    or chunk.clause_id != normalize_section(chunk.clause_id)):
                raise ValueError("내규 ID를 정규 형식으로 입력하세요. 예: ART-06-2")
            if not (chunk.chunk_id == chunk.clause_id
                    or chunk.chunk_id.startswith(chunk.clause_id + "-")):
                raise ValueError("구간 ID와 상위 조항 ID가 일치하지 않습니다.")
        if sum(len(c.text) for c in self.chunks) > 150_000:
            raise ValueError("내규 본문이 150,000자를 초과했습니다. 필요한 구간만 제공하세요.")
        return self

    def resolve(self, section: str) -> list[PolicyChunk]:
        section = normalize_section(section)
        # 전체 조를 요청하면 머리말 구간뿐 아니라 모든 항을 반환합니다.
        children = [c for c in self.chunks if c.clause_id == section]
        return children or [c for c in self.chunks if c.chunk_id == section]


def split_policy_lines(lines: list[tuple[int, str]], doc_id: str, version: str) -> PolicyBundle:
    chunks = []
    chapter = None
    clause_id = None
    clause_label = ""
    current = None

    def flush():
        nonlocal current
        if current and current["lines"]:
            chunks.append(PolicyChunk(
                doc_id=doc_id, version=version, chunk_id=current["id"],
                clause_id=current["clause"], label=current["label"],
                chapter=current["chapter"], page=current["page"],
                text="\n".join(current["lines"]),
            ))
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
        current = {"id": chunk_id, "clause": clause_id, "label": label,
                   "chapter": chapter, "page": page, "lines": [line]}
    flush()
    return PolicyBundle(chunks=chunks)


def load_policy_pdf(path: Path, *, doc_id: str, version: str) -> PolicyBundle:
    import pdfplumber

    if path.stat().st_size > 30_000_000:
        raise ValueError("내규 PDF는 30MB 이하여야 합니다.")
    lines = []
    with pdfplumber.open(path) as pdf:
        if len(pdf.pages) > 100:
            raise ValueError("내규 PDF는 100페이지 이하여야 합니다.")
        for page, content in enumerate(pdf.pages, start=1):
            lines.extend((page, line) for line in (content.extract_text() or "").splitlines())
    if not lines:
        raise ValueError("PDF에서 텍스트를 추출할 수 없습니다. OCR이 필요한지 확인하세요.")
    return split_policy_lines(lines, doc_id, version)


def load_policy_chunks(path: Path) -> PolicyBundle:
    with path.open("rb") as stream:
        raw = stream.read(2_000_001)
    if len(raw) > 2_000_000:
        raise ValueError("내규 구간 JSON은 2MB 이하여야 합니다.")
    # 노트북의 policy_chunks.json 배열을 그대로 입력할 수 있습니다.
    return PolicyBundle(chunks=TypeAdapter(list[PolicyChunk]).validate_json(raw))
