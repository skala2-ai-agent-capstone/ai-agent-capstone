# 내규 업로드 → 평가 기준 초안 생성 Worker

브랜치: `feat/rubric-generation-worker`. 운영 설정 화면의 「평가 기준 설계 에이전트」를 실제로 돌리는 경로다.
기준 생성 로직(`criteria_agent`)은 바꾸지 않고, 큐 수신·내규 버전 고정·`save_rubric` 저장·완료 알림만 붙였다.

```text
화면: 내규 파일(.pdf·.txt) + 버전
백엔드: POST /api/settings/documents
        → business-mcp /internal/documents (조·항 분할, 새 버전 현행, 이전 현행 폐기, rag-mcp 갱신)
        → rubric_generations(대기) + outbox RUBRIC_GENERATE  (한 트랜잭션)
        → rubric_generation.requests
Worker: 유형마다 search_policy(요청에 고정한 내규 버전만) → 기준 생성·검사 → save_rubric(초안)
        → rubric_generation.results (RUBRIC_GENERATED)      실패: retry 또는 dlq(error_code 헤더)
백엔드: results → 완료(drafts), dlq → 실패(error)
화면: GET /api/settings/rubric-generations/latest 폴링 → 초안 검토 → 승인 후 적용(/api/settings/rubrics/approve)
```

초안은 회사·유형마다 하나다. Worker가 저장하면 같은 유형의 기존 초안(관리자 수정 포함)을 덮어쓴다.
현행 전환은 관리자 승인 때만 일어난다.

## 큐 계약

백엔드가 선언한다(`rubric_generation.exchange`, direct). Worker는 큐를 선언하지 않고 없으면 소비 전에 실패한다.

| 큐 | 방향 | 내용 |
| --- | --- | --- |
| `rubric_generation.requests` | 백엔드 → Worker | `RUBRIC_GENERATE` (routing key `rubric_generate`) |
| `rubric_generation.retry` | Worker → (TTL 뒤 requests) | 원래 요청, `attempt + 1` |
| `rubric_generation.dlq` | Worker → 백엔드 | 원래 요청 + `error_code` 헤더 |
| `rubric_generation.results` | Worker → 백엔드 | `RUBRIC_GENERATED` |

정확한 Schema는 `python -m ai_agents.cli.rubric_worker schemas`. 요청 예시는
`samples/integration/rubric-generate-v1.json`이며 백엔드 `EvaluationMessages.RubricGenerate`와 같은 칸이다.

```json
{
  "message_id": "…", "type": "RUBRIC_GENERATED", "generation_id": "RG-…", "company_id": "…",
  "policy_doc_id": "…", "policy_version": "v1.2",
  "drafts": [{"work_type": "quote", "doc_id": "RUB-quote", "version": "v3", "replaced_draft": false}]
}
```

`save_rubric`의 idempotency_key는 `rubric-gen:{generation_id}:{work_type}:{기준 내용 해시}`다.
같은 요청을 다시 받아 같은 내용이면 MCP가 이전 저장 결과를 돌려주고, 다시 생성해 내용이 다르면 같은 초안을 덮어쓴다.
한 유형이라도 실패하면 재시도 때 모든 유형을 처음부터 다시 만든다.

## 오류 코드

| 코드 | 재시도 | 원인 |
| --- | --- | --- |
| `VERSION_CONFLICT` | O | 검색 결과가 요청한 내규 버전이 아님(업로드 직후 rag-mcp 사본이 아직 이전 버전) |
| `MCP_UNAVAILABLE` | O | MCP 연결 실패·일시 오류 |
| `LLM_UNAVAILABLE` | O | 모델 API 연결·시간 초과·HTTP 오류 |
| `RUBRIC_INVALID` | O | 생성 결과가 형식·근거 검사 또는 `save_rubric` 검사(현행 내규 조항·민감정보)를 통과하지 못함 |
| `POLICY_INCOMPLETE` | X | 내규에 기준 생성에 필요한 조항이 없음·출처 불일치 |
| `TOKEN_SCOPE_MISMATCH` | X | 요청 회사와 Worker 토큰 회사가 다름 |
| `TOKEN_REJECTED` | X | MCP가 토큰을 거부(만료·종류) |
| `INVALID_EVENT` | X | 요청 형식 오류. 원본은 복사하지 않고 `generation_id`만 남긴다 |
| `RETRY_EXHAUSTED`, `MCP_REJECTED`, `UNEXPECTED_ERROR` | X | 그 밖 |

재시도는 기본 3회다. 백엔드는 30분 넘게 대기인 건을 `TIMED_OUT` 실패로 보여 주고, 다음 업로드·다시 생성 때 실패로 기록한다.

## 실행

