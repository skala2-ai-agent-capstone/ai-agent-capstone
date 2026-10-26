"""평가 Agent. 승인 입력 → MCP 근거 → GPT → 원문 검증 → 결과 반환."""

import asyncio
import json
from types import SimpleNamespace

from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI
from pydantic import ValidationError

from ...integrations.mcp_adapter import (
    BusinessToolCaller,
    MCPToolError,
    collect_evaluation_evidence,
    decode_mcp_result,
    read_mcp_response,
)
from ...observability.tracing import trace_span
from ...shared.evidence_models import EvaluationEvidence, EvaluationRun
from ...shared.llm_trace import trace_headers
from ...shared.workflow_models import EvaluationInput
from .contract import MISSING_WORK_MESSAGES
from .materials import EvaluationMaterials, collect_evaluation_materials
from .models import AgentEvaluationOutput
from .payload import build_evaluation_payload, compact_evaluation_payload
from .prompts import SYSTEM_PROMPT
from .selection import (
    SELECTION_VERSION,
    build_choices,
    choice_requirements,
    resolve_selection,
    security_choice_ids,
    selection_model,
)
from .stored_rubric import checked_input
from .validation import (
    EvaluationError,
    evidence_catalog,
    reference_catalog,
    validate_criteria,
    validate_evaluation_result,
    validate_materials,
)

MAX_INPUT_CHARS = 180_000


