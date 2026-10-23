"""MCP 내규 적재(mcp_servers/common/policy_doc.py)가 평가 기준 생성 Agent와
같은 구간·필수 조항을 쓰는지 대조한다.

MCP 이미지에는 ai_agents가 없어 분할 규칙을 복사해 두었으므로, 한쪽만 바뀌면 여기서 깨진다.
"""
import asyncio
import importlib.util
from pathlib import Path
from typing import get_args

import pytest

from ai_agents.criteria_agent.policy import split_policy_lines
from ai_agents.integrations.mcp_data import fetch_policy
from ai_agents.shared.models import WorkType

_SPEC = importlib.util.spec_from_file_location(
    "mcp_policy_doc", Path(__file__).parents[1] / "mcp_servers" / "common" / "policy_doc.py")
mcp_doc = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mcp_doc)

LINES = [
    (1, "영업 업무 생성형 AI 활용 지침"), (1, "목차"),
    (1, "제6조 정보 보호"), (1, "별표 5 점검 기준"),
    (2, "제1장 총칙"), (2, "제1조 (목적) 이 지침은 목적을 정한다."), (2, "이어지는 목적 문장"),
    (2, "제2장 보호"), (2, "제6조 (정보 보호)"), (2, "서문"), (2, "① 원문 첫째"),
    (2, "2 / 3"), (3, "가상 문서 · 교육용 시연 자료"),
    (3, "이어지는 문장"), (3, "② 원문 둘째"), (3, "제7조 (비식별) ① 같은 줄 항"),
    (3, "별표 5. 기준"), (3, "0/1/2점"),
    (3, "부칙"), (3, "이 지침은 2026년 11월 1일부터 시행한다."),
]


def test_mcp_splitter_matches_agent_splitter():
    agent = [(c.chunk_id, c.clause_id, c.label, c.chapter, c.page, c.text)
             for c in split_policy_lines(LINES, "p", "v1").chunks]
    mcp = [(c["chunk_id"], c["clause_id"], c["label"], c["chapter"], c["page"], c["text"])
           for c in mcp_doc.split_policy_lines(LINES)]
    assert mcp == agent
    assert [c[0] for c in mcp] == [
        "ART-01", "ART-06", "ART-06-1", "ART-06-2", "ART-07-1", "APX-5", "SUPP"]


class _Stop(Exception):
    pass


class _CaptureGateway:
    def __init__(self):
        self.clause_ids = None

    async def call(self, server, tool, arguments):
        assert (server, tool) == ("rag", "search_policy")
        self.clause_ids = set(arguments["clause_ids"])
        raise _Stop


def test_mcp_required_clauses_match_what_agent_fetches():
    fetched = set()
    for work_type in get_args(WorkType):
        gateway = _CaptureGateway()
        with pytest.raises(_Stop):
            asyncio.run(fetch_policy(gateway, work_type))
        fetched |= gateway.clause_ids
    assert set(mcp_doc.AGENT_REQUIRED_CLAUSES) == fetched


def test_read_policy_lines_accepts_utf8_text_and_rejects_other_formats():
    text = "\ufeff제1조 (목적)\n본문".encode()
    assert mcp_doc.read_policy_lines(text, "규정.TXT") == [(None, "제1조 (목적)"), (None, "본문")]
    with pytest.raises(ValueError, match="PDF 또는 TXT"):
        mcp_doc.read_policy_lines(b"x", "규정.docx")
    with pytest.raises(ValueError, match="UTF-8"):
        mcp_doc.read_policy_lines("제1조".encode("cp949"), "규정.txt")
