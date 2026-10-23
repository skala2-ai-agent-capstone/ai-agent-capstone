# 최근 질문·응답 5회 중간 피드백

2026-10-23 변경: 새 업무의 유형은 시작 시 사용자가 선택한다. 중간 피드백은 원래 분류
LLM을 호출하지 않으며, MCP가 저장된 유형을 문맥에 반환한다.
아래 null 유형 예시/가상 데이터는 이전 요청 호환용이다. 새 업무는 필수 선택이다.
[유형 선택 계약과 담당자별 변경](user_selected_work_type.md)을 참고한다.

2026-10-22. 입력 자료: 사용자가 제공한 `realtime-feedback-flow (1).md`와
`axeval` PostgreSQL 16 DB 명세(동일 날짜). 이 문서는 해당 자료를 검토해 구현한
**Agent 측 계약 v1 및 서버 담당자에게 제안하는 연동 계약**이다.
백엔드·MCP 서버가 이미 이 계약을 구현했다는 뜻은 아니다.

## 구현 범위

사용자가 버튼을 누르면, 백엔드가 정한 **최근 완료된 질문 + AI 응답 최대 5쌍**에 대해
간단한 진단·근거·다음 행동·추천 프롬프트를 만든다. 1~4쌍이면 있는 만큼 처리한다.
0쌍이면 요청을 만들지 않아야 한다. 자료의 `최근 5개 seq` 표현은 **5회, 최대 10개 메시지**로
고쳐야 한다. 실시간 감지, 최종 개인 평가, 팀 집계는 이번 구현 범위가 아니다.

```text
사용자 버튼 → Spring: 본인 권한·5분 제한 확인
           → 최근 완료 5쌍 고정 + 요청·outbox 저장 → 202
           → RabbitMQ → FeedbackWorker
                         → MCP: start_live_evaluation
                         → MCP: get_messages
                         → 질문·응답 짝 / 범위 / 마스킹 검사
                         → 자체 호스팅 LLM: 피드백 JSON 생성
                         → JSON / 근거 인용 / 추천 대상 검사
                         → MCP: save_live_evaluation → ack
화면 폴링 ← Spring ← live_evaluations 결과
```

Agent는 DB나 Spring을 직접 호출하지 않는다. 조회·저장은 MCP를 통한다.
MCP가 Spring 내부 API를 사용할지 DB에 접근할지는 서버 담당 영역이며,
이번 코드는 MCP 외부 계약만 의존한다. 첨부 흐름도는 MCP→DB로 기술되어 있다.

| 파일 | 책임 |
|---|---|
| `shared/live_feedback_contracts.py` | 큐 이벤트, 요청 문맥, 대화 묶음, 출력, 저장 응답 스키마 |
| `evaluation_agent/feedback/agent.py` | 자체 호스팅 Chat Completions 호출, 구조·근거 오류 1회 재생성 |
| `evaluation_agent/feedback/validation.py` | 5쌍·범위·마스킹·인용·기준 ID·추천 질문 검사 |
| `orchestration/live_feedback.py` | MCP 조회→생성→저장, 일시 오류 재시도, 실행 근거 저장 |
| `orchestration/feedback_worker.py` | 중복 처리, 재시도/DLQ 발행 확인 후 ack, 추적 |
| `integrations/live_mcp.py` | 전용 자격증명으로 MCP 도구 3개만 호출 |
| `integrations/feedback_rabbit.py` | RabbitMQ 소비·확인 발행·수동 ack |
| `cli/feedback.py` | `schemas`, `once`, `worker` 실행 |
| `validation/feedback_targets.py` | 모의 기능 검증 / 실제 모델 품질 실험 진입점 |

경로는 모두 `src/ai_agents/` 기준이다. 팀원 소유 `criteria_agent/`와 기존
`evaluation_agent/final/`은 수정하지 않았다. 기준이 있으면 고정한 버전을 읽고,
없으면 일반적인 프롬프트 코칭을 한다. 기준 생성 Agent를 피드백 도중 실행하지 않는다.

## DB 명세를 보고 정한 사항

