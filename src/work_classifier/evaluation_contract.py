"""평가 출력 스키마와 최종 검증이 함께 사용하는 항목별 근거·점수 규칙."""

ITEM_KEYS = ("problem_definition", "instruction_quality", "verification", "security", "quality")

REQUIRED_WORK_KINDS = {
    "problem_definition": frozenset({
        "message:user", "registration", "evidence_link", "approval_request",
    }),
    "instruction_quality": frozenset({
        "message:user", "registration", "evidence_link", "approval_request",
    }),
    "verification": frozenset({"message:user", "evidence_link"}),
    "security": frozenset({"policy_check"}),
    "quality": frozenset({"target_artifact"}),
}

MISSING_WORK_MESSAGES = {
    "problem_definition": "직원 행동 평가에 직원의 실제 행동 근거가 없습니다.",
    "instruction_quality": "직원 행동 평가에 직원의 실제 행동 근거가 없습니다.",
    "verification": "직원 행동 평가에 직원의 실제 행동 근거가 없습니다.",
    "security": "보안 점수에는 서버 정책 검사 근거가 필요합니다.",
    "quality": "결과물 품질 평가에 평가 대상 산출물의 근거가 없습니다.",
}


def eligible_work_sources(key, source_kinds, work_id, *, policy_available):
    """같은 검증 규칙으로 필수 근거 후보를 정한다. 추가 비교 근거는 별도 허용한다."""
    result = set()
    for source_id, kind in source_kinds.items():
        if kind not in REQUIRED_WORK_KINDS[key]:
            continue
        if key == "security" and (
            not policy_available or source_id != f"{work_id}:policy_check"
        ):
            continue
        result.add(source_id)
    return result


def score_issue(key, score, work_type, *, policy_violations, reference_quote_available):
    if key == "security" and policy_violations and score != 0:
        return "정책 위반이 탐지된 업무에 보안 준수 점수를 줄 수 없습니다."
    if (work_type == "quotation" and key in {"quality", "verification"}
            and score == 2 and not reference_quote_available):
        return "견적의 완전한 검증·품질 점수에는 기준 견적 조회가 필요합니다."
    return None
