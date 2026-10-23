"""사람 품질 검토를 실행 결과에 연결한다. 미검토·호출 실패를 품질 0점으로 합치지 않는다."""

import csv
from pathlib import Path
from statistics import mean, median
from typing import Literal, Self

from pydantic import AwareDatetime, Field, model_validator

from ..shared.models import NonEmpty, StrictModel
from .report import cell
from .runner import compare_experiments, digest, write_json

DIMENSIONS = ("grounding", "judgment", "actionability", "scope")
LABELS = {"grounding": "근거 타당성", "judgment": "판단 타당성",
          "actionability": "코칭 실행 가능성", "scope": "평가 범위 준수"}


class QualityRatings(StrictModel):
    grounding: int = Field(ge=0, le=2, strict=True)
    judgment: int = Field(ge=0, le=2, strict=True)
    actionability: int = Field(ge=0, le=2, strict=True)
    scope: int = Field(ge=0, le=2, strict=True)


class CaseReview(StrictModel):
    case_id: NonEmpty
    output_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["pending", "reviewed", "not_applicable"] = "pending"
    reviewer: NonEmpty | None = None
    reviewed_at: AwareDatetime | None = None
    ratings: QualityRatings | None = None
    notes: str = ""

    @model_validator(mode="after")
    def complete(self) -> Self:
        if self.status == "reviewed":
            if (not self.reviewer or self.reviewed_at is None
                    or self.ratings is None or not self.notes):
                raise ValueError("reviewed rows require reviewer, timestamp, ratings and notes")
        elif self.ratings is not None or self.reviewer is not None or self.reviewed_at is not None:
            raise ValueError("unreviewed rows cannot contain completed review fields")
        return self


class ReviewSheet(StrictModel):
    schema_version: Literal["1"] = "1"
    review_rubric_version: Literal["quality-review-v1"] = "quality-review-v1"
    experiment_id: NonEmpty
    dataset_sha256: NonEmpty
    experiment_sha256: NonEmpty
    cases: list[CaseReview] = Field(min_length=1)


def require_quality(experiment):
    settings = experiment.manifest.get("model_settings", {})
    if (not str(experiment.manifest.get("target", "")).startswith(
        "ai_agents.validation.quality:"
    ) or settings.get("execution_mode") not in {"real_model", "test_transport"}):
        raise ValueError("quality runner experiment required")
    compare_experiments(experiment, experiment)


def review_template(experiment):
    require_quality(experiment)
    return ReviewSheet(
        experiment_id=experiment.manifest["experiment_id"],
        dataset_sha256=experiment.manifest["dataset_sha256"],
        experiment_sha256=digest(experiment.model_dump(mode="json")),
        cases=[CaseReview(case_id=case.case_id, output_sha256=digest(case.actual),
                          status="not_applicable" if no_model_needed(case) else "pending")
               for case in experiment.cases],
    )


def generated(case):
    return (case.actual.get("status") == "ok"
            and case.actual.get("output", {}).get("status") == "generated")


def no_model_needed(case):
    return (generated(case)
            and case.actual["output"].get("metrics", {}).get("model_calls") == 0)


def checked_reviews(experiment, sheet=None):
    require_quality(experiment)
    if sheet is None:
        return review_template(experiment)
    sheet = ReviewSheet.model_validate(sheet.model_dump(mode="json"))
    expected = review_template(experiment)
    if (sheet.experiment_id, sheet.dataset_sha256, sheet.experiment_sha256) != (
        expected.experiment_id, expected.dataset_sha256, expected.experiment_sha256,
    ):
        raise ValueError("review belongs to a different experiment")
    by_id = {case.case_id: case for case in experiment.cases}
    if len(sheet.cases) != len(by_id) or {row.case_id for row in sheet.cases} != by_id.keys():
        raise ValueError("review case IDs must match exactly once")
    for row in sheet.cases:
        case = by_id[row.case_id]
        if row.output_sha256 != digest(case.actual):
            raise ValueError("reviewed output changed")
        if (row.status == "not_applicable") != no_model_needed(case):
            raise ValueError("call-free results are not model quality review subjects")
        if row.status == "reviewed" and not generated(case):
            raise ValueError("failed generations cannot receive semantic quality ratings")
    return sheet


def quality_summary(experiment, sheet=None):
    sheet = checked_reviews(experiment, sheet)
    reviewed = [row for row in sheet.cases if row.status == "reviewed"]
    successful = sum(generated(case) for case in experiment.cases)
    not_applicable = sum(no_model_needed(case) for case in experiment.cases)
    outputs = [case.actual.get("output", {}) for case in experiment.cases]
    metrics = [output.get("metrics", {}) for output in outputs]
    return {
        "experiment_id": experiment.manifest["experiment_id"],
        "execution_mode": experiment.manifest["model_settings"]["execution_mode"],
        "git_commit": experiment.manifest["git_commit"], "dirty": experiment.manifest["dirty"],
        "dataset_sha256": experiment.manifest["dataset_sha256"],
        "total": len(experiment.cases), "generated": successful,
        "failed": len(experiment.cases) - successful,
        "generation_success_rate": successful / len(experiment.cases),
        "reviewed": len(reviewed), "pending": len(sheet.cases) - len(reviewed) - not_applicable,
        "not_applicable": not_applicable,
        "review_coverage": len(reviewed) / (successful - not_applicable)
        if successful > not_applicable else None,
        "quality_mean": {
            key: mean(getattr(row.ratings, key) for row in reviewed) if reviewed else None
            for key in DIMENSIONS
        },
        "median_case_ms": median(case.duration_ms for case in experiment.cases),
        "model_calls": sum(m.get("model_calls", 0) for m in metrics),
        "tokens": {
            key: sum(m[key] for m in metrics)
            if all(m.get(key) is not None for m in metrics) else None
            for key in ("input_tokens", "output_tokens")
        },
        "token_usage_cases": sum(m.get("input_tokens") is not None
                                 and m.get("output_tokens") is not None for m in metrics),
        "langsmith_requested": experiment.manifest.get("langsmith_requested", False),
        "langsmith_export_failed": experiment.manifest.get("langsmith_export_failed"),
    }