1. `llm_exchanges.stage='workspace_chat'`의 **완료된 성공 응답**만 대화 후보로 삼는다.
   분류·기준 생성·평가 호출 로그를 섞지 않는다. `request`의 전체 대화 이력을 매번 펼치면
   이전 대화가 중복되므로, 각 exchange의 현재 사용자 질문과 해당 응답을 한 번만 노출해야 한다.
2. 백엔드가 요청 시점에 최근 5개 exchange를 선택하고 `from_seq`, `to_seq` 및
   `rubric_doc_id`, `rubric_version`을 고정한다. 5분 제한은 사용자별로 동시 요청까지 막아야 한다.
3. `messages` 뷰의 `seq`, `message_id`, 원문은 요청 후에도 변하지 않아야 한다.
   조회 때마다 필터 후 `row_number()`로 재번호를 부여하면 범위가 변할 수 있다.
   수정·삭제가 가능한 로그라면 선택 exchange/message ID와 마스킹본 스냅샷을 별도로 보존해야 한다.
   현재 첨부 자료만으로 뷰 SQL과 불변성은 확인되지 않았다.
4. `*_masked`가 원문을 그대로 반환한다는 스키마 주석이 있다. 이름만으로 마스킹 완료로
   취급할 수 없다. 실제 마스킹 처리 후에만 `masking_applied=true`를 반환해야 한다.
5. `live_evaluations.items/coaching` JSONB에 출력과 실행 정보를 저장할 수 있어
   Agent 때문에 별도 결과 테이블을 즉시 만들 필요는 없다. 아래 JSON 구조는 프론트와 합의할 제안이다.
6. 기존 `evaluation_failures`가 최종 `evaluations` FK를 참조한다면 live ID를 넣을 수 없다.
   백엔드 DLQ 담당자가 중간 피드백 실패 기록 경로를 분리하거나 스키마를 확장해야 한다.

## MCP 담당자에게 전달할 계약

현재 저장소의 MCP 구현은 첨부 DB와 다르며, 아래 도구는 신규 구현이 필요하다.
기존 최종 평가 토큰을 중간 평가에 재사용하지 않는다. Worker 자격증명으로 접근 가능한
회사·요청 범위를 서버에서 검증하고, 큐에 들어온 `company_id`를 인증 근거로 믿지 않는다.
`live_eval_id`로 요청자·회사·업무를 조회해 본인 요청인지 검사한다.

모든 도구 응답은 공통 envelope를 쓴다.

```json
{"ok": true, "data": {"도구별": "응답"}}
```

오류는 `{"ok":false,"error":{"code":"TEMPORARY_FAILURE","retryable":true}}`처럼
반환한다. 이 조합만 일시 오류로 재시도한다. 권한·없는 ID·잘못된 입력은 영구 오류다.
오류 메시지에 대화 원문이나 자격증명을 넣지 않는다.

### 1. start_live_evaluation

입력: `{"live_eval_id":"LIVE-DEMO"}`.
`대기`일 때만 원자적으로 `생성 중`으로 변경한다. 재전달된 `생성 중`은 같은 문맥으로
재개하고, `완료/실패`는 상태를 변경하지 않는다. **상태뿐 아니라 정본 문맥을 반환하도록 확장 제안한다.**

```json
{
  "live_eval_id": "LIVE-DEMO", "company_id": "C-DEMO",
  "work_id": "W-DEMO", "requested_by": "U-DEMO",
  "requested_at": "2026-10-22T10:00:00+09:00",
  "from_seq": 1, "to_seq": 10, "status": "생성 중",
  "work_type": null,
  "rubric_doc_id": null, "rubric_version": null, "rubric": null
}
```

기준이 있으면 `rubric`은 아래 구조다. doc/version은 요청 행의 값과 일치해야 한다.
기준이 고정되어 있는데 조회에 실패했다고 `null`로 대체하면 안 된다.
기준 생성 로직 수정 없이 MCP 변환 계층에서 기존 기준을 필요한 필드로 매핑한다.

