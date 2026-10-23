"""llm_exchanges 모의 데이터 생성기.

업무 DB 원천(mcp_servers/data/all_cases.jsonl, 가상 40건)을 읽어
"직원이 업무 공간에서 vLLM과 대화 → 업무 완료 → Agent 분류·기준 생성·평가" 흐름의
호출 기록을 만든다.
결과는 mcp_servers/data/llm_exchanges.jsonl이며, 업무 DB 적재기(db-init)가 그대로
llm_exchanges에 넣는다.
work_id·user_id는 업무 DB 값을 그대로 써서 work_cases·users와 외래 키로 이어진다.

- 직원 대화: mcp_servers/db/loader/chat_log.py 규칙(적재기와 같음).
  사용자 메시지 1개 = 호출 1건이고, 답이 없는 메시지는 끊긴 호출로 남는다.
  그래서 messages 뷰가 원천 대화를 그대로 돌려준다.
- Agent 호출: 요청·응답 본문은 실제 호출 형태를 따르되 스키마·내규는 축약한 모의 값이다.
- 토큰 수·처리 시간은 글자 수로 추정한 값이다(실측 아님). 같은 입력이면 항상 같은 결과가 나온다.

실행(프로젝트 루트에서):  .venv/bin/python llm_proxy/mock/build_mock.py
"""

import json
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "mcp_servers"))

from db.loader.chat_log import MODEL, iso, short_id, sse, tokens, workspace_exchanges  # noqa: E402

from ai_agents.classification.prompts import PROMPT_VERSION as CLASSIFY_PROMPT_VERSION  # noqa: E402
from ai_agents.classification.prompts import SYSTEM_PROMPT as CLASSIFY_PROMPT  # noqa: E402
from ai_agents.criteria_agent.builder import ITEM_NAMES  # noqa: E402
from ai_agents.criteria_agent.builder import SYSTEM_PROMPT as RUBRIC_PROMPT  # noqa: E402
from ai_agents.evaluation_agent.final.prompts import (  # noqa: E402
    PROMPT_VERSION as EVALUATION_PROMPT_VERSION,
)
from ai_agents.evaluation_agent.final.prompts import SYSTEM_PROMPT as EVALUATION_PROMPT  # noqa: E402

SOURCE = ROOT / "mcp_servers/data/all_cases.jsonl"
OUT = ROOT / "mcp_servers/data/llm_exchanges.jsonl"
SEED = 20261022

AGENT_MEMBER = "eval-agent"              # 평가 Agent의 프록시 키 이름
DEV_MEMBER = "dev-local"                 # 개발자 직접 테스트(업무·사용자 없음)

# 업무 DB 유형 → 분류기(WorkType) 유형
CLASSIFIER_TYPE = {
    "rfp_analysis": "rfp_analysis", "proposal": "proposal",
    "quote": "quotation", "inquiry_reply": "customer_response",
}
CRITERIA = ["problem_definition", "instruction_quality", "verification", "security", "quality"]
RETRY_CASE = "E2E-IQ-02"           # 첫 답변 도중 창을 닫아 끊긴 뒤 같은 질문을 다시 보낸 업무
CONTEXT_OVERFLOW_CASE = "E2E-RFP-05"   # 분류 입력이 길어 vLLM이 400을 돌려준 뒤 재실행
RUBRIC_RETRY_CASE = "E2E-PR-04"    # 기준 초안이 검증을 통과하지 못해 1회 재요청
EVAL_TIMEOUT_CASE = "E2E-QT-03"    # 평가 호출이 시간 초과(504)된 뒤 담당자가 재실행
DEV_TEST_COUNT = 3                 # 서버 기동 직후 개발자 curl 테스트
COLUMNS = ["id", "created_at", "member", "endpoint", "stage", "work_id", "user_id", "model",
           "stream", "status_code", "error", "latency_ms", "input_tokens", "output_tokens",
           "request", "response", "response_text"]


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value)


