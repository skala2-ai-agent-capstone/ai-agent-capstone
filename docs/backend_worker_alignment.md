# 배포 백엔드·Worker 계약 맞추기

브랜치: `feat/backend-worker-contracts`. 기준은 팀원이 전달한 백엔드 확인 결과와
현재 저장소 Worker 코드다. 백엔드 `fix/sync-mcp-schema` 소스는 이 작업 폴더에 없다.
서버 코드·배포·큐 설정을 수정한 결과가 아니며, 사용자 권한·마스킹 정책은 이번 범위에서 제외한다.
평가 기준 생성 로직도 변경하지 않는다.

## 1. 중간 피드백: 기존 계약 유지

큐는 `live_evaluation.requests`, `live_evaluation.retry`, `live_evaluation.dlq`다.
백엔드가 본문 및 AMQP 속성에 message_id를 넣는 구조와 기존 Worker가 호환된다.
10개 필드 이외의 확장 필드는 합의 전 거부한다.

```bash
python -m ai_agents.cli.feedback schemas
python -m ai_agents.cli.feedback check-event --event samples/integration/live-event-v1.json
python -m ai_agents.cli.feedback check-config
```

`check-event`와 `check-config`는 네트워크/LLM/DB를 호출하지 않는다. 설정 검사에는
값을 출력하지 않고 누락·형식 오류인 변수 이름만 표시한다. 연결 성공이나 토큰의
회사 범위·만료를 확인하는 명령은 아니다. 배포된 프록시를 위한 API 키도 검사 대상이다.

필요한 설정:

```dotenv
FEEDBACK_AMQP_URL=amqp://ACCOUNT:PASSWORD@TAILSCALE_IP:5672/
FEEDBACK_MCP_URL=http://TAILSCALE_IP:8001/mcp
FEEDBACK_MCP_TOKEN=ISSUED_LIVE_WORKER_TOKEN
FEEDBACK_LLM_BASE_URL=http://TAILSCALE_IP:4000/v1
FEEDBACK_LLM_MODEL=cyankiwi/gemma-4-26B-A4B-it-AWQ-4bit
FEEDBACK_LLM_API_KEY=ISSUED_PROXY_KEY
```

실행 전에 담당자와 확인할 조건:

- 해당 컴퓨터의 Tailscale 접근, 테스트 요청과 토큰의 company_id 일치.
- `002_live_feedback_snapshot.sql` 적용, 큐/TTL/DLX와 DLQ 소비자 배포.
- 한 건을 추적하는 테스트 동안 중복 Worker가 같은 큐를 소비하지 않는지 확인.

설정이 준비되면 실제 실행:

```bash
python -m ai_agents.cli.feedback worker --langsmith
```

LangSmith 설정이 없으면 `--langsmith`를 뺀다. 화면에서 요청 한 건을 만들고
`요청 → 수신 → 고정 5쌍 조회 → LLM → MCP 저장 → 폴링 완료`를 확인한다.
샘플 이벤트는 형식 검사용이며 운영 큐에 발행하지 않는다.
원문 기록 프록시에는 기존 `X-Stage: live_evaluate`, `X-Work-Id`,
`X-User-Id: requested_by`를 전달한다.

## 2. 최종 평가: opt-in 백엔드 호환 모드

기존 실행은 기본값 `agent-v1`로 유지한다. 새 `backend-v1` 모드는 다음을 사용한다.

| 항목 | agent-v1 | backend-v1 |
| --- | --- | --- |
| 요청 | final_evaluation.requests | evaluation.requests |
| 재시도 | final_evaluation.retry | evaluation.retry |
| DLQ | final_evaluation.dlq | evaluation.dlq |
| 완료 | final_evaluation.results | evaluation.results |
| 입력 타입 | FINAL_EVALUATE | EVALUATE |
| 완료 타입 | FINAL_EVALUATION_SAVED | FINAL_EVALUATION_SAVED |

`backend-v1` 입력은 다음 8개 필드다. 정확한 Schema는
`python -m ai_agents.cli.final_worker schemas`의 `BackendEvaluationEvent`를 사용한다.

```json
{
  "type": "EVALUATE",
  "message_id": "OUTBOX-EXAMPLE-1",
  "evaluation_id": "EVAL-EXAMPLE",
  "company_id": "COMPANY-EXAMPLE",
  "task_id": "WORK-EXAMPLE",
  "deliverable_version": "v2",
  "rubric_version": "v2",
  "attempt": 0
}
```