토큰은 회사마다 한 번 발급한다(kind=rubric). Worker는 내부 API를 부르지 않는다.

```bash
curl -s -X POST http://MCP_HOST:8001/internal/rubric-runs \
  -H "X-Internal-Key: $INTERNAL_API_KEY" -H 'Content-Type: application/json' \
  -d '{"company_id": "C-LGCNS-SALES", "ttl_sec": 2592000}'
```

응답 `data.run_token`을 `RUBRIC_MCP_TOKEN`에 넣는다. 회수는 `RUN_TOKEN_SECRET` 교체나 짧은 `ttl_sec` 재발급이다.

```dotenv
RUBRIC_AMQP_URL=amqp://ACCOUNT:PASSWORD@TAILSCALE_IP:5672/
RUBRIC_BUSINESS_MCP_URL=http://TAILSCALE_IP:8001/mcp
RUBRIC_RAG_MCP_URL=http://TAILSCALE_IP:8002/mcp
RUBRIC_MCP_TOKEN=ISSUED_RUBRIC_TOKEN
RUBRIC_LLM_API_KEY=OPENAI_KEY            # 없으면 OPENAI_API_KEY
RUBRIC_LLM_MODEL=gpt-4o-mini             # 없으면 OPENAI_MODEL
# RUBRIC_LLM_BASE_URL=http://TAILSCALE_IP:4000/v1   # 기록 프록시 경유(X-Stage: rubric). 비우면 OpenAI 직접
```

```bash
python -m ai_agents.cli.rubric_worker check-config    # 형식·토큰 종류·회사·만료만. 연결 없음
python -m ai_agents.cli.rubric_worker check-event --event samples/integration/rubric-generate-v1.json
python -m ai_agents.cli.rubric_worker worker --langsmith   # LangSmith 설정이 없으면 --langsmith 제외
```

`once --event 파일`은 큐 없이 한 건을 처리한다. MCP에는 실제로 저장하고, 큐 발행 대신 할 일만 출력한다.

서버 전제: business-mcp에 `/internal/documents`·`/internal/rubric-runs`와 `RAG_MCP_URL`(업로드 뒤 rag-mcp 갱신)이 있어야 하고,
백엔드에 Flyway `V4__rubric_generations.sql`과 `rubric_generation.*` 큐 선언·결과/DLQ 소비자가 있어야 한다.

### compose로 서버에서 켜기

`mcp_servers/docker-compose.app.yml`의 `rubric-worker`(`--profile rubric`)를 쓴다. 기록 프록시를 거치도록
`RUBRIC_LLM_BASE_URL` 기본값이 `http://llm-proxy:4000/v1`이다.

1. 위 `curl`로 회사 토큰을 받아 `.env`의 `RUBRIC_MCP_TOKEN`에 넣는다(값은 출력하지 않는다).
2. llm-proxy `PROXY_KEYS`에 `rubric-worker:<RUBRIC_LLM_API_KEY 값>`을 넣고 `.env`에 `RUBRIC_LLM_API_KEY`를 둔다.
   `OPENAI_STAGES`에 `rubric`이 있어야 OpenAI로 간다.
3. `docker compose --profile rubric up -d --no-deps rubric-worker` 뒤 `docker compose run --rm rubric-worker python -m ai_agents.cli.rubric_worker check-config`.

토큰이 회사 하나 범위라 다른 회사의 업로드는 `TOKEN_SCOPE_MISMATCH`로 실패한다. 회사가 늘면 회사별 Worker를 따로 띄워야 한다.

## 검증 기록 — 2026-10-23

- Agent: 기준 커밋 `f5eba27`에서 `tests/test_rubric_worker.py` 14개 포함 전체 937개 통과
  (4개 건너뜀: 테스트 PostgreSQL 필요), Ruff 통과.
  가짜 MCP(조항별 내규 응답·`save_rubric`)와 가짜 모델로 4개 유형 생성·저장, 고정 버전 불일치 시 저장 전 중단,
  완료 발행 뒤 ack, 회사 불일치·형식 오류·재시도·DLQ 경로를 확인했다.
- 백엔드(aworko-server 로컬 `fix/sync-mcp-schema`): 업로드·생성 상태·다시 생성 API, MCP 클라이언트 HTTP 계약,
  실제 RabbitMQ(Testcontainers)로 요청 발행·results·DLQ 소비를 확인했다.
- 프론트(front-end `feat/admin-rubric-save`): 백엔드 응답 형식의 모의 API로 업로드 → 대기 → 완료(초안 검토)와
  실패 → 다시 생성 화면을 확인했다.
- 실제 business-mcp·rag-mcp·RabbitMQ·OpenAI를 한 번에 잇는 실행은 하지 않았다.
