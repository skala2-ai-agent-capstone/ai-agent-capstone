"""팀 통계/문장은 코드로 계산한다. LLM에는 공개 가능한 지표와 코칭 선택만 맡긴다."""

from ...shared.team_contracts import (
    CRITERIA,
    TeamDecision,
    TeamInsight,
    TeamMetric,
    TeamReport,
    TeamSnapshot,
)
from ...shared.workflow_models import content_hash

ACTIONS = {
    "define_acceptance": (
        "problem_definition", "업무 시작 전에 목표와 완료 조건을 함께 작성하세요."),
    "clarify_constraints": ("problem_definition", "입력 자료·제약·확인할 사항을 먼저 정리하세요."),
    "structure_prompt": (
        "instruction_quality", "요청에 목적·입력·출력 형식·제약을 구분해 적으세요."),
    "request_evidence": (
        "instruction_quality", "답변에 근거와 불확실한 부분을 함께 표시하도록 요청하세요."),
    "cross_check": ("verification", "AI 답변의 수치와 출처를 원자료와 대조하는 단계를 두세요."),
    "review_assumptions": ("verification", "확인된 사실과 AI의 가정을 나누고 가정을 검토하세요."),
    "mask_before_input": (
        "security", "AI 입력 전에 민감정보 포함 여부와 마스킹 상태를 확인하세요."),
    "check_sharing_scope": ("security", "자료별 사용·공유 허용 범위를 확인하는 절차를 마련하세요."),
    "use_review_checklist": (
        "output_quality", "제출 전에 요구사항별 완료 여부를 체크리스트로 점검하세요."),
    "validate_deliverable": (
        "output_quality", "산출물의 수치·형식·누락 사항을 원자료와 대조하세요."),
}
WORK_LABELS = {
    "rfp_analysis": "RFP 분석", "proposal": "제안서", "quote": "견적서",
    "inquiry_reply": "고객 질의 답변",
}


def metrics_for(snapshot: TeamSnapshot) -> list[TeamMetric]:
    snapshot = TeamSnapshot.model_validate(snapshot.model_dump(mode="json"))
    metrics = []
    groups = sorted(snapshot.groups, key=lambda g: (
        g.work_type, g.rubric_version, g.rubric_sha256,
    ))
    for index, group in enumerate(groups, 1):
        for cell in sorted(group.cells, key=lambda c: c.criterion):
            stats = cell.statistics
            if stats is None:
                continue
            metrics.append(TeamMetric(
                metric_id=f"g{index}.{cell.criterion}", work_type=group.work_type,
                rubric_version=group.rubric_version, rubric_sha256=group.rubric_sha256,
                criterion=cell.criterion, score_sum=stats.score_sum,
                scored_evaluations=stats.scored_evaluations,
                finalized_evaluations=group.finalized_evaluations,
                mean_score=round(stats.score_sum / stats.scored_evaluations, 3),
                coverage_percent=round(
                    100 * stats.scored_evaluations / group.finalized_evaluations, 1,
                ),
                band="strength" if stats.score_sum * 2 >= stats.scored_evaluations * 3
                else "improvement",
            ))
    return metrics


def validate_decision(decision, metrics):
    decision = TeamDecision.model_validate(decision.model_dump(mode="json"))
    by_id = {m.metric_id: m for m in metrics}
    strength_ids = decision.strengths
    improvement_ids = [row.metric_id for row in decision.improvements]
    selected = strength_ids + improvement_ids
    if len(set(selected)) != len(selected) or any(k not in by_id for k in selected):
        raise ValueError("duplicate or unknown metric")
    for band, ids in (("strength", strength_ids), ("improvement", improvement_ids)):
        if any(by_id[k].band != band for k in ids):
            raise ValueError("claim contradicts calculated band")
        if any(m.band == band for m in metrics) and not ids:
            raise ValueError("available band requires at least one selection")
    for row in decision.improvements:
        if ACTIONS[row.action_id][0] != by_id[row.metric_id].criterion:
            raise ValueError("action does not match criterion")
    return decision


def build_report(snapshot: TeamSnapshot, decision: TeamDecision) -> TeamReport:
    snapshot = TeamSnapshot.model_validate(snapshot.model_dump(mode="json"))
    metrics = metrics_for(snapshot)
    decision = validate_decision(decision, metrics)
    by_id = {m.metric_id: m for m in metrics}

    def insight(key, action=None):
        metric = by_id[key]
        observation = (
            f"{WORK_LABELS[metric.work_type]} · {key.split('.')[0]} "
            f"(기준 버전 {metric.rubric_version})의 {CRITERIA[metric.criterion]}: "
            f"공개된 관측 평가 {metric.scored_evaluations}건의 평균은 "
            f"{metric.mean_score:.3f}/2점입니다. "
            f"해당 기준 그룹 확정 평가 대비 관측 비율은 {metric.coverage_percent:.1f}%입니다."
        )
        return TeamInsight(metric_id=key, observation=observation, action_id=action,
                           recommendation=ACTIONS[action][1] if action else None)

    status = "generated" if metrics else (
        "insufficient_data" if snapshot.release_approved else "withheld"
    )
    summary = (
        f"공개 가능한 항목별 집계 {len(metrics)}개를 바탕으로 팀 업무 개선 활동을 제안합니다."
        if metrics else "공개 가능한 집계 근거가 없어 팀 평가와 코칭을 보류합니다."
    )
    return TeamReport(
        request_id=snapshot.request_id, team_id=snapshot.team_id, snapshot_id=snapshot.snapshot_id,
        snapshot_sha256=content_hash(snapshot),
        period_start=snapshot.period_start, period_end=snapshot.period_end, status=status,
        summary=summary, metrics=metrics,
        strengths=[insight(k) for k in decision.strengths],
        improvements=[insight(row.metric_id, row.action_id) for row in decision.improvements],
        limitations=[
            "개인의 역량·순위·인사평가를 나타내지 않습니다.",
            "업무 유형과 평가 기준 버전별로 분리하며 팀 전체 단일 점수는 산출하지 않습니다.",
            "평균은 공개된 관측 평가 건수로 가중합니다. 구성원별 동일 가중 평균이 아닙니다.",
            "미관측·검토 필요·공개 제한 항목은 0점으로 대체하지 않습니다.",
            "1.5/2 이상 강점 후보, 미만 개선 후보는 코칭 선택용 임시 기준입니다.",
            "공개 제한과 관측 누락으로 전체 팀 업무를 대표하지 않을 수 있습니다.",
        ],
    )
