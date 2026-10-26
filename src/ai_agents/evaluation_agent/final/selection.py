"""모델은 제공된 구간 ID만 선택하고, 공개 평가 결과의 인용은 원문으로 복원한다."""

from dataclasses import dataclass
from enum import Enum
from typing import Annotated, Literal, Union

from pydantic import BeforeValidator, Field, ValidationError, WrapValidator, create_model
from pydantic_core import PydanticCustomError

from ...shared.models import NonEmpty, StrictModel
from ...shared.workflow_models import content_hash
from .contract import MISSING_WORK_MESSAGES, eligible_work_sources, score_issue
from .models import AgentEvaluationOutput
from .validation import EvaluationError

SELECTION_VERSION = "evaluation-evidence-selection-v6-unscored-evidence"
MAX_CHOICES = 900
PIECE_LENGTH = 500


def pieces(text):
    offset = 0
    for line in text.splitlines(keepends=True):
        for start in range(0, len(line), PIECE_LENGTH):
            raw = line[start : start + PIECE_LENGTH]
            quote = raw.strip()
            if quote:
                begin = offset + start + len(raw) - len(raw.lstrip())
                yield quote, begin, begin + len(quote)
        offset += len(line)


@dataclass
class EvidenceChoices:
    work: list[dict]
    rules: list[dict]
    approved_rule_ids: dict[str, list[str]]
    policy_available: bool
    policy_violations: bool
    reference_quote_available: bool


def build_choices(request, sources, kinds, references, *, evidence) -> EvidenceChoices:
    work, rules, approved_ids = [], [], {}
    for source_id, text in sources.items():
        for quote, start, end in pieces(text):
            work.append(
                {
                    "id": f"w{len(work) + 1:04d}",
                    "source_id": source_id,
                    "kind": kinds[source_id],
                    "text": quote,
                    "start": start,
                    "end": end,
                }
            )
    seen = {}

    def add_rule(key, quote, start, end):
        signature = (*key, quote)
        if signature not in seen:
            choice_id = f"r{len(rules) + 1:04d}"
            rules.append(
                {
                    "id": choice_id,
                    "document_id": key[0],
                    "version": key[1],
                    "section_id": key[2],
                    "content": quote,
                    "start": start,
                    "end": end,
                }
            )
            seen[signature] = choice_id
        return seen[signature]

    for key, reference in references.items():
        for quote, start, end in pieces(reference.content):
            add_rule(key, quote, start, end)
    for criterion in request.approved_criteria.items:
        allowed = []
        for proof in criterion.rule_evidence:
            reference = references.get(proof.key())
            if reference is None or proof.quote not in reference.content:
                raise EvaluationError("승인된 기준 근거의 원문을 확인할 수 없습니다.")
            origin = reference.content.index(proof.quote)
            for quote, start, end in pieces(proof.quote):
                allowed.append(add_rule(proof.key(), quote, origin + start, origin + end))
        approved_ids[criterion.id] = list(dict.fromkeys(allowed))
    if len(work) + len(rules) > MAX_CHOICES:
        raise EvaluationError("평가 근거 구간이 900개를 초과했습니다. 조회 범위를 줄여주세요.")
    if not rules or any(not ids for ids in approved_ids.values()):
        raise EvaluationError("승인된 항목의 기준 근거 구간이 없습니다.")
    return EvidenceChoices(
        work, rules, approved_ids, evidence.policy_check is not None,
        bool(evidence.policy_check and evidence.policy_check.violations),
        f"{request.work_record.work_id}:reference_quote" in sources,
    )


def _strict_score(value):
    # Literal은 False/0.0도 0으로 받아들일 수 있으므로 원래 정수 제한을 유지한다.
    if type(value) is not int:
        raise ValueError("점수는 정수 0·1·2 또는 null이어야 합니다.")
    return value


def _strict_review(value):
    if type(value) is not bool:
        raise ValueError("검토 여부는 true 또는 false여야 합니다.")
    return value


def security_choice_ids(choices: EvidenceChoices, work_id: str) -> list[str]:
    eligible = eligible_work_sources(
        "security", {row["source_id"]: row["kind"] for row in choices.work}, work_id,
        policy_available=choices.policy_available,
    )
    return [
        row["id"] for row in choices.work
        if row["source_id"] in eligible
    ]


def choice_requirements(choices, request):
    kinds = {row["source_id"]: row["kind"] for row in choices.work}
    result = {}
    for criterion in request.approved_criteria.items:
        key = criterion.id.rsplit(".", 1)[1]
        eligible = eligible_work_sources(
            key, kinds, request.work_record.work_id, policy_available=choices.policy_available,
        )
        issues = {
            level.score: score_issue(
                key, level.score, request.classification.work_type,
                policy_violations=choices.policy_violations,
                reference_quote_available=choices.reference_quote_available,
            ) for level in criterion.levels
        }
        work_ids = [row["id"] for row in choices.work if row["source_id"] in eligible]
        allowed_scores = sorted(score for score, issue in issues.items() if issue is None)
        result[criterion.id] = {
            "required_work_choice_ids": work_ids,
            "required_rule_choice_ids": choices.approved_rule_ids[criterion.id],
            "allowed_scores": allowed_scores,
            "score_issues": {str(score): issue for score, issue in issues.items() if issue},
            "can_score": bool(work_ids) and bool(allowed_scores),
            "missing_evidence_message": MISSING_WORK_MESSAGES[key] if not work_ids else None,
            "review_reason": "승인된 점수 조건과 서버 검증 결과가 상충합니다."
            if work_ids and not allowed_scores else None,
        }
    return result