```json
{
  "doc_id": "DOC-RUBRIC", "version": "v2",
  "items": [{"item_id": "R-1", "item": "요청 명확성", "criterion": "목적과 출력 조건을 명시한다"}]
}
```

이 축약 계약은 코칭용이다. 최종 평가 점수·등급의 계약을 대체하지 않는다.
`work_type`은 `rfp_analysis`, `proposal`, `quote`, `inquiry_reply` 또는 이전 요청의 null이다.
기준/최종 평가 내부 별칭 `quotation`, `customer_response`와 혼용하지 않는다.

### 2. get_messages

입력: `{"live_eval_id":"LIVE-DEMO","work_id":"W-DEMO","from_seq":1,"to_seq":10}`.
첨부 도구 인자에 **live_eval_id를 추가 제안**한다. 이를 통해 요청의 권한·고정 범위를 검사한다.

```json
{
  "live_eval_id": "LIVE-DEMO", "work_id": "W-DEMO", "from_seq": 1, "to_seq": 10,
  "masking_applied": true, "masking_version": "mask-v1", "complete": true,
  "messages": [
    {"message_id":"W-DEMO-M01","exchange_id":101,"seq":1,"role":"user",
     "content":"견적 요청 메일을 작성해줘.","created_at":"2026-10-22T09:01:00+09:00"},
    {"message_id":"W-DEMO-M02","exchange_id":101,"seq":2,"role":"assistant",
     "content":"견적 요청 메일 초안입니다.","created_at":"2026-10-22T09:02:00+09:00"}
  ]
}
```

위 배열은 앞 1쌍만 보여 준 형식 예시다. 실제 from=1/to=10 응답에는 선택된 5쌍 전체가 필요하다.
완전한 실행 예시는 `samples/feedback/complete-five.json`의 `batch`다.
순서는 Agent가 seq로 정렬하지만 중복 seq/ID, 질문·응답 누락, 교차하는 쌍, 요청 이후 데이터는
거부한다. `complete=true`는 서버가 고정한 선택 집합을 누락 없이 반환했다는 의미다.
Agent만으로 DB의 '정말 최신 5회인가'까지 입증할 수는 없다.

메시지당 12,000자, 전체 대화 50,000자까지 받는다. 초과 시 몰래 자르지 않고 실패시킨다.
기준은 최대 30개다. 마스킹 위치는 프록시 또는 MCP에서 정하되, 이 도구의 출구에서는
마스킹이 끝나 있어야 한다. Agent 정규식은 일부 잔여 패턴 검사이며 완전한 개인정보 탐지기가 아니다.

### 3. save_live_evaluation

입력 필드는 `live_eval_id, status, items, coaching, model, latency_ms, error`다.
이번 Worker는 성공 결과만 저장한다. 실패 확정은 아래 백엔드 DLQ 소비자가 맡는다.

```json
{
  "live_eval_id": "LIVE-DEMO", "status": "완료",
  "items": [{
    "item_id": "F-1", "criterion_id": null, "kind": "improvement",
    "diagnosis": "메일에 포함할 요청 조건이 부족합니다.",
    "evidence": [{"message_id":"W-DEMO-M01","quote":"견적 요청 메일을 작성해줘."}]
  }],
  "coaching": {
    "schema_version": "1", "provenance": {"trace_id": "실행 ID"},
    "suggestions": [{
      "item_id":"F-1", "target_message_id":"W-DEMO-M01",
      "next_action":"품목과 수량, 회신 기한을 구체화하세요.",
      "suggested_prompt":"[품목] [수량]에 대한 견적 요청 메일을 작성해줘. 회신 기한은 [날짜]야."
    }]
  },
  "model":"배포한 모델 ID", "latency_ms":1200, "error":null
}
```

화면은 `items`와 `coaching.suggestions`를 사용한다. 총점·직원 등급을 만들지 않는다.
항목·추천은 각각 최대 3개이며, 모든 진단에 실제 메시지의 연속 인용을 요구한다.
추천은 사용자 질문 ID를 가리키고 개선 항목에는 반드시 다음 행동이 연결된다.
인용의 정확성은 코드로 검사하지만, 진단의 의미상 타당성은 별도 품질 평가 대상이다.

