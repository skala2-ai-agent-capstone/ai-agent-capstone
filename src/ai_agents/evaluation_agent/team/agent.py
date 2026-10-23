"""공개 집계 지표에서 팀 코칭 우선순위를 선택하는 LLM 호출."""

import json

from openai import APIConnectionError, APIStatusError, APITimeoutError
from pydantic import ValidationError

from ...observability import trace_step
from ...observability.tracing import trace_span
from ...shared.team_contracts import TeamDecision, TeamSnapshot
from .report import ACTIONS, build_report, metrics_for, validate_decision

PROMPT_VERSION = "team-coaching-v1"
SYSTEM_PROMPT = """당신은 팀의 AI 업무 개선 코치다. 입력은 서버가 공개를 허용한 업무 집계다.
개인 평가·인물 추론·개인 순위를 만들지 않는다. 업무 유형·기준 그룹 간 점수를 합치지 않는다.
metrics의 band=strength에서 강점 근거를 최대 3개, band=improvement에서 개선 근거를
최대 3개 선택한다. 각 band에 후보가 있으면 최소 1개를 선택한다. 없으면 빈 배열을 쓴다.
관측 비율이 낮으면 대표성이 제한됨을 고려하고 개선 활동의 실행 가능성을 우선한다.
개선 근거마다 criterion이 일치하는 actions의 action_id 하나를 선택한다.
점수·문장·인물·새 ID를 생성하지 말고 제공된 metric_id와 action_id만 선택한다.
반드시 아래 JSON 스키마의 객체만 반환한다.
"""


class TeamEvaluationError(ValueError):
    def __init__(self, code, *, retryable=False):
        self.code, self.retryable = code, retryable
        super().__init__(code)


class TeamAgent:
    def __init__(self, client, model):
        self.client, self.model = client, model

    @trace_step("team.generate")
    async def generate(self, snapshot: TeamSnapshot):
        snapshot = TeamSnapshot.model_validate(snapshot.model_dump(mode="json"))
        metrics = metrics_for(snapshot)
        if not metrics:
            return build_report(snapshot, TeamDecision(strengths=[], improvements=[]))
        # UUID·기간·구성원 수·기준의 자유 문구는 모델에 보내지 않는다.
        payload = {
            "metrics": [{k: v for k, v in row.model_dump(mode="json").items()
                         if k not in {"rubric_version", "rubric_sha256"}} for row in metrics],
            "actions": {key: {"criterion": value[0], "recommendation": value[1]}
                        for key, value in ACTIONS.items()},
        }
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT + json.dumps(
                TeamDecision.model_json_schema(), ensure_ascii=False,
            )},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        for attempt in range(2):
            try:
                with trace_span("team.llm", inputs=messages, run_type="llm") as span:
                    reply = await self.client.chat.completions.create(
                        model=self.model, messages=messages, temperature=0, max_tokens=1800,
                        response_format={"type": "json_object"},
                        # 기존 프록시 stage 계약 유지. 개인 work/user ID는 전달하지 않는다.
                        extra_headers={"X-Stage": "evaluate"},
                    )
                    span.result(reply)
                if not reply.choices or reply.choices[0].finish_reason != "stop":
                    raise TeamEvaluationError("LLM_REJECTED")
                raw = reply.choices[0].message.content
                if not isinstance(raw, str) or len(raw) > 30000:
                    raise ValueError("invalid response")
                decision = TeamDecision.model_validate_json(raw)
                validate_decision(decision, metrics)
                return build_report(snapshot, decision)
            except (APIConnectionError, APITimeoutError):
                raise TeamEvaluationError("LLM_UNAVAILABLE", retryable=True) from None
            except APIStatusError as exc:
                retryable = exc.status_code in {408, 429} or exc.status_code >= 500
                raise TeamEvaluationError("LLM_UNAVAILABLE" if retryable else "LLM_REJECTED",
                                          retryable=retryable) from None
            except TeamEvaluationError:
                raise
            except (ValidationError, ValueError):
                if attempt:
                    raise TeamEvaluationError("INVALID_TEAM_DECISION") from None
                messages.append({"role": "user", "content":
                                 "형식·근거 검증 실패. 제공된 band와 일치하는 ID 및 "
                                 "해당 criterion의 action_id만 선택하세요."})
        raise AssertionError("unreachable")
