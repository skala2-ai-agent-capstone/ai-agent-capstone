"""분류 모듈이 승인받은 MCPHandoff를 평가기와 연결한다."""

from copy import deepcopy

from openai import AsyncOpenAI

from ..integrations.mcp_adapter import assemble_evaluation_evidence
from ..integrations.mcp_client import MCPError, ToolGateway
from ..orchestration.graph import MCPHandoff
from ..shared.evidence_models import EvaluationRun
from ..shared.workflow_models import content_hash
from .agent import OpenAIEvaluator
from .materials import collect_evaluation_materials
from .models import EvaluationReceipt
from .validation import evidence_catalog


class ApprovedBusinessCache:
    """승인받은 원문은 다시 조회하지 않는다. 추가 조회만 같은 gateway로 수행한다."""

    def __init__(self, handoff: MCPHandoff, gateway: ToolGateway):
        self.handoff = MCPHandoff.model_validate(handoff.model_dump(mode="json"))
        self.packet = self.handoff.business
        self.gateway = gateway
        self.context = EvaluationRun(
            run_id=self.packet.run_id,
            work_id=self.packet.work_case["work_id"],
            snapshot=self.packet.snapshot,
        )

    def envelope(self, data):
        return {
            "ok": True,
            "data": deepcopy(data),
            "meta": {
                "run_id": self.context.run_id,
                "snapshot": deepcopy(self.context.snapshot),
            },
        }

    def artifact(self, raw):
        data = {
            key: raw[key]
            for key in (
                "artifact_id",
                "work_id",
                "version",
                "author",
                "saved_at",
                "is_evaluation_target",
                "content",
            )
        }
        data["spans"] = [{"span_id": s["span_id"], "text": s["text"]} for s in raw["spans"]]
        return data

    def work_case(self):
        case = deepcopy(self.packet.work_case)
        wid = self.context.work_id
        case.setdefault(
            "registration_evidence_id",
            f"{wid}:registration" if case.get("registration") is not None else None,
        )
        if case.get("source_message") is not None:
            source = case["source_message"]
            source.setdefault("evidence_id", f"{wid}:source_message")
            source.setdefault("content_type", "data")
            source.setdefault("channel", None)
            source.setdefault("received_at", None)
            source.setdefault(
                "lines",
                [
                    {"line_id": f"{wid}:source_message:L{i:02d}", "text": t}
                    for i, t in enumerate(source["content"].split("\n"), 1)
                ],
            )
        by_id = {(a["artifact_id"], a["version"]): a for a in self.packet.artifacts}
        refs = []
        for ref in case["artifact_refs"]:
            raw = by_id[(ref["artifact_id"], ref["version"])]
            full = {
                k: raw[k]
                for k in (
                    "artifact_id",
                    "version",
                    "author",
                    "saved_at",
                    "is_evaluation_target",
                )
            }
            if any(k in ref and ref[k] != value for k, value in full.items()):
                raise ValueError("승인한 산출물 본문과 참조 정보가 다릅니다.")
            refs.append(full)
        case["artifact_refs"] = refs
        return case

    def base_evidence(self):
        return assemble_evaluation_evidence(
            context=self.context,
            work_case_response=self.envelope(self.work_case()),
            artifact_responses=[self.envelope(self.artifact(a)) for a in self.packet.artifacts],
            policy_check_response=(
                self.envelope(self.packet.supplemental["policy_check"])
                if "policy_check" in self.packet.supplemental
                else None
            ),
        )

    async def business(self, name, arguments):
        packet = self.packet
        if "work_id" in arguments and arguments["work_id"] != self.context.work_id:
            raise ValueError("승인한 업무 밖의 조회입니다.")
        if name == "get_work_case":
            return self.envelope(self.work_case())
        if name == "get_conversation":
            bounds = packet.snapshot["conversation_range"]
            start, end = arguments["from_seq"], arguments["to_seq"]
            if not bounds["from_seq"] <= start <= end <= bounds["to_seq"]:
                raise ValueError("승인한 대화 범위를 벗어난 조회입니다.")
            rows = [m for m in packet.messages if start <= m["seq"] <= end]
            size = arguments.get("limit", 50)
            return self.envelope(
                {
                    "work_id": self.context.work_id,
                    "conversation_range": bounds,
                    "messages": rows[:size],
                    "next_seq": rows[size]["seq"] if len(rows) > size else None,
                }
            )
        if name == "get_source_document":
            document = next(
                (
                    d
                    for d in packet.source_documents
                    if d["attachment_id"] == arguments["attachment_id"]
                ),
                None,
            )
            if document is not None:
                start, limit = arguments.get("from_line", 1), arguments.get("limit", 200)
                lines = document["lines"]
                return self.envelope(
                    {
                        **document,
                        "lines": lines[start - 1 : start - 1 + limit],
                        "total_lines": len(lines),
                        "next_line": start + limit if start + limit <= len(lines) else None,
                    }
                )
        cache = {
            "get_reference_access_log": "access_log",
            "get_evidence_links": "evidence_links",
            "get_approval_requests": "approval_requests",
            "get_policy_check_result": "policy_check",
            "compute_reference_quote": "quote",
            "get_linked_works": "linked_works",
        }
        if name in cache and cache[name] in packet.supplemental:
            return self.envelope(packet.supplemental[cache[name]])
        if name == "get_artifact_diff" and "artifact_diff" in packet.supplemental:
            diff = packet.supplemental["artifact_diff"]
            if all(diff.get(key) == value for key, value in arguments.items()):
                return self.envelope(diff)
        return await self.fresh("business", name, arguments)

    async def fresh(self, server, name, arguments):
        try:
            return await self.gateway.call(server, name, arguments)
        except MCPError as exc:
            return {
                "ok": False,
                "error": {
                    "code": exc.code,
                    "message": "MCP 추가 조회 실패",
                    "retryable": exc.code == "TEMPORARY_FAILURE",
                },
            }

    async def rag(self, name, arguments):
        return await self.fresh("rag", name, arguments)


class HandoffEvaluator:
    """build_agent_graph(..., evaluator=HandoffEvaluator(...))의 실제 평가 모듈 구현."""

    def __init__(self, client: AsyncOpenAI, gateway: ToolGateway, *, model="gpt-4o-mini"):
        self.client, self.gateway, self.model = client, gateway, model

    async def evaluate(self, handoff: MCPHandoff) -> EvaluationReceipt:
        cache = ApprovedBusinessCache(handoff, self.gateway)
        request = cache.handoff.evaluation_input
        evidence = cache.base_evidence()
        materials = await collect_evaluation_materials(
            evidence=evidence,
            criteria=request.approved_criteria,
            business_call=cache.business,
            rag_call=cache.rag,
        )
        evaluator = OpenAIEvaluator(
            self.client,
            context=cache.context,
            business_call=cache.business,
            rag_call=cache.rag,
            model=self.model,
        )
        result = await evaluator.evaluate_collected(request, evidence=evidence, materials=materials)
        sources, _ = evidence_catalog(request, evidence, materials)
        return EvaluationReceipt(
            report=result,
            approved_business_sha256=cache.handoff.approved_business_sha256,
            snapshot=cache.context.snapshot,
            materials_sha256=content_hash(materials),
            additional_sources=sources,
            additional_references=materials.references,
        )