실제 provenance에는 trace ID, 모델·설정, 프롬프트 버전, 기준 doc/version/hash,
source hash, exchange/message IDs, 범위, 마스킹 버전, Git commit/dirty/worktree hash가 들어간다.
`latency_ms`는 workflow 시작부터 저장 직전까지이며 큐 대기·저장 왕복 시간은 제외한다.

서버는 원자적으로 결과를 저장하고, 완료·실패 상태는 이후 호출로 덮어쓰지 않는다.
응답 `data`는 `{"live_eval_id":"LIVE-DEMO","status":"완료","duplicate":false}`다.
중복이면 `duplicate:true`와 **기존 확정 상태**를 반환한다. 저장 확인을 받은 뒤에만 ack한다.
동시 중복 실행에서 LLM이 두 번 호출되는 것까지 막지는 않는다. 계산 중복도 막으려면
별도 실행 lease/소유권 계약을 추가해야 한다.

전체 JSON Schema: `python -m ai_agents.cli.feedback schemas`.
현재 스키마는 알 수 없는 필드를 거부하므로 서버의 추가 필드는 합의 후 반영한다.

## 큐와 실패 처리

백엔드가 durable exchange/queue와 binding을 선언한다. Worker는 수동 ack와 prefetch=1로
소비하고, retry/DLQ는 default exchange를 통해 정확한 큐 이름으로 발행한다.
큐 선언 값을 Worker가 덮어쓰지 않는다.

| 큐 | 필요한 설정 |
|---|---|
| `live_evaluation.requests` | `live_evaluation.exchange` direct / `live_evaluate` binding |
| `live_evaluation.retry` | TTL 5000ms, DLX 위 exchange, DL routing key `live_evaluate` |
| `live_evaluation.dlq` | 백엔드 실패 기록 소비자 |

호출 단위 일시 오류는 최초+1회(0.5초 후), 형식·인용 오류는 1회 재생성한다.
그래도 일시 오류면 event attempt를 1 올려 retry 큐로 보낸다. attempt 0~3으로 총 4회 전달까지
허용하고 마지막 실패는 DLQ다. 권한·잘못된 범위·마스킹·영구 모델 오류는 즉시 DLQ다.
MCP 세션 연결 실패는 호출 재시도 대신 큐 재시도로 처리한다.

