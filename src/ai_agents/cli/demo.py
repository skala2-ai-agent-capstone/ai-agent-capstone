"""API 비용 없이 모듈 간 데이터 계약을 확인하는 고정 응답 데모. 실제 평가 기능이 아닙니다."""

import json
import sys
from pathlib import Path

from pydantic import Field

from ..classification.agent import ModelReply, classify_work
from ..orchestration.approval import ReviewWorkflow, WorkflowError
from ..shared.models import ClassificationResult, StrictModel, WorkContext
from ..shared.workflow_models import (
    Coaching,
    CriteriaDraft,
    EvaluationInput,
    EvaluationOutput,
    ItemEvaluation,
    NonEmpty,
    content_hash,
)


class DemoFixture(StrictModel):
    work_record: WorkContext
    classification: ClassificationResult
    drafts: list[CriteriaDraft] = Field(min_length=1)
    items: list[ItemEvaluation] = Field(min_length=1)
    coaching: list[Coaching]
    summary: NonEmpty


class DemoClassifier:
    model = "fixture-only-no-llm"

    def __init__(self, result: ClassificationResult):
        self.result = result

    async def classify(self, *, system_prompt: str, payload: str) -> ModelReply:
        return ModelReply(result=self.result)


class DemoPartner:
    def __init__(self, fixture: DemoFixture):
        self.fixture = fixture
        self.calls = 0

    async def prepare_criteria(
        self, *, work_record: WorkContext, classification: ClassificationResult,
        previous: CriteriaDraft | None, feedback: str | None,
    ) -> CriteriaDraft:
        if self.calls >= len(self.fixture.drafts):
            raise WorkflowError("데모의 준비된 초안을 모두 사용했습니다. 다시 실행하세요.")
        draft = self.fixture.drafts[self.calls].model_copy(deep=True)
        self.calls += 1
        return draft

    async def evaluate(self, request: EvaluationInput) -> EvaluationOutput:
        return EvaluationOutput(
            run_id=request.run_id,
            criteria_sha256=request.approval.criteria_sha256,
            items=self.fixture.items,
            coaching=self.fixture.coaching,
            summary=self.fixture.summary,
        )


async def run_demo(path: Path) -> str:
    with path.open("rb") as stream:
        raw = stream.read(1_000_001)
    if len(raw) > 1_000_000:
        raise WorkflowError("데모 입력 파일은 1MB 이하여야 합니다.")
    fixture = DemoFixture.model_validate_json(raw)
    print(
        "[DEMO] LLM/MCP/DB 호출 없음. 분류·기준·평가·코칭은 고정 예시입니다.\n"
        "수정 요청은 실제 생성 대신 준비된 다음 초안으로 전환합니다.",
        file=sys.stderr,
    )
    classification = await classify_work(
        fixture.work_record, DemoClassifier(fixture.classification)
    )
    workflow = ReviewWorkflow(fixture.work_record, classification, DemoPartner(fixture))
    print(classification.classification.model_dump_json(indent=2), file=sys.stderr)
    draft = await workflow.prepare()
    while True:
        print(f"\n[평가기준 초안 {workflow.revision}]", file=sys.stderr)
        print(draft.model_dump_json(indent=2), file=sys.stderr)
        shown_hash = content_hash(draft)
        print("승인 / 수정 / 취소 중 입력하세요: ", end="", file=sys.stderr, flush=True)
        try:
            choice = input().strip()
        except EOFError:
            choice = "취소"
        if choice == "승인":
            workflow.approve(displayed_hash=shown_hash)
            result = await workflow.evaluate()
            return json.dumps({
                "mode": "demo", "state": workflow.state,
                "result": result.model_dump(mode="json"),
            }, ensure_ascii=False, indent=2)
        if choice == "취소":
            workflow.cancel()
            return json.dumps({"mode": "demo", "state": workflow.state})
        if choice == "수정":
            print("수정 요청: ", end="", file=sys.stderr, flush=True)
            try:
                feedback = input().strip()
            except EOFError:
                workflow.cancel()
                return json.dumps({"mode": "demo", "state": workflow.state})
            if not feedback:
                print("수정 내용을 입력해야 합니다.", file=sys.stderr)
                continue
            draft = await workflow.prepare(feedback=feedback)
        else:
            print("승인, 수정, 취소 중 하나를 정확히 입력하세요.", file=sys.stderr)