def _item_problem(row, requirements):
    if not isinstance(row, dict):
        return "평가 항목은 객체여야 합니다."
    criterion = row.get("criterion_id")
    if not isinstance(criterion, str) or criterion not in requirements:
        return "제공된 항목·근거 선택 ID 중에서 선택해야 합니다."
    requirement = requirements[criterion]
    state, score = row.get("observation"), row.get("score")
    if state not in ("observed", "unobserved", "insufficient_data", "needs_review"):
        return "허용된 관측 상태를 선택해야 합니다."
    if score is not None and type(score) is not int:
        return "점수는 정수 0·1·2 또는 null이어야 합니다."
    if state == "observed":
        if score is None or not row.get("required_work_evidence_id"):
            return "관측된 항목에는 점수와 업무 근거가 필요합니다."
        if criterion.endswith(".security") and score == 1:
            return "보안 준수 항목에는 1점을 줄 수 없습니다."
        if row["required_work_evidence_id"] not in requirement["required_work_choice_ids"]:
            return MISSING_WORK_MESSAGES[criterion.rsplit(".", 1)[1]]
        if score not in requirement["allowed_scores"]:
            return requirement["score_issues"].get(
                str(score), "승인된 기준에서 허용하지 않는 점수입니다.",
            )
    elif score is not None:
        return "미관측·자료 부족 항목에는 점수를 부여할 수 없습니다."
    if state == "needs_review" and row.get("review_required") is not True:
        return "검토 필요 항목은 review_required가 true여야 합니다."
    if state == "needs_review" and not row.get("required_work_evidence_id"):
        return "검토 필요 판단에는 실제 업무 근거가 필요합니다."
    key = criterion.rsplit(".", 1)[1]
    eligible = requirement["required_work_choice_ids"]
    if (state != "observed" and eligible
            and (state in {"needs_review", "insufficient_data"} or key in {"quality", "security"})
            and row.get("required_work_evidence_id") not in eligible):
        return MISSING_WORK_MESSAGES[key]
    if row.get("required_rule_evidence_id") not in requirement["required_rule_choice_ids"]:
        return "해당 항목의 승인된 기준 근거를 선택해야 합니다."
    return "선택 ID·필수 필드·자료형이 평가 응답 형식과 맞지 않습니다."


def _item_diagnostic(row, choices, requirements):
    row = row if isinstance(row, dict) else {}
    criterion, state, score = row.get("criterion_id"), row.get("observation"), row.get("score")
    known = isinstance(criterion, str) and criterion in requirements
    allowed_work = {entry["id"] for entry in choices.work}
    allowed_rules = {entry["id"] for entry in choices.rules}

    def safe_id(value, allowed):
        if value is None:
            return None
        return value if isinstance(value, str) and value in allowed else "[후보 밖 ID]"

    def safe_ids(value, allowed):
        return [safe_id(key, allowed) for key in value[:3]] if isinstance(value, list) else []

    required = requirements[criterion]["required_work_choice_ids"] if known else []
    return {
        "criterion_id": criterion if known else "[누락 또는 허용되지 않은 ID]",
        "observation": state if state in (
            "observed", "unobserved", "insufficient_data", "needs_review"
        ) else "[누락 또는 잘못된 상태]",
        "score": score if score is None or type(score) is int else "[잘못된 점수 형식]",
        "required_work_evidence_id": safe_id(row.get("required_work_evidence_id"), allowed_work),
        "work_evidence_ids": safe_ids(row.get("work_evidence_ids"), allowed_work),
        "required_rule_evidence_id": safe_id(row.get("required_rule_evidence_id"), allowed_rules),
        "rule_evidence_ids": safe_ids(row.get("rule_evidence_ids"), allowed_rules),
        "eligible_required_work_ids": required[:4],
        "eligible_required_work_count": len(required),
    }