내부적으로 기존 최종 평가 실행으로 변환하며, operation_id는 회사·평가 ID·업무·산출물
버전·기준 버전의 해시로 결정한다. message_id 재발급과 attempt 변경은 실행 키에 영향을
주지 않는다. retry/DLQ에는 변환된 내부 이벤트가 아닌 원래 EVALUATE 형식으로 발행한다.
모델 호출과 저장 전에 MCP 정본의 회사·업무·산출물 버전·기준 버전을 메시지와 대조한다.
큐 메시지 자체가 권한 증명은 아니며 기존 MCP 검증을 유지한다.

기존 agent-v1 체크포인트와 backend-v1은 operation_id가 다르다. 처리 중인 평가의 계약을
임의로 바꾸지 않는다. 이미 저장 결과가 있으면 기존 체크포인트로 마무리한 뒤 전환한다.

### 백엔드 쪽 준비 — main에 구현됨

2026-10-26 백엔드 `b943709` 기준으로 아래 연결은 main에 있습니다.
과거 브랜치명 `fix/sync-mcp-schema`는 하단의 당시 검증 기록에서만 사용합니다.
배포 환경에는 해당 변경이 포함된 백엔드 이미지가 실행 중인지 별도로 확인합니다.

1. `evaluation.results` 큐: 백엔드가 durable로 선언(`evaluation.exchange`에 `evaluation.results`
   키로도 바인딩). Worker는 기본 exchange로 큐 이름에 직접 발행한다.
2. RESUME: 백엔드가 더 이상 발행하지 않고, `resume → evaluation.requests` 바인딩도 선언하지 않는다.
   아직 안 나간 RESUME 아웃박스 행은 Flyway V5가 `DISCARDED`로 정리한다(3절).
3. 평가별 토큰: 백엔드가 `/internal/runs`로 발급해 토큰 파일에 둔다(4절). 내부 키·서명 비밀은 Worker에 가지 않는다.
4. 완료 소비자(`EvaluationResultListener`): 평가 행을 잠근 뒤 `draft_revision`·`coaching_revision`이
   DB에 있는지(영수증) 보고, 처리 중(대기·진행 4단계)일 때만 `확인 대기`로 바꾼다.
   `review_required`는 기록만 한다(자동 request_review 아님).
5. 완료 message_id로 중복 제거(`evaluation_result_receipts`, V5). 이미 확정·검토 진행 등 더 뒤 상태면
   되돌리지 않고 `STALE`로 남긴다. 영수증이 안 맞으면 `RECEIPT_MISMATCH`로 남기고 상태는 그대로다.
   형식이 깨진 알림은 버리고, DB 오류는 1초 쉬었다가 다시 큐에 넣는다.

현재 완료 알림은 초안·코칭 저장 완료만 뜻한다. 전체 리포트 서버 저장이나 최종 확정을
의미하지 않는다. 리포트 전체 저장 계약은 `docs/mcp_final_report_request.md`에 별도로 있다.

## 3. RESUME 경계

본인 확인, 이의 제기 접수, 검토 요청 생성·배정, 검토 결정에 따른 상태 전환은 백엔드
트랜잭션에서 처리한다. 이를 모델 평가 요청으로 바꾸지 않는다. 백엔드 구현(미배포):

| 사람의 행동 | 백엔드 처리 (메시지 없음) |
| --- | --- |
| 본인 확인 | 확인 대기 → 확정 |
| 이의 제기 | 이의 항목(item_id나 항목 이름, 최신 초안에 있는 것만)으로 `review_requests`를 만들고 업무 담당 팀의 팀장에게 배정(MCP request_review와 같은 규칙), 확인 대기 → 검토 대기 |
| 검토 keep / hold | 검토 대기 → 확정 / 자료 보류 |
| 검토 override | 팀장 `new_scores`(0~2, 보안 준수는 0·2)로 최신 초안을 복사한 새 판을 저장(고친 항목은 `채점`, 이전 값은 `review_override`에 보존, 근거·코칭은 그대로), 검토 대기 → 확인 대기. AI 재평가는 하지 않음 |

Worker는 검토 필요 플래그만 반환하며 자동으로 팀장에게 검토 요청을 보내지 않는다.
override를 AI 재평가로 바꾸려면 Agent가 검토 결정을 읽고 새 operation_id로 실행하는 계약을 따로 정해야 한다
(지금 같은 범위로 EVALUATE를 다시 보내면 같은 operation_id라 이전 결과가 그대로 돌아온다).

