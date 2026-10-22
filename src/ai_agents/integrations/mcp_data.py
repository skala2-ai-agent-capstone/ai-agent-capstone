"""Business 응답을 보존하면서 분류 입력에서는 정답 유형과 원본 ID를 제외한다."""

import json

from pydantic import Field

from ..criteria_agent.builder import check_policy, required_sections
from ..criteria_agent.policy import PolicyBundle, PolicyChunk
from ..shared.models import Artifact, Message, NonEmpty, StrictModel, WorkContext, WorkType
from .mcp_client import MCPError, ToolGateway


class BusinessPacket(StrictModel):
    run_id: NonEmpty
    snapshot: dict
    work_case: dict
    messages: list[dict]
    artifacts: list[dict]
    source_documents: list[dict]
    supplemental: dict = Field(default_factory=dict)
    unavailable: list[str] = Field(default_factory=list)

    def evidence_sources(self) -> dict[str, str]:
        sources = {m["message_id"]: m["content"] for m in self.messages}
        for artifact in self.artifacts:
            sources.update({s["span_id"]: s["text"] for s in artifact["spans"]})
        for doc in self.source_documents:
            sources.update({line["line_id"]: line["text"] for line in doc["lines"]})
        case = self.work_case
        wid = case["work_id"]
        if case.get("registration") is not None:
            sources[f"{wid}:registration"] = json.dumps(
                case["registration"], ensure_ascii=False, sort_keys=True,
            )
        if case.get("source_message"):
            message = case["source_message"]
            sources[f"{wid}:source_message"] = message["content"]
            for i, line in enumerate(message["content"].split("\n"), 1):
                sources[f"{wid}:source_message:L{i:02d}"] = line
        for name, suffix in (("policy_check", "policy_check"), ("quote", "reference_quote")):
            if name in self.supplemental:
                sources[f"{wid}:{suffix}"] = json.dumps(
                    self.supplemental[name], ensure_ascii=False, sort_keys=True,
                )
        for name, collection, id_key in (
            ("access_log", "accesses", "access_id"),
            ("evidence_links", "links", "link_id"),
            ("approval_requests", "requests", "approval_id"),
        ):
            for record in self.supplemental.get(name, {}).get(collection, []):
                sources[record[id_key]] = json.dumps(record, ensure_ascii=False, sort_keys=True)
        return sources


async def fetch_business(gateway: ToolGateway, work_id: str) -> BusinessPacket:
    first = await gateway.call("business", "get_work_case", {"work_id": work_id})
    case, meta = first["data"], first["meta"]
    if case.get("work_id") != work_id:
        raise MCPError("WORK_MISMATCH", "get_work_case")
    snapshot = meta["snapshot"]

    async def get(tool, args):
        reply = await gateway.call("business", tool, args)
        if (reply["meta"]["run_id"] != meta["run_id"]
                or reply["meta"]["snapshot"] != snapshot):
            raise MCPError("SNAPSHOT_CHANGED", tool)
        return reply["data"]

    messages = []
    rng = snapshot["conversation_range"]
    cursor = rng["from_seq"]
    seen = set()
    for _ in range(100):
        page = await get("get_conversation", {
            "work_id": work_id, "from_seq": cursor, "to_seq": rng["to_seq"], "limit": 50,
        })
        previous_seq = cursor - 1
        for message in page["messages"]:
            seq = message["seq"]
            if (not previous_seq < seq <= rng["to_seq"]
                    or message["message_id"] in seen):
                raise MCPError("INVALID_PAGINATION", "get_conversation")
            seen.add(message["message_id"])
            previous_seq = seq
            messages.append(message)
        nxt = page["next_seq"]
        if nxt is None:
            break
        if not max(cursor, previous_seq) < nxt <= rng["to_seq"]:
            raise MCPError("INVALID_PAGINATION", "get_conversation")
        cursor = nxt
    else:
        raise MCPError("PAGE_LIMIT", "get_conversation")

    artifacts = []
    target = snapshot["artifact_ref"]
    refs = case["artifact_refs"]
    if not any(all(ref[k] == target[k] for k in ("artifact_id", "version")) for ref in refs):
        raise MCPError("TARGET_NOT_FOUND", "get_artifact_version")
    seen_refs = set()
    for ref in refs:
        key = (ref["artifact_id"], ref["version"])
        if key in seen_refs:
            raise MCPError("DUPLICATE_ARTIFACT", "get_work_case")
        seen_refs.add(key)
        artifact = await get("get_artifact_version", {
            "artifact_id": ref["artifact_id"], "version": ref["version"],
        })
        if (artifact.get("work_id") != work_id
                or (artifact.get("artifact_id"), artifact.get("version")) != key):
            raise MCPError("ARTIFACT_MISMATCH", "get_artifact_version")
        is_target = all(ref[k] == target[k] for k in ("artifact_id", "version"))
        if artifact["is_evaluation_target"] is not is_target:
            raise MCPError("ARTIFACT_MISMATCH", "get_artifact_version")
        if is_target and not artifact["content"].strip():
            raise MCPError("EMPTY_TARGET", "get_artifact_version")
        artifacts.append(artifact)

    documents = []
    for doc in case["source_documents"]:
        cursor, lines = 1, []
        for _ in range(100):
            page = await get("get_source_document", {
                "attachment_id": doc["attachment_id"], "from_line": cursor, "limit": 200,
            })
            if any(page[k] != doc[k] for k in ("attachment_id", "doc_id", "version")):
                raise MCPError("SOURCE_MISMATCH", "get_source_document")
            lines.extend(page["lines"])
            nxt = page["next_line"]
            if nxt is None:
                if len(lines) != page["total_lines"]:
                    raise MCPError("INCOMPLETE_SOURCE", "get_source_document")
                break
            if not cursor < nxt <= page["total_lines"]:
                raise MCPError("INVALID_PAGINATION", "get_source_document")
            cursor = nxt
        else:
            raise MCPError("PAGE_LIMIT", "get_source_document")
        documents.append({**page, "lines": lines})

    supplemental, unavailable = {}, []
    for name, tool in (("linked_works", "get_linked_works"),
                       ("access_log", "get_reference_access_log"),
                       ("evidence_links", "get_evidence_links")):
        try:
            supplemental[name] = await get(tool, {"work_id": work_id})
        except MCPError as exc:
            if exc.code != "NOT_FOUND":
                raise
            unavailable.append(tool)
    supplemental["policy_check"] = await get("get_policy_check_result", {
        "work_id": work_id, "version": target["version"],
    })
    previous = [a for a in artifacts if not a["is_evaluation_target"]]
    if previous:
        # 날짜순으로 정렬하며 문자열 v10/v2를 비교하지 않는다.
        previous = [a for a in previous if a["artifact_id"] == target["artifact_id"]]
    if previous:
        before = max(previous, key=lambda a: a["saved_at"])
        supplemental["artifact_diff"] = await get("get_artifact_diff", {
            "artifact_id": target["artifact_id"], "from_version": before["version"],
            "to_version": target["version"],
        })
    return BusinessPacket(
        run_id=meta["run_id"], snapshot=snapshot, work_case=case,
        messages=messages, artifacts=artifacts, source_documents=documents,
        supplemental=supplemental, unavailable=unavailable,
    )