def validation_feedback(error: ValidationError) -> str:
    """검증 이유만 전달한다. 모델 응답·인용·민감한 입력 값은 포함하지 않는다."""
    known_messages = {
        "관측된 항목에는 점수와 업무 근거가 필요합니다.",
        "미관측·자료 부족 항목에는 점수를 부여할 수 없습니다.",
        "검토 필요 항목은 review_required가 true여야 합니다.",
        "검토 필요 판단에는 실제 업무 근거가 필요합니다.",
        "검토 필요 항목에는 상충·모호함을 보여 주는 이 항목의 업무 근거가 필요합니다.",
        "보안 준수 항목에는 1점을 줄 수 없습니다.",
        "평가 항목 ID가 중복되었습니다.",
        "점수는 정수 0·1·2 또는 null이어야 합니다.",
        "평가 항목은 객체여야 합니다.",
        "제공된 항목·근거 선택 ID 중에서 선택해야 합니다.",
        "허용된 관측 상태를 선택해야 합니다.",
        "승인된 기준에서 허용하지 않는 점수입니다.",
        "선택 ID·필수 필드·자료형이 평가 응답 형식과 맞지 않습니다.",
        "서버 정책 검사 근거가 없어 보안 항목을 채점할 수 없습니다.",
        "보안 채점의 업무 근거는 서버 정책 검사 구간에서 선택해야 합니다.",
        "해당 항목의 승인된 기준 근거를 선택해야 합니다.",
        "정책 위반이 탐지된 업무에 보안 준수 점수를 줄 수 없습니다.",
        "견적의 완전한 검증·품질 점수에는 기준 견적 조회가 필요합니다.",
    } | set(MISSING_WORK_MESSAGES.values())
    reasons = {
        "enum": "제공된 항목·근거 선택 ID 중에서 선택해야 합니다.",
        "literal_error": "허용된 관측 상태를 선택해야 합니다.",
        "int_type": "점수는 정수 0·1·2 또는 null이어야 합니다.",
        "greater_than_equal": "점수는 0 이상이어야 합니다.",
        "less_than_equal": "점수는 2 이하여야 합니다.",
        "missing": "필수 필드가 누락됐습니다.",
        "extra_forbidden": "정의되지 않은 필드가 포함됐습니다.",
        "too_short": "필수 항목·근거의 개수가 부족합니다.",
        "too_long": "항목·근거의 개수가 허용 한도를 초과했습니다.",
        "string_too_short": "빈 문자열을 사용할 수 없습니다.",
        "bool_type": "검토 여부는 true 또는 false여야 합니다.",
    }
    details = []
    for entry in error.errors(include_input=False, include_url=False)[:3]:
        # 예상 밖 필드 이름에도 사용자 입력이 들어갈 수 있어 알려진 경로만 표시한다.
        fields = {
            "items", "coaching", "criterion_id", "observation", "score", "reason",
            "work_evidence_ids", "rule_evidence_ids", "review_required", "action",
            "required_work_evidence_id", "required_rule_evidence_id",
        }
        path = ".".join(
            str(part) if isinstance(part, int) or part in fields else "unknown_field"
            for part in entry["loc"]
        ) or "결과 전체"
        message = entry["msg"].removeprefix("Value error, ")
        reason = message if message in known_messages else reasons.get(
            entry["type"], "평가 결과의 값이 스키마 규칙과 맞지 않습니다."
        )
        details.append(f"{path}: {reason}")
        if entry["type"] == "evaluation_selection":
            diagnostic = entry.get("ctx", {}).get("diagnostic")
            if diagnostic:
                details.append("항목 진단=" + json.dumps(diagnostic, ensure_ascii=False))
    return "평가 결과의 형식·점수·선택 ID가 잘못되었습니다: " + "; ".join(details)


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
        api_mode: str = "responses",
        extra_headers: dict | None = None,
    ):
        if not 0 <= max_validation_retries <= 2:
            raise ValueError("평가 검증 재시도는 0~2회로 제한합니다.")
        self.client, self.context = client, context
        self.business_call, self.rag_call = business_call, rag_call
        self.model, self.max_validation_retries = model, max_validation_retries
        if api_mode not in {"responses", "chat"}:
            raise ValueError("지원하지 않는 평가 모델 API입니다.")
        self.api_mode, self.extra_headers = api_mode, extra_headers

    async def evaluate(self, request: EvaluationInput) -> AgentEvaluationOutput:
        request = checked_input(request)
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
        """이미 수집한 자료를 사용하는 경계. 테스트·분류·승인 모듈의 수집 노드 재사용용."""
        request = checked_input(request)
        evidence = EvaluationEvidence.model_validate(evidence.model_dump(mode="json"))
        materials = EvaluationMaterials.model_validate(materials.model_dump(mode="json"))
        validate_criteria(request)
        validate_materials(request, evidence, materials)
        if self.context.model_dump(mode="json") != evidence.context.model_dump(mode="json"):
            raise EvaluationError("평가기와 조회 자료의 실행 범위가 다릅니다.")
        payload = build_evaluation_payload(request, evidence=evidence)
        sources, kinds = evidence_catalog(request, evidence, materials)
        refs = reference_catalog(request, materials)
        choices = build_choices(request, sources, kinds, refs, evidence=evidence)
        payload["work_evidence_catalog"] = choices.work
        payload["reference_catalog"] = choices.rules
        payload["approved_rule_choices"] = choices.approved_rule_ids
        payload["criterion_requirements"] = choice_requirements(choices, request)
        payload["security_evidence"] = {
            "available": evidence.policy_check is not None,
            "policy_check_choice_ids": security_choice_ids(choices, request.work_record.work_id),
            "violations_detected": bool(evidence.policy_check.violations)
            if evidence.policy_check is not None else None,
        }
        payload["evidence_selection_version"] = SELECTION_VERSION
        response_format = selection_model(choices, request)
        payload["additional_details"] = materials.details
        payload["missing_sources"] = sorted(
            set(payload["missing_sources"] + materials.missing_sources)
        )
        encoded = json.dumps(compact_evaluation_payload(payload), ensure_ascii=False)
        if len(encoded) > MAX_INPUT_CHARS:
            raise EvaluationError("평가 입력이 크기 제한을 초과했습니다. 조회 범위를 줄여주세요.")
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": encoded},
        ]
        for attempt in range(self.max_validation_retries + 1):
            try:
                response = await self.generate(messages, response_format)
            except (APITimeoutError, APIConnectionError) as exc:
                raise EvaluationError("평가 API 연결 또는 시간 초과 오류입니다.") from exc
            except APIStatusError as exc:
                raise EvaluationError(f"평가 API 오류(HTTP {exc.status_code}).") from exc
            except ValidationError as exc:
                issue = validation_feedback(exc)
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
                    result = resolve_selection(
                        response.output_parsed.model_dump(mode="json"), choices, request
                    )
                    return validate_evaluation_result(
                        request,
                        result,
                        evidence=evidence,
                        materials=materials,
                    )
                except ValidationError as exc:
                    issue = validation_feedback(exc)
                except EvaluationError as exc:
                    issue = str(exc)
                if attempt < self.max_validation_retries:
                    messages.append({
                        "role": "assistant", "content": response.output_parsed.model_dump_json(),
                    })
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

    async def generate(self, messages, response_format):
        # 호출부가 헤더를 직접 넘기지 않으면 업무 범위(work_scope)에서 정한다
        headers = {"extra_headers": self.extra_headers or trace_headers("evaluate")}
        if self.api_mode == "responses":
            return await self.client.responses.parse(
                model=self.model, input=messages, text_format=response_format,
                max_output_tokens=6000, temperature=0, store=False, **headers,
            )
        with trace_span("final.llm", inputs=messages, run_type="llm") as span:
            reply = await self.client.chat.completions.create(
                model=self.model, messages=messages, temperature=0, max_tokens=6000,
                response_format={"type": "json_schema", "json_schema": {
                    "name": "final_evaluation", "strict": True,
                    "schema": response_format.model_json_schema(),
                }}, **headers,
            )
            span.result(reply)
        if not reply.choices or reply.choices[0].finish_reason != "stop":
            raise EvaluationError("최종 평가 모델 응답이 완료되지 않았습니다.")
        message = reply.choices[0].message
        if getattr(message, "refusal", None) or not isinstance(message.content, str):
            raise EvaluationError("최종 평가 모델이 결과를 반환하지 않았습니다.")
        return SimpleNamespace(
            status="completed", output=[],
            output_parsed=response_format.model_validate_json(message.content),
        )


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