def compare_quality(before, after, before_reviews=None, after_reviews=None, *, allow_changes=()):
    left, right = checked_reviews(before, before_reviews), checked_reviews(after, after_reviews)
    before_mode = before.manifest["model_settings"]["execution_mode"]
    if before_mode != after.manifest["model_settings"]["execution_mode"]:
        raise ValueError("real model and test transport runs cannot be compared")
    automatic = compare_experiments(before, after, allow_changes=allow_changes)
    left = {row.case_id: row for row in left.cases if row.status == "reviewed"}
    right = {row.case_id: row for row in right.cases if row.status == "reviewed"}
    paired = sorted(left.keys() & right.keys())
    return {
        "automatic_checks": automatic,
        "before": quality_summary(before, before_reviews),
        "after": quality_summary(after, after_reviews),
        "paired_reviewed_case_ids": paired,
        "paired_quality_mean": {
            key: {
                side: mean(getattr(rows[case].ratings, key) for case in paired) if paired else None
                for side, rows in (("before", left), ("after", right))
            } for key in DIMENSIONS
        },
        "different_reviewers": [case for case in paired
                                if left[case].reviewer != right[case].reviewer],
        "interpretation": "품질 변화는 양쪽 모두 사람이 검토한 동일 사례에서만 비교한다.",
    }


def export_quality_report(experiment, sheet, output_dir):
    sheet = checked_reviews(experiment, sheet)
    summary = quality_summary(experiment, sheet)
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=False)
    write_json(root / "summary.json", summary)
    lines = ["# LLM 평가 품질 검증 기록", "",
             "생성 성공률은 출력 형식·근거 검증 결과이며 사람 품질 점수와 다르다.", "",
             "| 실행 근거 | 값 |", "| --- | --- |"]
    for key in ("experiment_id", "execution_mode", "git_commit", "dirty", "dataset_sha256"):
        lines.append(f"| {key} | {cell(summary[key])} |")
    lines += ["", f"- 전체 {summary['total']}건 / 생성·검증 성공 {summary['generated']}건 / "
              f"실패 {summary['failed']}건", f"- 사람 검토 {summary['reviewed']}건 / "
              f"미검토 {summary['pending']}건 / 모델 미호출 {summary['not_applicable']}건",
              f"- 사례 처리 시간 중앙값: "
              f"{summary['median_case_ms']:.3f} ms (검증 재시도 포함)",
              f"- 모델 호출 수: {summary['model_calls']}",
              f"- 토큰 사용량 확인 가능 사례: {summary['token_usage_cases']}/{summary['total']}",
              "", "| 사람 검토 항목 | 평균 (0~2) |", "| --- | ---: |"]
    for key in DIMENSIONS:
        value = summary["quality_mean"][key]
        lines.append(f"| {LABELS[key]} | {value if value is not None else '미검토'} |")
    if summary["execution_mode"] == "test_transport":
        lines += ["", "**테스트 전송 대역 실행이다. 실제 LLM 품질·지연 시간 증거가 아니다.**"]
    if summary["dirty"]:
        lines += ["", "미커밋 변경 포함. 최종 증빙은 커밋 후 같은 평가셋으로 다시 실행한다."]
    lines += ["", "LangSmith 전송 요청 여부: " + str(summary["langsmith_requested"]),
              "원격 전송 실패 감지: " + str(summary["langsmith_export_failed"]),
              "원격 저장 성공은 LangSmith에서 trace ID로 별도 확인한다.", "",
              "상세 입력·기대 결과·실제 출력은 실험 폴더의 "
              "report.csv / experiment.json을 참조한다.",
              "이 품질 점수는 평가 Agent의 출력 검토 점수이며 직원 업무 점수가 아니다.", ""]
    (root / "report.md").write_text("\n".join(lines))
    by_id = {row.case_id: row for row in sheet.cases}
    with (root / "reviews.csv").open("x", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["case_id", "generation", "review", *DIMENSIONS, "reviewer", "notes",
                         "trace_id"])
        for case in experiment.cases:
            row = by_id[case.case_id]
            values = [case.case_id, "generated" if generated(case) else "failed", row.status,
                      *[getattr(row.ratings, k) if row.ratings else "" for k in DIMENSIONS],
                      row.reviewer or "", row.notes, case.trace_id]
            writer.writerow(["'" + v if isinstance(v, str) and v.startswith(("=", "+", "-", "@"))
                             else v for v in values])
    return summary