def classification_context(packet: BusinessPacket) -> tuple[WorkContext, dict[str, dict]]:
    """정답 유형·고객/직원 ID·테스트 파일명은 LLM에 전달하지 않는다. 원문은 수정하지 않는다."""
    messages, artifacts, mapping = [], [], {}
    for i, message in enumerate(packet.messages, 1):
        key = f"message-{i:03d}"
        messages.append(Message(id=key, role=message["role"], content=message["content"]))
        mapping[key] = {"message_id": message["message_id"]}
    for i, artifact in enumerate(sorted(packet.artifacts, key=lambda a: a["saved_at"]), 1):
        key = f"artifact-{i:03d}"
        if not artifact["content"].strip():
            continue
        artifacts.append(Artifact(
            id=key, version=i, content=artifact["content"],
            stage="final" if artifact["is_evaluation_target"] else "draft",
        ))
        mapping[key] = {k: artifact[k] for k in ("artifact_id", "version", "spans", "author")}
    for i, doc in enumerate(packet.source_documents, 1):
        key = f"source-{i:03d}"
        text = "\n".join(line["text"] for line in doc["lines"])
        if text.strip():
            artifacts.append(Artifact(id=key, stage="reference", content=text))
            mapping[key] = {k: doc[k] for k in ("attachment_id", "doc_id", "version", "lines")}
    if packet.work_case.get("registration") is not None:
        artifacts.append(Artifact(id="registration", stage="reference", content=json.dumps(
            packet.work_case["registration"], ensure_ascii=False, sort_keys=True,
        )))
        mapping["registration"] = {
            "evidence_id": f"{packet.work_case['work_id']}:registration",
        }
    customer_request = (packet.work_case.get("source_message") or {}).get("content", "")
    if customer_request:
        mapping["work:customer_request"] = {
            "evidence_id": f"{packet.work_case['work_id']}:source_message",
        }
    return WorkContext(
        work_id=packet.work_case["work_id"], customer_request=customer_request,
        messages=messages, artifacts=artifacts, coverage="complete",
    ), mapping


async def fetch_policy(gateway: ToolGateway, work_type: WorkType) -> PolicyBundle:
    sections = sorted(required_sections(work_type) | {"APX-4", "ART-04", "ART-14"})
    reply = await gateway.call("rag", "search_policy", {"clause_ids": sections, "top_k": 5})
    data = reply["data"]
    if data.get("no_evidence") or data.get("missing_clause_ids"):
        raise MCPError("POLICY_INCOMPLETE", "search_policy")
    chunks = []
    for chunk in data["chunks"]:
        expected_ref = f"{chunk['doc_id']}@{chunk['version']}#{chunk['chunk_id']}"
        if chunk["ref"] != expected_ref:
            raise MCPError("REFERENCE_MISMATCH", "search_policy")
        chunks.append(PolicyChunk(
            doc_id=chunk["doc_id"], version=chunk["version"], chunk_id=chunk["chunk_id"],
            clause_id=chunk["clause_id"], label=chunk["label"], text=chunk["text"],
            chapter=None, page=None,
        ))
    policy = PolicyBundle(chunks=chunks)
    check_policy(work_type, policy)
    return policy
