"""MCP 응답 검증과 평가 입력 변환. 연결·인증은 호출자가 제공한다."""

import json
from typing import Literal, Protocol

from pydantic import ConfigDict, Field, JsonValue, TypeAdapter

from ..shared.evidence_models import (
    ArtifactRecord,
    ArtifactReference,
    CustomerSourceMessage,
    EvaluationEvidence,
    EvaluationRun,
    PolicyCheckResult,
    RegistrationRecord,
    WorkCaseEvidence,
)
from ..shared.models import NonEmpty, StrictModel


class MCPMeta(StrictModel):
    """조회 결과가 어느 실행과 스냅샷에 속하는지."""

    model_config = ConfigDict(extra="allow")

    run_id: NonEmpty
    snapshot: dict[str, JsonValue]


class MCPSuccess(StrictModel):
    """성공 응답의 구조."""

    model_config = ConfigDict(str_strip_whitespace=False)

    ok: Literal[True]
    data: dict[str, JsonValue]
    meta: MCPMeta


class MCPErrorDetail(StrictModel):
    """실패 원인과 재시도 가능 여부."""

    code: NonEmpty
    message: NonEmpty
    retryable: bool = Field(strict=True)


class MCPFailure(StrictModel):
    """실패 응답의 구조."""

    ok: Literal[False]
    error: MCPErrorDetail
    meta: dict[str, JsonValue] | None = None


class MCPToolError(RuntimeError):
    """MCP 조회 실패를 호출한 쪽에 전달한다."""

    def __init__(self, failure: MCPFailure):
        self.code = failure.error.code
        self.retryable = failure.error.retryable
        self.meta = failure.meta

        super().__init__(f"{failure.error.code}: {failure.error.message}")


class BusinessToolCaller(Protocol):
    async def __call__(self, name: str, arguments: dict[str, JsonValue]) -> object:
        """인증된 business MCP 클라이언트의 도구 호출 함수를 전달한다."""
        ...


def decode_mcp_result(result: object) -> dict[str, JsonValue]:
    """SDK 결과의 구조화 데이터 또는 JSON 텍스트를 읽는다. 내용은 실행하지 않는다."""
    if getattr(result, "is_error", False) is True or getattr(result, "isError", False) is True:
        raise ValueError("MCP 프로토콜 수준의 도구 호출 오류입니다.")
    if isinstance(result, dict):
        value = result
    elif isinstance(result, str):
        value = json.loads(result)
    else:
        value = getattr(result, "structured_content", None)
        if value is None:
            value = getattr(result, "structuredContent", None)
        if value is None:
            blocks = getattr(result, "content", [])
            texts = [block.text for block in blocks if getattr(block, "type", None) == "text"]
            if len(texts) != 1:
                raise ValueError("MCP 결과에는 JSON 텍스트 한 개가 필요합니다.")
            value = json.loads(texts[0])
    if isinstance(value, dict) and set(value) == {"result"}:
        value = value["result"]
    return TypeAdapter(dict[str, JsonValue]).validate_python(value)


def _canonical(value: dict[str, JsonValue]) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


def read_mcp_response(
    response: dict[str, JsonValue],
    *,
    context: EvaluationRun | None = None,
) -> MCPSuccess:
    """실패 응답은 예외로 전달하고, 성공 응답은 검증한다."""

    if response.get("ok") is False:
        failure = MCPFailure.model_validate(response)
        raise MCPToolError(failure)

    if response.get("ok") is not True:
        raise ValueError("MCP 응답의 ok는 true 또는 false여야 합니다.")

    success = MCPSuccess.model_validate(response)
    if context is not None:
        if success.meta.run_id != context.run_id:
            raise ValueError("MCP 응답이 다른 평가 실행에 속합니다.")
        if _canonical(success.meta.snapshot) != _canonical(context.snapshot):
            raise ValueError("MCP 응답의 스냅샷이 이번 평가와 다릅니다.")
    return success


def parse_policy_check_response(
    response: dict[str, JsonValue],
    *,
    context: EvaluationRun,
) -> tuple[PolicyCheckResult, MCPMeta]:
    """성공 응답의 data를 정책 검사 모델로 변환한다."""

    success = read_mcp_response(response, context=context)

    result = PolicyCheckResult.model_validate(success.data)
    target = context.target
    checked = result.scope.artifact_ref
    if (
        result.work_id != context.work_id
        or result.version != target.version
        or result.evidence_id != f"{context.work_id}:policy_check"
        or (checked.artifact_id, checked.version) != (target.artifact_id, target.version)
    ):
        raise ValueError("정책 검사 결과의 업무 또는 평가 대상 버전이 다릅니다.")

    return result, success.meta


