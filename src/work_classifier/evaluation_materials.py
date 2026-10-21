"""평가에 필요한 추가 business/RAG 조회. 승인된 기준은 변경하지 않는다."""

import json
from datetime import datetime

from pydantic import ConfigDict, JsonValue

from .evidence_models import EvaluationEvidence, EvaluationRun
from .mcp_adapter import BusinessToolCaller, MCPToolError, decode_mcp_result, read_mcp_response
from .models import NonEmpty, StrictModel
from .workflow_models import CriteriaDraft, Reference, content_hash


class EvaluationMaterials(StrictModel):
    model_config = ConfigDict(str_strip_whitespace=False)

    context: EvaluationRun
    evidence_sha256: NonEmpty
    work_sources: dict[str, str]
    source_kinds: dict[str, str]
    references: list[Reference]
    details: dict[str, JsonValue]
    missing_sources: list[str]


class RetrievedChunk(StrictModel):
    model_config = ConfigDict(extra="allow", str_strip_whitespace=False)

    ref: NonEmpty
    chunk_id: NonEmpty
    text: str


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _before_cutoff(value: object, context: EvaluationRun) -> bool:
    if not isinstance(value, str):
        raise ValueError("조회 기록의 시각이 없습니다.")
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.utcoffset() is None:
        raise ValueError("조회 기록의 시각에는 시간대가 필요합니다.")
    return stamp <= context.target.saved_at