재발행은 persistent + mandatory + publisher confirms를 사용하며 **Basic.Ack 확인 후** 원본을 ack한다.
확인 실패 시 원본을 nack/requeue한다. 확정 상태 재전달은 조회 후 바로 ack한다.
이 방식도 at-least-once이므로 서버의 결과 중복 방지는 필수다.
참고: [aio-pika 작업 큐 예제](https://docs.aio-pika.com/rabbitmq-tutorial/2-work-queues.html),
[publish·confirm API](https://docs.aio-pika.com/apidoc.html).

백엔드 DLQ 소비자는 유효한 LIVE_EVALUATE의 정본 권한·ID를 다시 검사해 아직 끝나지 않은 요청만
`실패`로 바꾼다. 이미 완료된 요청을 실패로 덮어쓰면 안 된다. error는 안전한 코드만 기록한다.
깨진/과대한 이벤트는 원문을 복사하지 않고
`{"message_id":"새 UUID","type":"INVALID_LIVE_EVALUATE"}`와 error_code 헤더만 DLQ에 보낸다.
이는 업무 ID가 없는 운영 오류 알림이므로 특정 DB 요청을 실패 처리하면 안 된다.
잘못된 이벤트 때문에 고립된 대기 요청을 찾는 운영 정합성 점검은 백엔드 후속 작업이다.

## 실행

```bash
uv sync --locked --extra worker --inexact
source .venv/bin/activate
python -m ai_agents.cli.feedback schemas
```

uv를 사용하지 않는 환경에서는 활성화한 환경에
`python -m pip install -r requirements-worker.txt`로 Worker 의존성까지 설치한다.
기존 `requirements.txt`는 기본 Agent 용도로 유지했다.

`.env.example`의 `FEEDBACK_LLM_BASE_URL`, `FEEDBACK_LLM_MODEL`, `FEEDBACK_LLM_API_KEY`,
`FEEDBACK_MCP_URL`, `FEEDBACK_MCP_TOKEN`, `FEEDBACK_AMQP_URL`을 실제 담당자와 맞춘다.
LLM은 Chat Completions의 JSON mode를 지원해야 한다. AWS 모델의 지원 여부와 지연은 아직 미검증이다.

```bash
# 서버 연동 후: 실제 MCP 조회·모델 호출·DB 저장을 수행한다.
python -m ai_agents.cli.feedback once --event /path/to/real-event.json

# 상시 소비. 큐는 백엔드에서 먼저 생성한다.
python -m ai_agents.cli.feedback worker --trace-dir outputs/feedback-traces --langsmith
```

`once`는 RabbitMQ에 발행하지 않는다. 실패 시 `would_publish`만 출력하므로 실제 재시도나 실패
확정도 하지 않는다. 샘플 event는 가상 ID여서 운영 서버에 그대로 보내는 실행 예시가 아니다.

`--langsmith`를 명시한 실행만 정제된 RunTree를 전송한다. CLI는 `.env`를 읽고 기존 환경변수는
덮어쓰지 않는다. LangSmith API 키와 프로젝트도 설정한다. 추적에는 원문/자격증명을 넣지 않고
해시·크기·단계·시간·상태·안전한 오류 코드와 모델이 제공한 토큰 수를 기록한다.
따라서 LangSmith에서 업무 원문/피드백 전문을 볼 수는 없다. 결과는 권한이 있는 DB에서
trace ID로 연결해 확인한다. 비용 계산이나 LangSmith Dataset 업로드 기능은 추가하지 않았다.

## 비교와 검증결과서 기록

두 종류의 데이터를 분리했다.

| 파일 | 건수 | 검증 의미 |
|---|---:|---|
| `samples/feedback/functional-v1.json` | 12 | 모의 MCP·모의 LLM으로 제어 흐름/오류 처리 확인 |
| `samples/feedback/quality-v1.json` | 20 | 실제 모델로 실행 후 사람이 진단·추천의 타당성을 평가할 가상 대화 |

```bash
python -m ai_agents.cli.validation run \
  --dataset samples/feedback/functional-v1.json \
  --target ai_agents.validation.feedback_targets:replay_feedback \
  --output-dir outputs/validation/my-feedback-functional \
  --label feedback-functional --model fixture-model \
  --prompt-version recent-five-feedback-v1 --rubric-version none --data-version synthetic-v1
```

품질 실험은 다음 명령으로 실행한다. `validation` CLI는 `.env`를 자동으로 읽지 않으므로
모델 주소/키와 LangSmith 설정을 실행 환경에 주입한다. 실제 모델 호출이 발생한다.

```bash
python -m ai_agents.cli.validation run \
  --dataset samples/feedback/quality-v1.json \
  --target ai_agents.validation.feedback_targets:model_feedback \
  --output-dir outputs/validation/feedback-quality-v1 \
  --label feedback-quality-v1 --model 배포한모델ID \
  --prompt-version recent-five-feedback-v1 --rubric-version none --data-version synthetic-v1 \
  --model-settings '{"temperature":0,"max_tokens":1800}' --langsmith
```

검증결과서는 `report.csv`의 케이스 ID·기대 결과·실제 결과·판정·trace ID를 가져오면 된다.
`manifest.json`에는 Git commit, dirty, working tree hash, 의존성 lock hash, 모델·프롬프트·기준·
데이터 버전 등을 남긴다. 가상 데이터만 허용하는 기존 검증 runner를 사용한다.
`report.csv`에는 가상 입력/출력 전문이 있으므로 업무 원문을 평가셋에 대입해 실행하지 않는다.

품질 20건은 자동 정답 검사가 없어 **unreviewed**로 남긴다. 다음 항목을 별도 검토표에
기록하고, 합격 기준과 검토자 합의를 정한 뒤 통과율을 산출한다.

- 진단이 실제 대화에 의해 뒷받침되는지, AI 답변 오류를 사용자 책임으로 돌리지 않는지.
- 추천 프롬프트가 구체적이고 실행 가능한지, 없는 사실을 만들어 넣지 않는지.
- 강점·문제점이 적절한지, 문서 부족/확인 필요를 구분하는지.
- 고정 기준이 있는 경우 해당 기준 해석이 타당한지.
- 개인정보/지시문 주입 대응, 출력 실패율, 처리 지연.

검토 기록 권장 열: `case_id, experiment_id, trace_id, 검토자, 근거 타당성,
추천 유용성, 문제 내용, 최종 판정, 재검증 실험 ID`. 자동 checks를 사람이 한 품질 판정으로
바꾸거나 mock 성공률을 LLM 품질 점수로 보고하지 않는다.

팀원이 기준을 수정하면 승인·배포할 기준의 코칭용 JSON을 `FeedbackRubric` 형태로 export하고
`FEEDBACK_RUBRIC_FILE` 경로를 실행 환경에 설정한다. 고정 대화셋은 그대로 두고 파일과
`--rubric-version`만 v1/v2로 바꿔 각각 새 출력 폴더에 실행한다. 모델/프롬프트는 고정한다.
이 파일 override는 실험 target에만 적용되고 Worker는 MCP의 기준을 사용한다.

```bash
python -m ai_agents.cli.validation compare \
  outputs/validation/feedback-quality-v1/experiment.json \
  outputs/validation/feedback-quality-v2/experiment.json \
  --allow-change rubric_version --output outputs/validation/feedback-quality-comparison.json
```

비교기는 데이터셋/입력 해시가 달라지거나 허용하지 않은 설정이 바뀌면 비교를 거부한다.
실행 인자의 모델·버전은 기록용 선언값이므로 실제 환경의 값과 맞춰야 한다. 실제 target 출력의
model/rubric_version/rubric_sha256도 함께 확인한다. 기준 생성 Agent 자체의 생성 품질은
이 실험과 별도로 팀원이 평가해야 한다.

브랜치 이름보다 **실행한 commit과 작업 트리 hash**가 비교 근거다. 머지 후에도 이전 결과와
commit SHA를 보존하면 비교할 수 있다. 미커밋 실행은 dirty로 기록되므로 최종 증빙용 실험은
사용자가 커밋한 뒤 다시 실행하는 것을 권장한다. `outputs/`는 Git 무시 대상이므로 검증 증빙은
프로젝트의 승인된 보관 위치에 별도로 보존한다.

## 다음 협업 순서

1. 백엔드: 완료 5쌍 범위 고정, 권한·5분 제한, 요청+outbox 트랜잭션,
   LIVE_EVALUATE 타입, 큐 선언, DLQ 실패 확정, 본인 조회 API 구현.
2. MCP: 위 도구 3개, 회사/요청 권한, 실제 마스킹, 기준 버전 고정,
   불변 대화 범위, 원자적 완료/실패 및 중복 응답 구현.
3. 기준 담당 팀원: 기존 생성 로직 수정과 버전 관리 유지. 코칭용 기준 export/매핑만 협의.
   중간 피드백 Worker 코드를 수정할 필요는 없다.
4. 프론트: 버튼/429 안내/2~3초 폴링, items·coaching.suggestions 표시,
   60초 초과 안내를 실패 확정으로 처리하지 않기.
5. Agent 담당: 실제 AWS 모델 설정 후 1건 연동 → 실제 큐 재전달/DB 중복 저장 확인 →
   고정 20건 품질 실험 → 기준 변경 비교 → 검증결과서 기록.

현재 실서버 RabbitMQ·PostgreSQL·AWS LLM 연동 및 LangSmith 원격 수집 확인은 하지 않았다.
실행한 검증과 보관 경로는 [이번 검증 기록](recent_five_feedback_verification.md)에 적는다.