def parse_work_case_response(
    response: dict[str, JsonValue],
    *,
    context: EvaluationRun,
) -> tuple[WorkCaseEvidence, MCPMeta]:
    success = read_mcp_response(response, context=context)
    data = success.data
    required = {
        "work_id",
        "registration",
        "registration_evidence_id",
        "source_message",
        "artifact_refs",
    }
    if not required <= data.keys():
        raise ValueError("업무 응답에 필수 근거 필드가 없습니다.")
    if data["work_id"] != context.work_id:
        raise ValueError("다른 업무의 조회 결과입니다.")
    registration = None
    if data["registration"] is not None:
        if data["registration_evidence_id"] != f"{context.work_id}:registration":
            raise ValueError("등록 내용의 근거 ID가 올바르지 않습니다.")
        registration = RegistrationRecord.model_validate(
            {
                "evidence_id": data["registration_evidence_id"],
                "content": data["registration"],
            }
        )
    elif data["registration_evidence_id"] is not None:
        raise ValueError("등록 기록이 없는데 근거 ID가 존재합니다.")
    message = (
        CustomerSourceMessage.model_validate(data["source_message"])
        if data["source_message"] is not None
        else None
    )
    result = WorkCaseEvidence(
        work_id=context.work_id,
        registration=registration,
        source_message=message,
        artifact_refs=TypeAdapter(list[ArtifactReference]).validate_python(data["artifact_refs"]),
        source_documents=(
            TypeAdapter(list[dict[str, JsonValue]]).validate_python(data["source_documents"])
            if "source_documents" in data
            else None
        ),
    )
    # work_type·정답·이벤트·회사 정보 등은 평가 모듈의 근거 데이터로 복사하지 않는다.
    return result, success.meta


def parse_artifact_response(
    response: dict[str, JsonValue],
    *,
    context: EvaluationRun,
    reference: ArtifactReference,
) -> tuple[ArtifactRecord, MCPMeta]:
    success = read_mcp_response(response, context=context)
    result = ArtifactRecord.model_validate(success.data)
    if result.work_id != context.work_id or (result.artifact_id, result.version) != (
        reference.artifact_id,
        reference.version,
    ):
        raise ValueError("요청과 다른 업무·산출물·버전이 반환되었습니다.")
    if result.saved_at.utcoffset() is None or result.saved_at > context.target.saved_at:
        raise ValueError("산출물은 스냅샷 이후 버전이거나 저장 시각에 시간대가 없습니다.")
    if (result.author, result.saved_at, result.is_evaluation_target) != (
        reference.author,
        reference.saved_at,
        reference.is_evaluation_target,
    ):
        raise ValueError("산출물 본문과 업무 조회의 참조 정보가 다릅니다.")
    return result, success.meta


def assemble_evaluation_evidence(
    *,
    context: EvaluationRun,
    work_case_response: dict[str, JsonValue],
    artifact_responses: list[dict[str, JsonValue]],
    policy_check_response: dict[str, JsonValue] | None = None,
) -> EvaluationEvidence:
    """서버 응답들을 같은 실행의 근거 묶음으로 변환한다. 조회 오류는 숨기지 않는다."""
    context = EvaluationRun.model_validate(context.model_dump(mode="json"))
    work_case, _ = parse_work_case_response(work_case_response, context=context)
    refs = {(ref.artifact_id, ref.version): ref for ref in work_case.artifact_refs}
    artifacts = []
    for response in artifact_responses:
        success = read_mcp_response(response, context=context)
        record = ArtifactRecord.model_validate(success.data)
        key = (record.artifact_id, record.version)
        if key not in refs:
            raise ValueError("업무의 허용 목록에 없는 산출물 버전입니다.")
        artifact, _ = parse_artifact_response(response, context=context, reference=refs[key])
        artifacts.append(artifact)
    policy = None
    if policy_check_response is not None:
        policy, _ = parse_policy_check_response(policy_check_response, context=context)
    return EvaluationEvidence(
        context=context,
        work_case=work_case,
        artifacts=artifacts,
        policy_check=policy,
    )


async def collect_evaluation_evidence(
    *,
    context: EvaluationRun,
    call_tool: BusinessToolCaller,
) -> EvaluationEvidence:
    """분류·승인 모듈의 인증된 도구 호출 함수를 사용한다. 저장 도구와 LLM은 호출하지 않는다."""
    context = EvaluationRun.model_validate(context.model_dump(mode="json"))
    work_response = decode_mcp_result(
        await call_tool(
            "get_work_case",
            {"work_id": context.work_id},
        )
    )
    work_case, _ = parse_work_case_response(work_response, context=context)
    artifact_responses = []
    for ref in work_case.artifact_refs:
        artifact_responses.append(
            decode_mcp_result(
                await call_tool(
                    "get_artifact_version",
                    {"artifact_id": ref.artifact_id, "version": ref.version},
                )
            )
        )
    policy_response = decode_mcp_result(
        await call_tool(
            "get_policy_check_result",
            {"work_id": context.work_id, "version": context.target.version},
        )
    )
    return assemble_evaluation_evidence(
        context=context,
        work_case_response=work_response,
        artifact_responses=artifact_responses,
        policy_check_response=policy_response,
    )