class Builder:
    def __init__(self):
        self.rng = random.Random(SEED)
        self.rows: list[dict] = []

    # ---------- 공통 ----------
    def latency(self, input_tokens: int, output_tokens: int) -> int:
        """L4 + Gemma 4 26B-A4B 4bit 가정: prefill 약 6,600 tok/s, 생성 약 45 tok/s."""
        base = 250 + input_tokens * 0.15 + output_tokens * 22
        return int(base * self.rng.uniform(0.9, 1.15))

    def add(self, **row) -> dict:
        defaults = dict(
            stage=None, work_id=None, user_id=None, stream=False, error=None,
            input_tokens=None, output_tokens=None, request=None, response=None,
            response_text=None, model=MODEL,
        )
        record = {**defaults, **row}
        self.rows.append(record)
        return record

    # ---------- 응답 본문 ----------
    @staticmethod
    def chat_response(key: str, content: str, prompt: int, completion: int, at: datetime) -> dict:
        return {
            "id": f"chatcmpl-mock-{key}", "object": "chat.completion",
            "created": int(at.timestamp()), "model": MODEL,
            "choices": [{
                "index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": content, "tool_calls": []},
            }],
            "usage": {"prompt_tokens": prompt, "completion_tokens": completion,
                      "total_tokens": prompt + completion},
        }

    @staticmethod
    def responses_response(key: str, text: str, prompt: int, completion: int,
                           at: datetime) -> dict:
        return {
            "id": f"resp_mock_{key}", "object": "response", "created_at": int(at.timestamp()),
            "model": MODEL, "status": "completed",
            "output": [{
                "id": f"msg_mock_{key}", "type": "message", "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }],
            "usage": {"input_tokens": prompt, "output_tokens": completion,
                      "total_tokens": prompt + completion},
        }

    # ---------- 직원 대화 ----------
    def workspace(self, case: dict) -> None:
        rows = workspace_exchanges(case)
        if case["case_id"] == RETRY_CASE:
            # 첫 답변이 1/3쯤 나왔을 때 창을 닫았다가 같은 질문을 다시 보냈다
            i, first = next((i, r) for i, r in enumerate(rows) if r["error"] is None)
            answer = next(m for m in case["raw"]["messages"] if m["role"] == "assistant")["content"]
            answered_at = parse_time(first["created_at"])
            asked_at = answered_at - timedelta(milliseconds=first["latency_ms"])
            cut_at = asked_at + (answered_at - asked_at) / 3
            spent = int((cut_at - asked_at).total_seconds() * 1000)
            cut_sse = sse(short_id(first["work_id"], "retry"), answer, 0, 0, asked_at,
                          cut=len(answer) // 3)
            rows.insert(i, {**first, "created_at": iso(cut_at), "error": "client_disconnected",
                            "latency_ms": spent, "input_tokens": None, "output_tokens": None,
                            "response_text": cut_sse})
        self.rows.extend(rows)

    # ---------- Agent ----------
    def responses_request(self, system: str, payload: dict, schema_name: str,
                          max_tokens: int, extra: list | None = None, **options) -> dict:
        return {
            "model": MODEL,
            "input": [{"role": "system", "content": system},
                      {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                      *(extra or [])],
            # 실제 호출은 Pydantic 모델 전체 스키마를 보낸다. 모의 데이터에서는 축약했다.
            "text": {"format": {"type": "json_schema", "name": schema_name, "strict": True,
                                "schema": {"type": "object", "description": "(축약)"}}},
            "max_output_tokens": max_tokens, "store": False, **options,
        }

    def agent_call(self, work: dict, stage: str, at: datetime, request: dict,
                   output: dict | None, *, status: int = 200, error: str | None = None,
                   error_body: dict | None = None, latency: int | None = None) -> datetime:
        prompt = sum(tokens(item["content"]) for item in request["input"])
        key = short_id(work["work_id"], stage, iso(at))
        if output is None:
            # 기록은 응답(실패)이 끝난 시각에 남는다
            spent = latency if latency is not None else self.latency(prompt, 0)
            done = at + timedelta(milliseconds=spent)
            self.add(member=AGENT_MEMBER, endpoint="/v1/responses", stage=stage,
                     work_id=work["work_id"], request=request, status_code=status,
                     error=error, created_at=iso(done), response=error_body, latency_ms=spent)
            return done
        text = json.dumps(output, ensure_ascii=False)
        completion = tokens(text)
        spent = self.latency(prompt, completion)
        done = at + timedelta(milliseconds=spent)
        self.add(member=AGENT_MEMBER, endpoint="/v1/responses", stage=stage,
                 work_id=work["work_id"], request=request, status_code=200,
                 created_at=iso(done), latency_ms=spent, input_tokens=prompt,
                 output_tokens=completion,
                 response=self.responses_response(key, text, prompt, completion, done))
        return done

    def agent(self, case: dict) -> None:
        raw = case["raw"]
        work = raw["work_case"]
        work_type = CLASSIFIER_TYPE[work["work_type"]]
        final = raw["artifact_versions"][-1]
        at = parse_time(work["completed_at"]) + timedelta(seconds=self.rng.randint(3, 20))

        # 1) 분류: 업무 ID는 프롬프트에 넣지 않는다(헤더로만 기록).
        #    입력 ID는 mcp_data.classification_context처럼 중립 키를 쓴다.
        classify_payload = {
            "title": case["summary"].split(" (")[0],
            "customer_request": "",
            "messages": [{"id": f"message-{i:03d}", "role": m["role"], "content": m["content"]}
                         for i, m in enumerate(raw["messages"], 1)],
            "artifacts": [{"id": f"artifact-{i:03d}", "title": "",
                           "stage": "final" if a is final else "draft",
                           "version": i, "content": a["content"]}
                          for i, a in enumerate(raw["artifact_versions"], 1)],
            "coverage": "complete", "missing_sources": [],
            "prompt_version": CLASSIFY_PROMPT_VERSION,
        }
        classify_request = self.responses_request(
            CLASSIFY_PROMPT, classify_payload, "DecisionEnvelope", 2000)
        if case["case_id"] == CONTEXT_OVERFLOW_CASE:
            at = self.agent_call(work, "classify", at, classify_request, None, status=400,
                                 error="upstream_error", latency=180, error_body={
                                     "object": "error", "type": "BadRequestError", "code": 400,
                                     "message": "This model's maximum context length is 32768 "
                                                "tokens. Please reduce the length of the input.",
                                 })
            at += timedelta(minutes=12)   # 운영자가 MAX_MODEL_LEN을 늘려 재기동 후 재실행
        decision = {
            "decision": {"status": "classified", "task": {
                "work_type": work_type, "goal": case["summary"].split(" (")[0],
                "evidence_ids": ["e0001"]}},
            "reason": "첫 사용자 요청과 최종 산출물의 형식이 이 업무 유형과 일치한다.",
        }
        at = self.agent_call(work, "classify", at, classify_request, decision)

        # 2) 평가기준 생성: 업무 대화 없이 유형·내규만 보낸다
        rubric_payload = {
            "work_type": work_type,
            "item_instructions": [{"item": name} for name in ITEM_NAMES],
            "policy": {"chunks": [
                {"doc_id": "POL-AX-EVAL", "version": "v2", "clause_id": clause,
                 "text": "(내규 본문 축약)"} for clause in ("ART-04", "ART-14", "APX-4", "APX-5")
            ]},
            "previous_draft": None, "human_feedback": None,
        }
        rubric = {"items": [{
            "item": name, "focus": f"{name} 관찰 기준(모의)",
            "score_2": "기준을 모두 충족하고 근거가 기록에 있다.",
            "score_1": "일부만 충족하거나 근거가 불완전하다.",
            "score_0": "충족하지 않는다." if name != "보안 준수" else "해당 없음",
            "related_clauses": ["ART-04", "APX-5"],
        } for name in ITEM_NAMES]}
        rubric_request = self.responses_request(
            RUBRIC_PROMPT, rubric_payload, "GenerationRubric", 6000)
        at += timedelta(seconds=self.rng.randint(2, 6))
        if case["case_id"] == RUBRIC_RETRY_CASE:
            broken = {"items": rubric["items"][:4]}
            at = self.agent_call(work, "rubric", at, rubric_request, broken)
            rubric_request = self.responses_request(
                RUBRIC_PROMPT, rubric_payload, "GenerationRubric", 6000, extra=[
                    {"role": "assistant", "content": json.dumps(broken, ensure_ascii=False)},
                    {"role": "user", "content": "다음 검증 오류를 수정해 전체 기준을 다시 "
                                                "생성하세요: 결과물 품질 항목이 없습니다."},
                ])
        at = self.agent_call(work, "rubric", at, rubric_request, rubric)

        # 3) 담당자 승인 후 평가
        at += timedelta(minutes=self.rng.randint(5, 40))
        run_id = f"RUN-{work['work_id']}-1"
        evaluate_payload = {
            "run_id": run_id, "work_type": work_type,
            "approved_criteria": [f"{work_type}.{c}" for c in CRITERIA],
            "work_evidence_catalog": [
                {"id": f"w{i + 1:03d}", "kind": "message", "seq": m["seq"]}
                for i, m in enumerate(raw["messages"])
            ],
            "missing_sources": [],
            "prompt_version": EVALUATION_PROMPT_VERSION,
        }
        evaluate_request = self.responses_request(
            EVALUATION_PROMPT, evaluate_payload, "EvaluationSelection", 6000, temperature=0)
        if case["case_id"] == EVAL_TIMEOUT_CASE:
            at = self.agent_call(work, "evaluate", at, evaluate_request, None, status=504,
                                 error="upstream_timeout", latency=600_000)
            at += timedelta(minutes=8)   # 담당자가 평가를 다시 실행
            run_id = f"RUN-{work['work_id']}-2"
            evaluate_payload["run_id"] = run_id
            evaluate_request = self.responses_request(
                EVALUATION_PROMPT, evaluate_payload, "EvaluationSelection", 6000,
                temperature=0)
        items = []
        for criterion in CRITERIA:
            if criterion == "security" and self.rng.random() < 0.35:
                items.append({"criterion_id": f"{work_type}.{criterion}", "state": "unobserved",
                              "score": None, "evidence_ids": []})
                continue
            items.append({"criterion_id": f"{work_type}.{criterion}", "state": "observed",
                          "score": self.rng.choice([1, 2, 2]),
                          "evidence_ids": [f"w{self.rng.randint(1, len(raw['messages'])):03d}"]})
        evaluation = {
            "run_id": run_id, "items": items,
            "coaching": [{"criterion_id": item["criterion_id"],
                          "action": "확정 전에 원문과 1회 더 대조한 기록을 남기세요."}
                         for item in items if item["score"] == 1][:2],
            "summary": "주요 요구는 반영했으나 일부 검증 기록이 부족하다.",
        }
        self.agent_call(work, "evaluate", at, evaluate_request, evaluation)

    # ---------- 개발자 테스트 ----------
    def dev_tests(self, first_at: datetime) -> None:
        at = first_at - timedelta(days=1)
        for i in range(DEV_TEST_COUNT):
            content = "안녕하세요. 서버가 정상 동작합니다." if i else "pong"
            request = {"model": MODEL, "messages": [
                {"role": "user", "content": "ping" if i == 0 else "한국어로 짧게 인사해줘"}]}
            prompt, completion = tokens(request["messages"][0]["content"]) + 8, tokens(content)
            spent = self.latency(prompt, completion)
            done = at + timedelta(minutes=i * 3, milliseconds=spent)
            self.add(member=DEV_MEMBER, endpoint="/v1/chat/completions", request=request,
                     status_code=200, created_at=iso(done), latency_ms=spent,
                     input_tokens=prompt, output_tokens=completion,
                     response=self.chat_response(short_id("dev", str(i)), content, prompt,
                                                 completion, done))


def main() -> None:
    cases = [json.loads(line) for line in SOURCE.read_text().splitlines() if line.strip()]
    builder = Builder()
    for case in cases:
        builder.workspace(case)
        builder.agent(case)
    builder.dev_tests(min(parse_time(r["created_at"]) for r in builder.rows))

    rows = sorted(builder.rows, key=lambda r: parse_time(r["created_at"]))
    ordered = [{"id": i, **{c: row.get(c) for c in COLUMNS[1:]}} for i, row in enumerate(rows, 1)]
    OUT.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in ordered))
    print(f"{len(ordered)} rows → {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