def selection_model(choices: EvidenceChoices, request) -> type[StrictModel]:
    """각 항목의 필수 업무·승인 기준 근거를 API 출력 스키마에도 반영한다."""
    requirements = choice_requirements(choices, request)
    criteria = Enum(
        "EvaluationCriterionId", {f"C{i}": key for i, key in enumerate(requirements)}, type=str,
    )
    rules = Enum(
        "EvaluationRuleChoice", {entry["id"]: entry["id"] for entry in choices.rules}, type=str,
    )
    if choices.work:
        work = Enum(
            "EvaluationWorkChoice", {entry["id"]: entry["id"] for entry in choices.work}, type=str,
        )
        extra_work = Annotated[list[work], Field(max_length=3)]
        unscored_work = work | None
    else:
        extra_work = Annotated[list[NonEmpty], Field(max_length=0)]
        unscored_work = type(None)

    variants = []
    for number, (criterion_id, requirement) in enumerate(requirements.items()):
        basis = Enum(
            f"ApprovedRuleChoice{number}",
            {key: key for key in requirement["required_rule_choice_ids"]}, type=str,
        )

        def variant(name, observation, score, primary_work, review):
            return create_model(
                name, __base__=StrictModel,
                criterion_id=(Literal[criterion_id], ...),
                required_work_evidence_id=(primary_work, ...),
                work_evidence_ids=(extra_work, ...),
                reason=(NonEmpty, Field(
                    pattern=r"^(「[^」\r\n]{1,200}」|\[근거 없음\])",
                    description=(
                        "선택한 업무 근거의 짧은 원문을 「」로 인용하여 시작한 뒤, 그 사실을 "
                        "승인 기준과 대조해 판단한다. 기준 문구를 업무 사실로 인용하지 않는다. "
                        "해당 항목의 업무 근거가 없을 때만 [근거 없음]으로 시작하고 "
                        "누락 자료를 명시한다."
                    ),
                )),
                required_rule_evidence_id=(basis, ...),
                rule_evidence_ids=(Annotated[list[rules], Field(max_length=3)], ...),
                observation=(observation, ...),
                score=(score, ...),
                review_required=(review, ...),
            )

        required_work = unscored_work
        if requirement["required_work_choice_ids"]:
            required_work = Enum(
                f"RequiredWorkChoice{number}",
                {key: key for key in requirement["required_work_choice_ids"]}, type=str,
            )
        if requirement["can_score"] and requirement["allowed_scores"]:
            score_values = tuple(requirement["allowed_scores"])
            variants.append(variant(
                f"ScoredEvaluationItem{number}", Literal["observed"],
                Annotated[Literal[score_values], BeforeValidator(_strict_score)], required_work,
                Annotated[bool, Field(strict=True)],
            ))
        variants.extend([
            variant(f"UnobservedEvaluationItem{number}", Literal["unobserved"], type(None),
                    required_work if criterion_id.rsplit(".", 1)[1] in {"quality", "security"}
                    else unscored_work, Annotated[bool, Field(strict=True)]),
            variant(f"InsufficientEvaluationItem{number}", Literal["insufficient_data"],
                    type(None), required_work, Annotated[bool, Field(strict=True)]),
        ])
        if choices.work:
            variants.append(variant(
                f"ReviewEvaluationItem{number}", Literal["needs_review"], type(None),
                required_work if requirement["required_work_choice_ids"] else work,
                Annotated[Literal[True], BeforeValidator(_strict_review)],
            ))

    def check_item(row, handler):
        try:
            return handler(row)
        except ValidationError as exc:
            raise PydanticCustomError("evaluation_selection", "{reason}", {
                "reason": _item_problem(row, requirements),
                "diagnostic": _item_diagnostic(row, choices, requirements),
            }) from exc

    item = Annotated[Union[tuple(variants)], WrapValidator(check_item)]
    coaching = create_model(
        "SelectedCoaching", __base__=StrictModel,
        criterion_id=(criteria, ...), action=(NonEmpty, ...),
    )
    return create_model(
        "SelectedEvaluation", __base__=StrictModel,
        items=(Annotated[list[item], Field(min_length=5, max_length=5)], ...),
        coaching=(Annotated[list[coaching], Field(min_length=5, max_length=5)], ...),
    )


def resolve_selection(raw, choices: EvidenceChoices, request) -> AgentEvaluationOutput:
    checked = selection_model(choices, request).model_validate(raw).model_dump(mode="json")
    work = {entry["id"]: entry for entry in choices.work}
    rules = {entry["id"]: entry for entry in choices.rules}
    expected = set(choices.approved_rule_ids)
    for name in ("items", "coaching"):
        ids = [item["criterion_id"] for item in checked[name]]
        if len(ids) != len(set(ids)) or set(ids) != expected:
            raise EvaluationError(f"{name}: 승인된 5개 항목이 누락 또는 중복됐습니다.")
    items = []
    for item in checked["items"]:
        # 같은 구간의 반복 선택은 하나의 인용으로 정리한다. 새 근거를 보충하지 않는다.
        primary_work = item.pop("required_work_evidence_id")
        work_ids = list(dict.fromkeys(
            ([primary_work] if primary_work else []) + item.pop("work_evidence_ids")
        ))
        rule_ids = list(dict.fromkeys(
            [item.pop("required_rule_evidence_id")] + item.pop("rule_evidence_ids")
        ))
        item["work_evidence"] = [
            {"source_id": work[key]["source_id"], "quote": work[key]["text"]} for key in work_ids
        ]
        item["rule_evidence"] = [
            {
                "document_id": rules[key]["document_id"], "version": rules[key]["version"],
                "section_id": rules[key]["section_id"], "quote": rules[key]["content"],
            } for key in rule_ids
        ]
        items.append(item)
    return AgentEvaluationOutput.model_validate({
        "run_id": request.run_id, "criteria_sha256": content_hash(request.approved_criteria),
        "items": items, "coaching": checked["coaching"], "summary": "근거·점수 검증 대기",
    })
