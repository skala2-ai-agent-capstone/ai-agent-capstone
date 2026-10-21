"""메모리에서만 진행하는 A의 승인 흐름. DB/MCP/평가 LLM 구현은 포함하지 않습니다."""

from datetime import datetime, timezone
from typing import Protocol
from uuid import uuid4

from .classifier import validate_evidence
from .models import ClassificationReport, ClassificationResult, WorkContext
from .workflow_models import (
    Approval,
    CriteriaDraft,
    EvaluationInput,
    EvaluationOutput,
    StrictModel,
    content_hash,
)


class WorkflowError(ValueError):
    pass


class EvaluationPartner(Protocol):
    """B가 구현할 계약. prepare_criteria는 실행마다 호출하며 수정 시 이전 초안을 전달합니다."""

    async def prepare_criteria(
        self, *, work_record: WorkContext, classification: ClassificationResult,
        previous: CriteriaDraft | None, feedback: str | None,
    ) -> CriteriaDraft: ...

    async def evaluate(self, request: EvaluationInput) -> EvaluationOutput: ...


class WorkflowResult(StrictModel):
    # 승인한 원문과 출처까지 리포트에 포함합니다. 결과의 DB 저장은 하지 않습니다.
    evaluation_input: EvaluationInput
    report: EvaluationOutput


def validate_report(
    report: EvaluationOutput, request: EvaluationInput,
    *, additional_sources: dict[str, str] | None = None,
) -> None:
    """구조·인용 일치 검증. 근거의 의미상 타당성 검증은 B의 별도 책임입니다."""
    if report.run_id != request.run_id or report.criteria_sha256 != content_hash(
        request.approved_criteria
    ):
        raise WorkflowError("평가 결과가 현재 실행 또는 승인한 기준과 다릅니다.")
    criteria = {item.id: item for item in request.approved_criteria.items}
    ids = [item.criterion_id for item in report.items]
    if len(ids) != len(set(ids)) or set(ids) != set(criteria):
        raise WorkflowError("평가 항목이 누락·중복되었거나 승인 기준에 없는 항목입니다.")
    sources = dict(additional_sources or {})
    sources.update(request.work_record.evidence_sources())
    for item in report.items:
        criterion = criteria[item.criterion_id]
        if item.score is not None and item.score not in {s.score for s in criterion.levels}:
            raise WorkflowError("승인된 점수 조건에 없는 점수입니다.")
        for evidence in item.work_evidence:
            if (evidence.source_id not in sources
                    or evidence.quote not in sources[evidence.source_id]):
                raise WorkflowError("평가의 업무 근거 ID 또는 인용문이 원문과 다릅니다.")
        for evidence in item.rule_evidence:
            if not any(
                evidence.key() == approved.key() and evidence.quote in approved.quote
                for approved in criterion.rule_evidence
            ):
                raise WorkflowError("평가의 기준 근거가 해당 승인 항목의 근거와 다릅니다.")
    if any(coaching.criterion_id not in criteria for coaching in report.coaching):
        raise WorkflowError("코칭이 승인 기준에 없는 항목을 참조합니다.")


