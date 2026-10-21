"""평가 Agent. 승인 입력 → MCP 근거 → GPT → 원문 검증 → 결과 반환."""

import asyncio
import json

from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI
from pydantic import ValidationError

from .evaluation_materials import EvaluationMaterials, collect_evaluation_materials
from .evaluation_models import AgentEvaluationOutput
from .evaluation_prompts import SYSTEM_PROMPT
from .evaluation_validation import (
    EvaluationError,
    evidence_catalog,
    validate_criteria,
    validate_evaluation_result,
    validate_materials,
)
from .evaluator import build_evaluation_payload
from .evidence_models import EvaluationEvidence, EvaluationRun
from .mcp_adapter import (
    BusinessToolCaller,
    MCPToolError,
    collect_evaluation_evidence,
    decode_mcp_result,
    read_mcp_response,
)
from .workflow_models import EvaluationInput

MAX_INPUT_CHARS = 180_000


class OpenAIEvaluator:
    """LocalCriteriaPartner(evaluator=...)에 그대로 전달할 수 있는 평가기."""

    def __init__(
        self,
        client: AsyncOpenAI,
        *,
        context: EvaluationRun,
        business_call: BusinessToolCaller,
        rag_call: BusinessToolCaller,
        model: str = "gpt-4o-mini",
        max_validation_retries: int = 1,
    ):
        if not 0 <= max_validation_retries <= 2:
            raise ValueError("평가 검증 재시도는 0~2회로 제한합니다.")
        self.client, self.context = client, context
        self.business_call, self.rag_call = business_call, rag_call
        self.model, self.max_validation_retries = model, max_validation_retries

    async def evaluate(self, request: EvaluationInput) -> AgentEvaluationOutput:
        request = EvaluationInput.model_validate(request.model_dump(mode="json"))
        validate_criteria(request)
        context = EvaluationRun.model_validate(self.context.model_dump(mode="json"))
        if request.run_id != context.run_id or request.work_record.work_id != context.work_id:
            raise EvaluationError("평가 입력과 MCP 실행 범위가 다릅니다.")

        async def business(name, arguments):
            return await self._read(self.business_call, name, arguments, context)

        async def rag(name, arguments):
            return await self._read(self.rag_call, name, arguments, context)

        evidence = await collect_evaluation_evidence(context=context, call_tool=business)
        materials = await collect_evaluation_materials(
            evidence=evidence,
            criteria=request.approved_criteria,
            business_call=business,
            rag_call=rag,
        )
        return await self.evaluate_collected(request, evidence=evidence, materials=materials)

    async def _read(self, caller, name, arguments, context):
        for attempt in range(2):
            try:
                decoded = decode_mcp_result(await caller(name, arguments))
                read_mcp_response(decoded, context=context)
                return decoded
            except MCPToolError as exc:
                if attempt or exc.code != "TEMPORARY_FAILURE" or not exc.retryable:
                    raise
                await asyncio.sleep(0.2)
        raise AssertionError("unreachable")

    async def evaluate_collected(
        self,
        request: EvaluationInput,
        *,
        evidence: EvaluationEvidence,
        materials: EvaluationMaterials,
    ) -> AgentEvaluationOutput:
        """이미 수집한 자료를 사용하는 경계. 테스트·A의 수집 노드 재사용용."""
        request = EvaluationInput.model_validate(request.model_dump(mode="json"))
        evidence = EvaluationEvidence.model_validate(evidence.model_dump(mode="json"))
        materials = EvaluationMaterials.model_validate(materials.model_dump(mode="json"))
        validate_criteria(request)
        validate_materials(request, evidence, materials)
        if self.context.model_dump(mode="json") != evidence.context.model_dump(mode="json"):
            raise EvaluationError("평가기와 조회 자료의 실행 범위가 다릅니다.")
        payload = build_evaluation_payload(request, evidence=evidence)
        sources, kinds = evidence_catalog(request, evidence, materials)
        payload["work_evidence_catalog"] = [
            {"source_id": source_id, "kind": kinds[source_id], "text": text}
            for source_id, text in sources.items()
        ]
        refs = {
            (r.document_id, r.version, r.section_id): r
            for r in request.approved_criteria.references + materials.references
        }
        payload["reference_catalog"] = [ref.model_dump(mode="json") for ref in refs.values()]
        payload["additional_details"] = materials.details
        payload["missing_sources"] = sorted(
            set(payload["missing_sources"] + materials.missing_sources)
        )
        encoded = json.dumps(payload, ensure_ascii=False)
        if len(encoded) > MAX_INPUT_CHARS:
            raise EvaluationError("평가 입력이 크기 제한을 초과했습니다. 조회 범위를 줄여주세요.")
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": encoded},
        ]
        for attempt in range(self.max_validation_retries + 1):
            try:
                response = await self.client.responses.parse(
                    model=self.model,
                    input=messages,
                    text_format=AgentEvaluationOutput,
                    max_output_tokens=6000,
                    temperature=0,
                    store=False,
                )
            except (APITimeoutError, APIConnectionError) as exc:
                raise EvaluationError("평가 API 연결 또는 시간 초과 오류입니다.") from exc
            except APIStatusError as exc:
                raise EvaluationError(f"평가 API 오류(HTTP {exc.status_code}).") from exc
            except ValidationError:
                issue = "평가 결과의 JSON 형식·점수·상태가 잘못되었습니다."
            else:
                if response.status != "completed":
                    raise EvaluationError(
                        "평가 응답이 완료되지 않았습니다. 출력 한도를 확인하세요."
                    )
                if any(
                    getattr(c, "type", None) == "refusal"
                    for item in response.output
                    for c in getattr(item, "content", [])
                ):
                    raise EvaluationError("모델이 평가 요청에 대한 응답을 거절했습니다.")
                if response.output_parsed is None:
                    raise EvaluationError("구조화된 평가 결과가 없습니다.")
                try:
                    result = AgentEvaluationOutput.model_validate(
                        response.output_parsed.model_dump(mode="json")
                    )
                    return validate_evaluation_result(
                        request,
                        result,
                        evidence=evidence,
                        materials=materials,
                    )
                except ValidationError:
                    issue = "평가 결과의 JSON 형식·점수·상태가 잘못되었습니다."
                except EvaluationError as exc:
                    issue = str(exc)
            if attempt == self.max_validation_retries:
                raise EvaluationError("평가 결과 검증 실패: " + issue)
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "이전 결과는 검증을 통과하지 못했습니다. 입력·기준은 변경하지 말고 "
                        "다음 오류를 수정하여 전체 결과를 다시 작성하세요: " + issue
                    ),
                }
            )
        raise AssertionError("unreachable")


