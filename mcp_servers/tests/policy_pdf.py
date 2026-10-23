"""검증용 내규 PDF 만들기 (라이브러리 없이). verify_documents.py가 시드 내규 구간으로 PDF를 만들어 /internal/documents에 올린다.

한글은 글꼴을 넣지 않는 표준 CJK 글꼴(HYGoThic-Medium, UniKS-UTF16-H)로 쓴다. pdfplumber(pdfminer)는 이 인코딩의
CMap을 갖고 있어 원문 그대로 뽑는다. 긴 줄이 잘리지 않도록 쪽 너비를 넉넉하게 둔다.
"""
from __future__ import annotations

import re

ARTICLE = re.compile(r"^제(\d+)조\s*\((.+?)\)")
LINES_PER_PAGE = 50


def policy_lines(chunks: list[dict]) -> list[str]:
    """시드 구간(chunk_id·clause_id·label·text) → 원문처럼 조 제목 줄 + 본문 줄. 같은 분할기로 다시 나누면 같은 구간이 나온다."""
    lines = ["영업 업무 생성형 AI 활용 지침", "목차"]
    clause = None
    for ch in chunks:
        if ch["clause_id"] != clause:
            clause = ch["clause_id"]
            m = ARTICLE.match(ch["label"])
            lines.append(m.group(0) if m else ch["label"])
        lines.extend(ch["text"].split("\n"))
    return lines


def _obj(n: int, body: bytes) -> bytes:
    return b"%d 0 obj\n" % n + body + b"\nendobj\n"


def build_pdf(lines: list[str]) -> bytes:
    pages = [lines[i:i + LINES_PER_PAGE] for i in range(0, len(lines), LINES_PER_PAGE)] or [[]]
    font = b"<< /Type /Font /Subtype /Type0 /BaseFont /HYGoThic-Medium /Encoding /UniKS-UTF16-H /DescendantFonts [4 0 R] >>"
    cid = (b"<< /Type /Font /Subtype /CIDFontType0 /BaseFont /HYGoThic-Medium /DW 1000 /FontDescriptor 5 0 R "
           b"/CIDSystemInfo << /Registry (Adobe) /Ordering (Korea1) /Supplement 1 >> >>")
    desc = (b"<< /Type /FontDescriptor /FontName /HYGoThic-Medium /Flags 6 /FontBBox [-6 -145 1003 880] /ItalicAngle 0 "
            b"/Ascent 880 /Descent -120 /CapHeight 880 /StemV 93 >>")
    objs = {3: font, 4: cid, 5: desc}
    kids = []
    for p, page_lines in enumerate(pages):
        page_no, content_no = 6 + 2 * p, 7 + 2 * p
        kids.append(page_no)
        body = [f"{p + 1} / {len(pages)}", "가상 문서 · 교육용 시연 자료", *page_lines]   # 쪽 번호·머리말은 분할기가 버린다
        ops = [b"BT /F1 10 Tf"]
        for i, text in enumerate(body):
            ops.append(b"1 0 0 1 40 %d Tm <%s> Tj" % (800 - 15 * i, text.encode("utf-16-be").hex().encode()))
        ops.append(b"ET")
        stream = b"\n".join(ops)
        objs[content_no] = b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"
        objs[page_no] = (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 4000 842] /Resources << /Font << /F1 3 0 R >> >> "
                         b"/Contents %d 0 R >>" % content_no)
    objs[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objs[2] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (b" ".join(b"%d 0 R" % k for k in kids), len(kids))
    out = bytearray(b"%PDF-1.4\n")
    offsets = {}
    for n in sorted(objs):
        offsets[n] = len(out)
        out += _obj(n, objs[n])
    xref = len(out)
    size = max(objs) + 1
    out += b"xref\n0 %d\n0000000000 65535 f \n" % size
    for n in range(1, size):
        out += b"%010d 00000 n \n" % offsets[n]
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (size, xref)
    return bytes(out)
