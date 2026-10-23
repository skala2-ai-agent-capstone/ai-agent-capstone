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
| 완료 | final_evaluation.results | evaluation.results **새 제안** |
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

### 실행 전 백엔드가 준비해야 할 것

1. `evaluation.results` 큐와 소비자를 추가하고 durable·publish confirm 경로 준비.
2. request/retry 큐에는 EVALUATE만 들어오도록 RESUME의 라우팅·기존 적체 메시지 정리.
3. 평가별 토큰 발급 결과를 Worker에 공급. 내부 발급 키와 서명 비밀은 Worker에 전달하지 않음.
4. 완료 소비자는 영수증과 현재 상태를 확인한 후 `확인 대기`로 전환.
   `review_required=true`는 본인 화면의 검토 필요 표시이며 자동 request_review 호출 지시가 아님.
5. 완료 message_id로 중복 제거. 이미 확정·검토 진행 등 더 뒤 상태인 건을 재전달로
   확인 대기로 되돌리지 않음. 완료/실패 알림은 서버 정본과 대조하여 적용.

현재 완료 알림은 초안·코칭 저장 완료만 뜻한다. 전체 리포트 서버 저장이나 최종 확정을
의미하지 않는다. 리포트 전체 저장 계약은 `docs/mcp_final_report_request.md`에 별도로 있다.

## 3. RESUME 경계

본인 확인, 이의 제기 접수, 검토 요청 생성·배정, 검토 결정에 따른 상태 전환은 백엔드
트랜잭션에서 처리한다. 이를 모델 평가 요청으로 바꾸지 않는다. 기존 RESUME 경로는
백엔드에서 처리하거나 별도 상태 전환 소비자로 옮겨야 한다.

이의 제기를 받으면 백엔드가 검토 요청 생성·배정·상태 전환을 일관되게 처리하는 방향이다.
현재 Worker는 검토 필요 플래그만 반환하며 자동으로 팀장에게 검토 요청을 보내지 않는다.
검토 결정이 명시적으로 재평가를 요구한다면 새 평가 실행을 만드는 계약을 별도로 정한다.

**안전한 전환 동작:** backend-v1 Worker가 RESUME을 받으면 모델/도구를 호출하지 않고,
성공 ack나 업무 실패 DLQ로 바꾸지도 않는다. RabbitMQ에서는 원본을 requeue한 뒤
`RESUME_REQUIRES_BACKEND`로 소비를 종료한다. 따라서 미연결 상태가 성공처럼 숨겨지지 않는다.
이는 RESUME 처리 구현이 아니다. 서버 경로가 준비되기 전 운영 소비를 시작하지 않는다.
supervisor가 재시작해도 상태 전환 문제가 해결되지 않으므로 라우팅을 먼저 수정한다.

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
체크포인트를 이어 쓰는 것은 거부한다. 이 파일 어댑터가 자동 발급/갱신을 수행하지는 않는다.

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
기존 백엔드 책임이다. 토큰 파일 방식은 작은 연동 시험용이고 운영 갱신은 provider로 확장한다.

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
확인한 결과가 아니며 완료 소비자와 RESUME 서버 처리 구현은 남아 있다.