def render_evaluation_report(result: AgentEvaluationOutput) -> str:
    names = {
        "problem_definition": "문제 정의",
        "instruction_quality": "지시 품질",
        "verification": "결과 검증",
        "security": "보안 준수",
        "quality": "결과물 품질",
    }
    states = {
        "observed": "채점",
        "unobserved": "미관측",
        "insufficient_data": "자료 부족",
        "needs_review": "검토 필요",
    }
    coaching = {entry.criterion_id: entry.action for entry in result.coaching}
    lines = [f"평가 실행: {result.run_id}", result.summary, ""]
    for item in result.items:
        name = names[item.criterion_id.rsplit(".", 1)[1]]
        score = f"{item.score}/2점" if item.score is not None else "점수 없음"
        lines.extend(
            [
                f"{name}: {score} · {states[item.observation]}",
                f"판단: {item.reason}",
                "업무 근거: " + (", ".join(e.source_id for e in item.work_evidence) or "없음"),
                "기준 근거: "
                + ", ".join(
                    f"{e.document_id}@{e.version}#{e.section_id}" for e in item.rule_evidence
                ),
                f"코칭: {coaching[item.criterion_id]}",
                "담당자 검토 필요" if item.review_required else "",
                "",
            ]
        )
    return "\n".join(lines).strip()