class ReviewWorkflow:
    def __init__(
        self, context: WorkContext, classification: ClassificationReport,
        partner: EvaluationPartner,
    ):
        self._context = WorkContext.model_validate(context.model_dump())
        classification = ClassificationReport.model_validate(classification.model_dump())
        if (classification.work_id != context.work_id
                or classification.input_sha256 != content_hash(self._context)):
            raise WorkflowError("분류한 업무 기록과 평가 대상이 다릅니다.")
        validate_evidence(classification.classification, self._context)
        self._classification = classification.classification
        self._partner = partner
        self.run_id = str(uuid4())
        self._draft: CriteriaDraft | None = None
        self._revision = 0
        self._request: EvaluationInput | None = None
        self._state = "created"

    @property
    def state(self) -> str:
        return self._state

    @property
    def revision(self) -> int:
        return self._revision

    @property
    def draft(self) -> CriteriaDraft:
        if self._draft is None:
            raise WorkflowError("평가기준 초안을 먼저 생성하세요.")
        return self._draft.model_copy(deep=True)

    @property
    def approved_input(self) -> EvaluationInput:
        """승인 후 B에 전달할 전체 입력의 복사본. 기준만 승인하고 연결을 기다릴 때도 사용."""
        if self._state != "approved" or self._request is None:
            raise WorkflowError("현재 초안에 대한 담당자 승인이 필요합니다.")
        return EvaluationInput.model_validate(self._request.model_dump())

    async def prepare(self, feedback: str | None = None) -> CriteriaDraft:
        if self._state not in {"created", "awaiting_approval", "approved"}:
            raise WorkflowError("현재 상태에서는 기준을 생성·수정할 수 없습니다.")
        if self._classification.status != "classified":
            raise WorkflowError(
                "단일 업무로 확정되지 않아 평가를 보류합니다. "
                "복합 업무는 대상 분리, 판단 보류는 추가 정보 확인이 필요합니다."
            )
        if self._draft is not None and not (feedback and feedback.strip()):
            raise WorkflowError("기준을 수정하려면 수정 요청을 입력하세요.")
        # 수정 요청 즉시 이전 승인을 무효화합니다. 생성 실패 시에도 이전 승인으로 평가하지 않습니다.
        self._request = None
        self._state = "preparing"
        try:
            draft = await self._partner.prepare_criteria(
                work_record=self._context.model_copy(deep=True),
                classification=self._classification.model_copy(deep=True),
                previous=self._draft.model_copy(deep=True) if self._draft else None,
                feedback=feedback,
            )
            draft = CriteriaDraft.model_validate(draft.model_dump())
            if draft.work_type != self._classification.work_type:
                raise WorkflowError("생성된 기준과 분류 결과의 업무 유형이 다릅니다.")
        except Exception:
            # 실패한 수정 전 초안을 승인할 수 없도록 초기 상태로 되돌립니다.
            self._draft = None
            self._state = "created"
            raise
        self._draft = draft
        self._revision += 1
        self._state = "awaiting_approval"
        return self.draft

    def approve(self, *, displayed_hash: str) -> None:
        """A의 사용자 입력 처리부만 호출합니다. 모델/도구로 공개하지 않습니다."""
        if self._state != "awaiting_approval" or self._draft is None:
            raise WorkflowError("승인을 기다리는 초안이 없습니다.")
        if displayed_hash != content_hash(self._draft):
            raise WorkflowError("표시된 초안이 변경되었습니다. 새 초안을 확인하고 승인하세요.")
        self._request = EvaluationInput(
            run_id=self.run_id,
            work_record=self._context.model_copy(deep=True),
            classification=self._classification.model_copy(deep=True),
            approved_criteria=self.draft,
            approval=Approval(
                status="approved", run_id=self.run_id,
                work_sha256=content_hash(self._context), criteria_sha256=displayed_hash,
                revision=self.revision, approved_at=datetime.now(timezone.utc),
            ),
        )
        self._state = "approved"

    def cancel(self) -> None:
        if self._state not in {"created", "awaiting_approval", "approved"}:
            raise WorkflowError("현재 상태에서는 취소할 수 없습니다.")
        self._request = None
        self._state = "cancelled"

    async def evaluate(self) -> WorkflowResult:
        if self._state != "approved" or self._request is None:
            raise WorkflowError("담당자가 현재 기준을 승인한 뒤에만 평가할 수 있습니다.")
        request = EvaluationInput.model_validate(self._request.model_dump())
        self._state = "evaluating"
        try:
            output = await self._partner.evaluate(request.model_copy(deep=True))
            output = EvaluationOutput.model_validate(output.model_dump())
            validate_report(output, request)
        except Exception:
            self._state = "failed"
            raise
        self._state = "completed"
        return WorkflowResult(evaluation_input=request, report=output)