**안전한 전환 동작:** backend-v1 Worker가 RESUME을 받으면 모델/도구를 호출하지 않고,
성공 ack나 업무 실패 DLQ로 바꾸지도 않는다. RabbitMQ에서는 원본을 requeue한 뒤
`RESUME_REQUIRES_BACKEND`로 소비를 종료한다. 따라서 미연결 상태가 성공처럼 숨겨지지 않는다.
이는 RESUME 처리 구현이 아니다. 백엔드가 RESUME을 더 보내지 않으므로 새로 생기지는 않지만,
**이미 `evaluation.requests`에 쌓인 RESUME이 있으면 Worker가 멈춘다**(재시작해도 같음). 5절에서 먼저 비운다.

## 4. 평가별 토큰 공급

`--token-provider module:function`은 유지하며, 로컬 연동용 `--token-file`을 추가했다.
백엔드가 `/internal/runs`에서 평가별로 발급한 토큰과 만료 시각을 아래 파일 형식으로 제공한다.
발급 호출은 백엔드/운영자가 담당하며 Worker가 내부 HTTP API를 직접 호출하지 않는다.

```json
{
  "tokens": {
    "EVAL-EXAMPLE": {
      "token": "ISSUED_EVALUATION_TOKEN",
      "expires_at": "2026-10-23T17:00:00+09:00"
    }
  }
}
```

날짜·토큰은 예시이며 실제 발급값을 사용한다. 파일은 `outputs/final-tokens.json`처럼
Git 제외 위치에 두고 파일 접근 권한을 제한한다. Worker는 매번 다시 읽으므로 운영자는
완성된 파일을 원자적으로 교체할 수 있다. 토큰이 없거나 만료되면 제한된 재시도,
형식이 잘못됐으면 명확한 오류로 처리하며 키·토큰·원문은 오류에 출력하지 않는다.
새 토큰을 발급받아도 기존 평가의 고정 스냅샷을 유지해야 한다. 바뀐 스냅샷으로 기존
체크포인트를 이어 쓰는 것은 거부한다. Worker의 파일 어댑터는 읽기만 한다.

**백엔드 공급(구현됨, 미배포):** `FINAL_TOKEN_FILE`을 주면 백엔드 `FinalTokenSupplier`가 3초마다
처리 중(대기·진행 4단계)인 평가 건을 오래된 순으로 훑어 이 파일을 쓴다.

- 토큰이 없거나 남은 수명이 30분 아래면 `/internal/runs`(`ttl_sec` 7200)로 발급한다. 그 밖에는 다시 발급하지 않는다
  (다시 발급하면 스냅샷도 새로 고정되기 때문). 처리가 끝난 평가 건의 토큰은 뺀다.
- MCP에 닿지 않으면 그 주기의 발급을 멈추고, 아직 유효한 토큰은 남긴다. 거절된 평가 건만 건너뛴다.
- 같은 디렉터리 임시 파일에 쓴 뒤 이름을 바꿔 교체하고, 권한은 600이다(백엔드·Worker 이미지 모두 uid 10001).
  Worker 제한(1MB, 1000개, ID 형식)을 넘는 항목은 넣지 않는다. 내용이 같으면 다시 쓰지 않는다.
- 처리 중인 평가가 없으면 `{"tokens":{}}`를 쓴다. Worker는 빈 목록을 정상으로 읽고 조회는 `TOKEN_NOT_READY`(재시도)다.
- EVALUATE 발행과 따로 돌아서, Worker가 토큰보다 먼저 메시지를 받으면 `TOKEN_NOT_READY` → 재시도 큐(30초) → 다시 받는다.

compose에서는 이름 있는 볼륨 `run-tokens`를 백엔드(`/app/run-tokens`, 쓰기)와
`final-worker`(`/app/outputs/run-tokens`, 읽기 전용)가 같이 쓴다(`mcp_servers/docker-compose.app.yml`).

추가 `.env`:

```dotenv
FINAL_AMQP_URL=amqp://ACCOUNT:PASSWORD@TAILSCALE_IP:5672/
FINAL_BUSINESS_MCP_URL=http://TAILSCALE_IP:8001/mcp
FINAL_RAG_MCP_URL=http://TAILSCALE_IP:8002/mcp
FINAL_LLM_BASE_URL=http://TAILSCALE_IP:4000/v1
FINAL_LLM_MODEL=cyankiwi/gemma-4-26B-A4B-it-AWQ-4bit
FINAL_LLM_API_KEY=ISSUED_PROXY_KEY
```