async def collect_evaluation_materials(
    *,
    evidence: EvaluationEvidence,
    criteria: CriteriaDraft,
    business_call: BusinessToolCaller,
    rag_call: BusinessToolCaller,
) -> EvaluationMaterials:
    context = evidence.context
    sources: dict[str, str] = {}
    kinds: dict[str, str] = {}
    details: dict[str, JsonValue] = {}
    references: dict[tuple[str, str, str], Reference] = {}
    missing: list[str] = []

    def add(source_id: str, text: str, kind: str) -> None:
        if source_id in sources and sources[source_id] != text:
            raise ValueError("추가 조회 결과에 중복된 근거 ID가 있습니다.")
        sources[source_id], kinds[source_id] = text, kind

    async def call(caller, name, arguments, *, optional=False):
        try:
            result = decode_mcp_result(await caller(name, arguments))
            return read_mcp_response(result, context=context).data
        except MCPToolError as exc:
            if optional and exc.code == "NOT_FOUND":
                missing.append(f"{name}:{_json(arguments)}")
                return None
            raise

    # 대화는 서버가 지정한 순번 범위를 끝까지 조회한다.
    bounds = context.snapshot["conversation_range"]
    start, end = bounds["from_seq"], bounds["to_seq"]
    seq = start
    seen = set()
    messages = []
    while True:
        page = await call(
            business_call,
            "get_conversation",
            {
                "work_id": context.work_id,
                "from_seq": seq,
                "to_seq": end,
                "limit": 50,
            },
        )
        if page.get("work_id") != context.work_id or page.get("conversation_range") != bounds:
            raise ValueError("대화의 업무 또는 조회 범위가 다릅니다.")
        for message in page["messages"]:
            number = message["seq"]
            if type(number) is not int or not seq <= number <= end or number in seen:
                raise ValueError("대화 순번이 범위를 벗어나거나 중복되었습니다.")
            if message["role"] not in {"user", "assistant", "system", "tool"}:
                raise ValueError("알 수 없는 대화 역할입니다.")
            seen.add(number)
            add(message["message_id"], message["content"], f"message:{message['role']}")
            messages.append(message)
        next_seq = page.get("next_seq")
        if next_seq is None:
            break
        if type(next_seq) is not int or not seq < next_seq <= end:
            raise ValueError("대화 페이지의 다음 순번이 올바르지 않습니다.")
        seq = next_seq
    expected = set(range(start, end + 1)) if start else set()
    if seen != expected:
        raise ValueError("스냅샷 범위의 대화가 누락되었습니다.")
    if evidence.policy_check is not None:
        user_ids = {m["message_id"] for m in messages if m["role"] == "user"}
        if user_ids != set(evidence.policy_check.scope.user_message_ids):
            raise ValueError("보안 검사의 직원 대화 범위가 실제 대화와 다릅니다.")
    details["messages"] = messages

    # 참고 열람만으로 직원의 검증 행동을 인정하지 않도록 종류를 구분한다.
    for tool, field, id_field, date_field, kind in [
        ("get_reference_access_log", "accesses", "access_id", "accessed_at", "access"),
        ("get_evidence_links", "links", "link_id", "created_at", "evidence_link"),
        ("get_approval_requests", "requests", "approval_id", "submitted_at", "approval_request"),
    ]:
        data = await call(business_call, tool, {"work_id": context.work_id})
        if data.get("work_id") != context.work_id:
            raise ValueError("다른 업무의 추가 기록입니다.")
        records = [r for r in data[field] if _before_cutoff(r[date_field], context)]
        details[field] = records
        for record in records:
            if kind == "evidence_link" and record["span_id"] not in {
                span.span_id for artifact in evidence.artifacts for span in artifact.spans
            }:
                raise ValueError("변경 근거 링크가 이번 업무의 산출물 구간에 없습니다.")
            if kind == "approval_request" and record["artifact_version"] not in {
                artifact.version for artifact in evidence.artifacts
            }:
                raise ValueError("승인 요청이 이번 업무의 허용 산출물 버전에 없습니다.")
            add(record[id_field], _json(record), kind)

    artifacts = sorted(evidence.artifacts, key=lambda artifact: artifact.saved_at)
    diffs = []
    for before, after in zip(artifacts, artifacts[1:]):
        if before.artifact_id != after.artifact_id:
            continue
        diff = await call(
            business_call,
            "get_artifact_diff",
            {
                "artifact_id": before.artifact_id,
                "from_version": before.version,
                "to_version": after.version,
            },
        )
        if (diff.get("artifact_id"), diff.get("from_version"), diff.get("to_version")) != (
            before.artifact_id,
            before.version,
            after.version,
        ):
            raise ValueError("변경 비교의 산출물 또는 버전이 다릅니다.")
        diffs.append(diff)
    details["artifact_diffs"] = diffs

    if criteria.work_type == "quotation":
        data = await call(
            business_call,
            "compute_reference_quote",
            {
                "work_id": context.work_id,
                "artifact_version": context.target.version,
            },
            optional=True,
        )
        if data is not None:
            if (
                data.get("work_id") != context.work_id
                or data.get("artifact_version") != context.target.version
                or data.get("evidence_id") != f"{context.work_id}:reference_quote"
            ):
                raise ValueError("기준 견적의 업무 또는 버전이 다릅니다.")
            add(data["evidence_id"], _json(data), "reference_quote")
            details["reference_quote"] = data

    # 고객 첨부 원문: 페이지를 모두 읽고 줄 ID를 그대로 사용한다.
    documents = evidence.work_case.source_documents
    if documents is None:
        missing.append("source_documents")
    for document in documents or []:
        line = 1
        collected = []
        while True:
            data = await call(
                business_call,
                "get_source_document",
                {
                    "attachment_id": document["attachment_id"],
                    "from_line": line,
                    "limit": 200,
                },
            )
            if (data.get("attachment_id"), data.get("doc_id"), data.get("version")) != (
                document["attachment_id"],
                document["doc_id"],
                document["version"],
            ):
                raise ValueError("고객 첨부의 문서 또는 버전이 다릅니다.")
            for offset, row in enumerate(data["lines"], line):
                if row["line_id"] != f"{document['attachment_id']}:L{offset:02d}":
                    raise ValueError("고객 첨부의 줄 ID가 올바르지 않습니다.")
                add(row["line_id"], row["text"], "customer_document")
                collected.append(row)
            next_line = data.get("next_line")
            if next_line is None:
                if len(collected) != data["total_lines"]:
                    raise ValueError("고객 첨부 원문이 일부 누락되었습니다.")
                break
            if type(next_line) is not int or next_line != len(collected) + 1 or next_line <= line:
                raise ValueError("고객 원문의 페이지 순서가 올바르지 않습니다.")
            line = next_line

    # 승인된 기준의 출처는 검색 유사도로 대체하지 않고 정확 조회해 원문을 대조한다.
    for approved in criteria.references:
        data = await call(
            rag_call,
            "get_reference",
            {
                "doc_id": approved.document_id,
                "version": approved.version,
                "chunk_id": approved.section_id,
            },
        )
        expected_ref = f"{approved.document_id}@{approved.version}#{approved.section_id}"
        if (
            data.get("doc_id"),
            data.get("version"),
            data.get("chunk_id"),
            data.get("ref"),
            data.get("text"),
        ) != (
            approved.document_id,
            approved.version,
            approved.section_id,
            expected_ref,
            approved.content,
        ):
            raise ValueError("승인된 평가기준의 출처 원문·버전이 RAG와 다릅니다.")
        references[(approved.document_id, approved.version, approved.section_id)] = approved

    # 허용된 자료 및 연결 업무의 문서만 정확 조회한다. 폐기·초안은 사실 기준으로 쓰지 않는다.
    reference_details = []
    allowed = context.snapshot["reference_refs"] + context.snapshot["linked_doc_refs"]
    for ref in {f"{r['doc_id']}@{r['version']}": r for r in allowed}.values():
        data = await call(
            rag_call,
            "get_reference",
            {
                "doc_id": ref["doc_id"],
                "version": ref["version"],
            },
            optional=True,
        )
        if data is None:
            continue
        if (data.get("doc_id"), data.get("version")) != (ref["doc_id"], ref["version"]):
            raise ValueError("허용 목록과 다른 참조 자료가 반환되었습니다.")
        reference_details.append({k: data.get(k) for k in ("doc_id", "version", "status", "grade")})
        if data.get("status") != "현행" or data.get("grade") in {"대외비", "기밀"}:
            missing.append(f"non_current_reference:{ref['doc_id']}@{ref['version']}")
            continue
        for raw in data["chunks"]:
            chunk = RetrievedChunk.model_validate(raw)
            if chunk.ref != f"{ref['doc_id']}@{ref['version']}#{chunk.chunk_id}":
                raise ValueError("참조 구간의 근거 ID가 올바르지 않습니다.")
            reference = Reference(
                document_id=ref["doc_id"],
                version=ref["version"],
                section_id=chunk.chunk_id,
                content=chunk.text,
            )
            key = (reference.document_id, reference.version, reference.section_id)
            if key in references and references[key].content != reference.content:
                raise ValueError("같은 버전·구간에 서로 다른 참조 원문이 있습니다.")
            references[key] = reference
    details["reference_status"] = reference_details
    return EvaluationMaterials(
        context=context,
        evidence_sha256=content_hash(evidence),
        work_sources=sources,
        source_kinds=kinds,
        references=list(references.values()),
        details=details,
        missing_sources=missing,
    )