서버 선행 작업 후 실행:

```bash
python -m ai_agents.cli.final_worker worker \
  --contract backend-v1 --token-file outputs/final-tokens.json \
  --checkpoint-dir outputs/final-checkpoints \
  --trace-dir outputs/final-worker-traces --langsmith
```

worker는 큐를 선언하거나 배포를 바꾸지 않는다. 완료 큐를 포함해 필요한 큐가 없으면
소비 전에 실패한다. 재시도는 동일 체크포인트 볼륨을 사용해야 하며 분산 중복 실행 제어는
기존 백엔드 책임이다.

## 5. 서버에서 최종 평가를 켜기 전 확인

1. 백엔드(이 변경 포함)를 먼저 올린다. Flyway V5가 돌고 `evaluation.results` 큐가 생긴다.
2. `evaluation.requests`에 예전 RESUME이 남아 있는지 본다. 남아 있으면 Worker가 멈춘다.
   ```bash
   docker compose exec rabbitmq rabbitmqctl list_queues name messages consumers
   ```
   메시지가 있으면 관리 화면(127.0.0.1:15672, ssh 터널)의 `evaluation.requests` → Get messages(Ack mode: Nack, requeue)로
   `type`을 확인한다. RESUME만 있으면 비워도 된다(RESUME으로 하려던 상태 전환은 이제 백엔드가 요청 때 바로 한다.
   예전에 이의를 제기했는데 검토 요청이 없는 건은 직원이 다시 이의를 제기해야 한다). EVALUATE가 섞여 있으면 비우지 말고
   RESUME만 골라 내거나, 비운 뒤 그 평가 건들을 팀장 재처리로 다시 보낸다.
3. 이전 브로커에 남은 `resume → evaluation.requests` 바인딩은 무해하다(보내는 곳이 없음). 지우려면
   `rabbitmqadmin delete binding source=evaluation.exchange destination=evaluation.requests destination_type=queue properties_key=resume`.
4. llm-proxy `PROXY_KEYS`에 `final-worker:<FINAL_LLM_API_KEY 값>`을 넣고, `.env`에 `FINAL_LLM_API_KEY`를 둔다.
5. `docker compose --profile final up -d --no-deps final-worker`. 백엔드 로그의 `Final run tokens: … issued=…`와
   Worker 로그를 보고, 화면에서 업무 완료 한 건이 `평가 중 → 확인 대기`로 바뀌는지 확인한다.

## 검증 기록 — 2026-10-23

기준 커밋 `541e978`과 이 브랜치의 미커밋 변경으로 검사했다.

- Ruff 및 `git diff --check` 통과.
- 전체 Agent·프록시 테스트: **929 통과 / 16 건너뜀**. 새 계약 검사 21개 포함.
- 건너뛴 검사는 별도 테스트 PostgreSQL 연결을 요구한다.
- EVALUATE 변환, 재발행 시 고정 실행 ID, 저장 후 완료 알림 유실·재전달,
  원래 retry 메시지 형식, MCP 정본 범위 불일치 시 모델 호출 전 거부를 확인했다.
- RESUME을 모델/업무 DLQ로 보내지 않고 requeue 후 소비 종료하는 경로를 검증했다.
- 평가별 토큰 교체·만료·누락·형식 오류와 비밀값 미출력, 중간 피드백 사전 검사 통과.

JUnit: `outputs/validation/backend-worker-contracts/pytest.xml` (Git 제외).
브로커·MCP·LLM 전송은 대역이다. 배포된 백엔드 코드·실제 큐·GCP 모델과 연결해
확인한 결과가 아니다.

### 백엔드 연결 — 2026-10-23

- 백엔드(aworko-server 로컬, 미커밋) 전체 테스트 143개 통과: Testcontainers Postgres(MCP 001 스키마)·RabbitMQ.
  완료 알림 적용·중복 무시·뒤 상태 유지·영수증 불일치·깨진 알림 폐기, resume 키가 요청 큐에 안 가는 것,
  이의 제기 → 검토 요청 배정, override → 새 초안·확인 대기, 토큰 파일 발급·갱신·정리·MCP 장애·권한 600을 확인했다.
- MCP `/internal/runs`는 HTTP 대역으로만 확인했다. 실제 business-mcp·Agent Worker와 한 큐에서 돌린 결과는 아니다.
- Agent: 빈 토큰 목록 허용 후 `tests/test_backend_worker_contracts.py` 21개 통과.
