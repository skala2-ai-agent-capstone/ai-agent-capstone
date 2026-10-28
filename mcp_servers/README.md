# AX 평가 Agent용 MCP 서버 + DB (PostgreSQL · ChromaDB)

이 저장소는 「MCP 구현 명세 — business-mcp · rag-mcp」를 구현한 MCP 서버 2개와, 그 서버가 읽고 쓰는 DB 2개를 담고 있습니다. 데이터는 E2E raw 40건(`data/all_cases.jsonl`)과 시드(`data/seed/`)이며, 모두 가상입니다.

```
           ┌────────────── db-init (적재기, 1회 실행) ──────────────┐
 data/ ──▶ │ load_postgres: 시드·케이스 → 원문+마스킹본, 평가 건 생성  │
           │ load_chroma:   documents(indexed) → 컬렉션 4개         │
           └──────────┬───────────────────────────────┬─────────────┘
                      ▼                               ▼
              PostgreSQL 16 (:5433)            ChromaDB 0.6.3 (:8000)
               업무 데이터·평가 결과·감사          references·policies·rubrics·coaching_examples
                      ▲   ▲                           ▲
       읽기·쓰기      │   │ 장애 주입 계획(읽기)      │ 검색(벡터+키워드)
              business-mcp (:8001)              rag-mcp (:8002)
                      └──────── Agent (Authorization: Bearer <run_token>) ────────┘
```

| 구성 | 역할 |
| --- | --- |
| PostgreSQL (`db/postgres/001_schema.sql`, 43개 테이블 + `messages` 뷰) | 조직·사용자(로그인 계정)·고객·가격·정책 규칙, 사내 문서 원본, 업무 데이터(LLM 호출 기록 `llm_exchanges`·산출물·구간·첨부·이벤트…), 평가 결과(초안·코칭·검토 요청·중간 피드백·진행·중복 키), 감사 기록. 기준 문서: 팀 `db-schema.json`(2026-10-22) |
| ChromaDB | rag-mcp 검색 색인. 컬렉션 `axeval_references`(34)·`axeval_policies`(77)·`axeval_rubrics`(20)·`axeval_coaching_examples`(10). 기밀 문서는 넣지 않음 |
| business-mcp | 최종 평가 조회 13 + 저장 4, 중간 피드백 3(시작·대화 조회·저장), 평가 기준 저장 1. 업무 데이터는 도구 호출마다 Postgres에서 읽고, 저장 도구는 Postgres에 트랜잭션으로 씀 |
| rag-mcp | 읽기 6(현행 기준 조회 `get_current_rubric` 포함). Chroma 벡터 질의 + BM25 키워드 점수 |
| db-init | 적재기 2개를 순서대로 실행 (멱등) |

## 1. 실행

```bash
cp .env.example .env     # POSTGRES_PASSWORD, RUN_TOKEN_SECRET, INTERNAL_API_KEY, SEED_USER_PASSWORD 채우기
docker compose up -d --build
docker compose logs db-init        # {"counts": {...,"llm_exchanges": 237,"messages": 182,"evaluations": 43}} / {"collections": {...}}
curl localhost:8001/health         # {"ok":true,"store":"postgresql","cases":40,"evaluations":43}
curl localhost:8002/health         # {"ok":true,"store":"chromadb","references":34,"policies":77,...}
```

> 이미지는 `python:3.12-slim` 기반입니다. 3.13 이상은 쓸 수 없습니다. `chromadb-client==0.6.3`이 `numpy<2`를 요구하는데 numpy 1.26.x에는 3.13용 휠이 없어서, slim 이미지(컴파일러 없음)에서는 소스 빌드가 실패합니다.
>
> 컨테이너 Postgres는 호스트에 **5433**으로 엽니다(`localhost:5433`). 호스트에 설치된 PostgreSQL(Homebrew 등)이 보통 5432를 쓰고 있어 겹치기 때문입니다. 5433도 쓰고 있으면 `.env`의 `POSTGRES_HOST_PORT`로 바꿉니다. MCP 서버는 compose 내부망(`postgres:5432`)으로 붙으므로 이 포트와 관계없습니다. 직접 들여다볼 때는 `docker compose exec postgres psql -U axeval axeval`로 들어가는 것이 가장 간단합니다.

> 이전 스키마로 만든 볼륨이 남아 있으면 `db-init`이 "이전 스키마로 만든 DB입니다"로 멈춥니다. `docker compose down -v`로 볼륨을 지우고 다시 올리세요.

### GPU 서버(EC2)에서 로컬 LLM까지 함께

`llm` 프로필을 붙이면 vLLM과 기록 프록시(`../llm_proxy`)가 같은 업무 DB를 쓰며 함께 뜹니다.
`.env`에 `VLLM_API_KEY`, `PROXY_KEYS`(및 필요하면 `LLM_MODEL`·`MAX_MODEL_LEN`)를 더 채웁니다.

```bash
docker compose --profile llm up -d --build
docker compose logs -f vllm        # "Application startup complete"까지 10분 이상 걸릴 수 있음
curl localhost:4000/health         # 기록 프록시
```

| 서비스 | 포트 | 열리는 범위 |
| --- | --- | --- |
| business-mcp · rag-mcp | 8001 · 8002 | 모든 인터페이스(보안그룹은 닫고 Tailscale로 접속) |
| llm-proxy | 4000 | `PROXY_BIND`(기본 모든 인터페이스, Tailscale IP 권장) |
| vllm | 8010 → 컨테이너 8000 | 서버 안(127.0.0.1)만 |
| postgres | `POSTGRES_HOST_PORT`(5433) → 컨테이너 5432 | 서버 안(127.0.0.1)만 |
| chroma | `CHROMA_HOST_PORT`(8000) | 서버 안(127.0.0.1)만 (인증 없음) |

프록시 사용법·헤더 규칙은 [llm_proxy/README.md](../llm_proxy/README.md)에 있습니다.

검증은 깨끗한 DB에서 돌립니다. 이 검증은 평가 건 상태와 초안을 바꿉니다. 종단 검증(`verify_e2e`)은 llm-proxy가 떠 있어야 하므로 LLM 프로필과 함께 올립니다.

```bash
python tests/check_deploy.py --online   # 올리기 전 설정 점검(컨테이너 안 띄움): .env.example·포트·GPU·키, vLLM 태그·모델 존재

docker compose down -v
docker compose --profile llm up -d --build       # EC2(GPU). GPU 없는 곳에서는 --profile mockllm (vLLM 대역)
docker compose --profile test run --rm verify
# 검사 861건: 통과 861, 실패 0   (명세 대조, tests/verify_spec.py)
# 검사 42건: 통과 42, 실패 0     (DB 적재·저장, tests/verify_db.py)
# 검사 10건: 통과 10, 실패 0     (앱이 만든 업무: 고객 미지정·원본 파일 첨부, tests/verify_app_work.py)
# 검사 92건: 통과 92, 실패 0     (중간 피드백·평가 기준 초안·관리자 수정·승인·폐기, tests/verify_live_rubric.py)
# 검사 60건: 통과 60, 실패 0     (대화→llm-proxy→DB→MCP 종단, tests/verify_e2e.py)
# 검사 30건: 통과 30, 실패 0     (내규 적재·search_policy 현행 필터, tests/verify_documents.py — 현행 내규를 바꾸므로 마지막)
# 검사 39건: 통과 39, 실패 0     (팀 평가 집계·저장, tests/verify_team.py — 바꾼 평가 상태·초안은 끝나면 되돌림)
```

`verify_e2e`는 시드에 없는 새 업무를 백엔드처럼 만들고, 대화 6턴(일반·스트리밍, 실패 후 재전송 포함)을 실제 llm-proxy로 보낸 뒤, 그 기록만으로 중간 피드백과 최종 평가 도구를 끝까지 돌립니다. 만든 업무는 끝나면 지웁니다. vLLM이 아직 모델을 올리는 중이면 `E2E_LLM_WAIT_SEC`(기본 900초)까지 기다립니다.

## 2. 데이터 적재

`db-init`은 `up` 할 때마다 실행되며 여러 번 돌려도 결과가 같습니다. 사내 문서(`documents`)는 시드에 있는 버전만 교체하므로, 앱에서 올린 내규와 평가 기준 초안·승인본은 남고 앱이 폐기한 시드 문서도 다시 현행이 되지 않습니다.

| 하고 싶은 일 | 방법 |
| --- | --- |
| 데이터 교체 (평가 결과 유지) | `data/`를 바꾼 뒤 `docker compose run --rm db-init`. 업무는 업무 단위로 교체되고 평가 건은 새 `work.completed`만 추가됨 |
| 전부 비우고 다시 적재 | `docker compose run --rm -e LOAD_ARGS=--reset db-init`. 앱에서 올린 문서와 같은 DB의 백엔드 테이블(아래 '백엔드 테이블')도 비워지므로 백엔드를 멈추고 RabbitMQ 큐를 비운 뒤 실행 |
| 서버 재시작 없이 반영 | business-mcp는 업무 데이터를 매번 DB에서 읽으므로 바로 반영됨. 기준 데이터(가격·정책 규칙 등)를 바꿨다면 `POST :8001/internal/reload`. rag-mcp는 컬렉션이 다시 만들어진 것을 스스로 감지해 다시 연결하고, `POST :8002/internal/reload`로도 갱신할 수 있음 |
| zip을 바로 적재 | `python -m db.loader.load_postgres --data e2e_raw_40.zip` (zip 안에서 `seed/`를 찾음) |

> `quote_data.exceptions`가 없는 이전 버전 데이터를 적재하면 경고가 나옵니다. 이 경우 SLA 상향·결제 조건 변경 같은 예외 승인 대상을 계산하지 못해 QT-04·QT-08의 기준값이 명세와 달라집니다. `data/`에는 `exceptions`가 들어간 최신 버전이 들어 있습니다.

### 대화와 사용자 적재 규칙

- **대화**: `messages`는 테이블이 아니라 `llm_exchanges`(기록 프록시가 쓰는 LLM 호출 기록)를 펼친 뷰입니다.
  - 적재기는 `data/llm_exchanges.jsonl`(모의 데이터, 237행)을 그대로 넣습니다. 파일이 없으면 케이스 대화를 [`db/loader/chat_log.py`](db/loader/chat_log.py) 규칙으로 바꿔 넣습니다.
  - 적재가 끝나면 뷰가 원천 대화와 똑같은지 업무마다 확인합니다. 다르면 경고를 냅니다.
  - 대화의 마스킹본은 저장하지 않습니다. 뷰를 읽을 때 `policy_rules`의 마스킹 규칙(`mask_text`)으로 만들며, 결과는 Python `Masker`와 같습니다.
- **사용자**: `users`는 로그인 계정을 겸합니다. 로그인 ID는 사번(`employee_no`), 비밀번호는 `password`이고, 백엔드가 JWT를 발급합니다.
  - 시드 사용자의 사번은 ID의 숫자입니다(`E-1021` → `1021`, `L-3001` → `3001`).
  - 비밀번호는 모두 `.env`의 `SEED_USER_PASSWORD`입니다. 평문으로 저장되므로 실제 쓰는 비밀번호를 넣지 않습니다. 이미 있는 사용자의 비밀번호는 다시 적재해도 바뀌지 않습니다.
  - 역할은 `member` 또는 `team_lead`입니다. 시드의 `team_lead`·`reviewer`는 `team_lead`가 되고, 나머지는 `member`가 됩니다.
  - 검토 요청(`request_review`)의 검토자는 업무 담당자 팀의 `team_lead`입니다. 영업1팀은 `L-3001`이고, 팀장이 없는 팀은 비워 둡니다.
- **앱이 저장하는 업무**: 시드에 없는 업무도 같은 테이블로 평가됩니다(`tests/verify_e2e.py`가 확인). 백엔드가 지킬 것은 다음과 같습니다.
  - 대화는 llm-proxy를 거치면 `llm_exchanges`에 쌓이고 `messages` 뷰로 바로 보입니다. 따로 저장하지 않습니다.
  - `*_masked` 칸(등록 내용·산출물 본문 등)은 `mask_text(원문)`으로 채웁니다. 예: `INSERT … VALUES (…, content, mask_text(content))`.
  - 산출물은 `artifact_versions`에 본문만 넣으면 됩니다. `artifact_spans` 행이 없으면 business-mcp가 시드와 같은 규칙(빈 줄·표 구분선 제외, 표 키 `REQ-01`·`HEADER`, 나머지 `Lnn`)으로 본문을 나눠 근거 ID를 만듭니다([`business_mcp/spans.py`](business_mcp/spans.py)).
  - 견적서 업무의 `compute_reference_quote`는 `quote_data`(상품·수량·할인 구조화 값)가 있어야 합니다. 없으면 `NOT_FOUND`입니다.
  - 고객 없이 만든 업무는 `work_cases.customer_id`를 NULL로 둡니다. 이때 `get_work_case`의 `customer`는 `{customer_id: null, name: null}`이고, 정책 검사는 어느 고객사가 '다른' 고객사인지 정할 수 없어 `V-OTHER-CUSTOMER`만 건너뛰고 `scope.note`에 그렇게 적습니다.
  - 앱에서 올린 고객 자료 원본은 `source_documents.file_name`·`content_type`(MIME)·`size_bytes`·`file_data`에 둡니다. business-mcp는 꺼낸 텍스트(`content`·`content_masked`)만 읽고 원본 칸은 응답에 넣지 않습니다(응답의 `content_type: "data"`는 데이터 표시로, MIME 칸과 이름만 같습니다). `tests/verify_app_work.py`가 확인합니다.
  - 기존 DB는 `db/postgres/003_frontend_upload.sql`(비파괴, 여러 번 실행해도 같음)을 먼저 적용한 뒤 백엔드를 올립니다. 새 DB는 `001_schema.sql`에 포함돼 있습니다.
- **백엔드 테이블**: `outbox_events`·`evaluation_failures`·`aggregation_approvals`(V1), `rubric_generations`(V4), `evaluation_result_receipts`(V5), `team_evaluation_failures`(V6)는 백엔드(aworko-server)의 Flyway가 같은 DB에 만듭니다. 이 스키마에는 넣지 않았습니다.
  - 적재기는 `--reset`일 때만 이 테이블들을 같이 비웁니다(`flyway_backend_history`는 남김). 평가 건 ID(`EVAL-{업무}-{버전}`)는 다시 적재해도 같아서, `outbox_events`가 남으면 이전 메시지가 새로 만든 평가 건으로 발행되고 `evaluation_result_receipts`가 남으면 같은 평가 건의 완료 알림이 중복으로 버려지기 때문입니다. 백엔드에 테이블이 늘면 `db/loader/load_postgres.py`의 `BACKEND_TABLES`에도 넣습니다.

### PostgreSQL에서 볼 만한 테이블

| 묶음 | 테이블 |
| --- | --- |
| 기준 | `companies` `teams` `users` `customers` `products` `price_tables` `price_table_items` `discount_components` `approval_tiers` `exception_rules` `policy_rules` |
| 문서 | `documents`(indexed=false = 기밀) `document_chunks` `rubric_items` |
| 업무 | `work_cases` `source_messages` `source_documents` `attached_references` `llm_exchanges`(→ `messages` 뷰) `artifact_versions` `artifact_spans` `reference_access_logs` `evidence_links` `work_events` `human_actions` `quote_data` `approval_requests` `fault_injections` `access_attempts` `historical_works` |
| 평가 | `evaluations` `evaluation_drafts` `evaluation_coaching` `review_requests` `live_evaluations` `progress_log` `idempotency_keys` `audit_log` |

텍스트 컬럼은 원문(`content`)과 마스킹본(`content_masked`)이 함께 있습니다 (명세 7장 기본안 2). 정책 검사는 원문으로 하고, MCP 응답과 Chroma 색인에는 마스킹본만 나갑니다.

```sql
-- 평가 건별 최신 초안
SELECT e.evaluation_id, e.status, d.revision, jsonb_array_length(d.items) AS items
FROM evaluations e LEFT JOIN LATERAL (
  SELECT * FROM evaluation_drafts WHERE evaluation_id = e.evaluation_id ORDER BY revision DESC LIMIT 1) d ON true;

-- 실행별 도구 호출 감사 기록
SELECT ts, run_id, tool, result_code, input FROM audit_log WHERE evaluation_id = 'EVAL-E2E-QT-08-v2' ORDER BY ts;
```

### ChromaDB 컬렉션

- 문서 ID는 `{doc_id}@{version}#{chunk_id}`로 근거 ID와 같습니다. 루브릭은 항목 하나가 문서 하나(`RUB-quote@v2#quote.3`)이고, 코칭 사례는 사례 하나가 문서 하나입니다.
- metadata로는 `doc_id`, `version`, `doc_key`(`DOC@ver`), `chunk_id`, `clause_id`, `title`, `grade`, `status`, `company_id`, `seq`를 넣었습니다. 목록 값(`evidence_sources` 등)은 JSON 문자열로 저장합니다.
- 회사 범위와 스냅샷 자료 범위는 rag-mcp가 Chroma `where` 조건(`company_id`, `doc_key $in`)으로 강제합니다.
- 임베딩은 클라이언트에서 계산합니다(`common/embedding.py`). 어절과 한글 2-gram을 해시한 512차원 벡터라 모델 다운로드가 필요 없습니다. 의미 검색이 필요하면 이 함수를 다국어 임베딩 모델로 바꾸고 `db-init`을 다시 돌리면 됩니다. 컬렉션 metadata의 `embedding` 값이 서버 쪽 값과 다르면 rag-mcp가 시작을 거부합니다.
- 검색 결과에는 `score`(종합), `keyword_score`(BM25), `vector_score`(코사인)가 같이 옵니다. 키워드 점수가 `RAG_MIN_SCORE`를 넘거나 코사인 유사도가 `RAG_MIN_SIM`을 넘어야 결과로 나오고, 둘 다 미달이면 `no_evidence:true`입니다.

## 3. 평가 실행 흐름

### ① 실행 토큰 받기 (백엔드 역할)

```bash
curl -s -X POST localhost:8001/internal/runs \
  -H "X-Internal-Key: $INTERNAL_API_KEY" -H "Content-Type: application/json" \
  -d '{"evaluation_id":"EVAL-E2E-QT-08-v2"}'
```

응답의 `data.run_token`을 Agent 실행 상태에 넣습니다. `snapshot`에는 `artifact_ref`, `reference_refs`, `conversation_range`, `linked_doc_refs`가 들어 있습니다. 평가 건 ID 형식은 `EVAL-{케이스}-{산출물 버전}`입니다. 선택 필드는 둘입니다. `as_of`는 이 시각 이전의 이의 제기·검토 결정만 보이게 하고, `ttl_sec`은 토큰 유효 시간입니다(기본 3600).

### ② Agent에서 연결하기 (LangGraph + langchain-mcp-adapters)

```python
from langchain_mcp_adapters.client import MultiServerMCPClient

def mcp_client(run_token: str) -> MultiServerMCPClient:
    headers = {"Authorization": f"Bearer {run_token}"}
    return MultiServerMCPClient({
        "business": {"transport": "streamable_http", "url": "http://localhost:8001/mcp", "headers": headers},
        "rag":      {"transport": "streamable_http", "url": "http://localhost:8002/mcp", "headers": headers},
    })
```

도구 결과의 `content[0].text`는 JSON 봉투입니다: `{"ok":true,"data":…,"meta":{"run_id","snapshot"}}` 또는 `{"ok":false,"error":{"code","message","retryable"}}`. 공식 MCP SDK로 붙는 예시는 `tests/harness.py`에 있습니다.

도구별 입력·응답 필드와 실제 응답 예시는 `../docs/mcp_swagger.html`(브라우저로 열기)과 `../docs/mcp_openapi.yaml`(OpenAPI 3.1)에 있습니다.

### ③ 내부 API (`X-Internal-Key` 필요)

| 경로 | 용도 |
| --- | --- |
| `POST :8001/internal/runs` | 최종 평가 실행 토큰 발급(kind=evaluation) |
| `POST :8001/internal/live-evaluations` | 중간 피드백 요청 행 생성(대기). 최근 완료 질문·응답 5쌍 범위와 유형·현행 기준을 고정. 업무 담당자만, 사용자별 5분 1회(`429 RATE_LIMITED`), 완료 쌍이 없으면 400 |
| `GET :8001/internal/live-evaluations/{id}` | 중간 피드백 상태·결과(화면 폴링) |
| `POST :8001/internal/live-worker-token` | 피드백 Worker 토큰 발급(kind=live, 회사 범위, 기본 30일). Worker의 `FEEDBACK_MCP_TOKEN`에 넣음 |
| `POST :8001/internal/rubric-runs` | 평가 기준 작성 실행 토큰 발급(kind=rubric, `company_id`) |
| `POST :8001/internal/team-evaluations` | 팀 평가 요청 행 생성. 이때 확정 평가를 집계해 스냅샷으로 고정. 아래 「팀 평가」 참고 |
| `GET :8001/internal/team-evaluations/{request_id}` | 팀 평가 요청 상태·스냅샷·저장된 리포트(관리자 화면) |
| `POST :8001/internal/team-runs` | 팀 평가 실행 토큰 발급(kind=team, `request_id`) |
| `POST :8001/internal/rubrics/drafts` | 관리자 「수정 초안 만들기」: 현행 기준을 복사한 초안 생성(AI 없이) |
| `POST :8001/internal/rubrics/drafts/items` | 관리자 「항목 저장」: 초안 기준 항목 하나의 칸 일부 수정 |
| `POST :8001/internal/rubrics/approve` | 관리자 승인: 평가 기준 초안 → 현행, 이전 현행 → 폐기. 아래 「평가 기준(규칙) 저장 흐름」 참고 |
| `POST :8001/internal/rubrics/discard` | 관리자 초안 폐기: 평가 기준 초안 삭제(현행 그대로) |
| `POST :8001/internal/documents` | 내규 적재(관리자 업로드). 아래 「내규 적재」 참고 |
| `GET/POST :8001/internal/evaluations` | 평가 건 목록 / `work.completed` 이벤트로 평가 건 생성 (같은 키는 `created:false`) |
| `GET :8001/internal/evaluations/{id}` | 저장된 초안·코칭·검토 요청·진행 이력 |
| `POST :8001/internal/evaluations/{id}/status` | 서버 전용 상태 전환 (`확인 대기`·`확정`·`처리 실패`·`자료 보류`) |
| `POST :8001/internal/reload`, `:8002/internal/reload` | 기준 데이터·색인 사본·장애 주입 계획 다시 읽기 |
| `POST :800{1,2}/internal/faults/reset` | 장애 주입 호출 횟수 초기화 |

응답은 도구와 같은 봉투입니다(`{ok, data}` / `{ok:false, error}`). 오류의 HTTP 상태는 `NOT_FOUND` 404, `FORBIDDEN` 403(키 없음 포함), `RATE_LIMITED` 429, 나머지는 400입니다.

### ④ 중간 피드백·평가 기준 저장 (Agent → MCP → DB)

실행 토큰에 종류(`kind`)가 있고, 종류마다 부를 수 있는 도구가 정해져 있습니다(다른 도구는 `FORBIDDEN`).

| 토큰 | 발급 | 범위 | 쓸 수 있는 도구 |
| --- | --- | --- | --- |
| `evaluation` | `/internal/runs` | 평가 건 1개(스냅샷) | 조회 14개 · 저장 4개, rag 6개 |
| `live` | `/internal/live-worker-token` | 회사(피드백 Worker에 한 번 발급) | `start_live_evaluation`, `get_messages`, `save_live_evaluation`, rag `get_current_rubric`·`search_rubric`·`search_policy`·`search_examples` |
| `rubric` | `/internal/rubric-runs` | 회사 | `save_rubric`, rag `get_current_rubric`·`search_rubric`·`search_policy`·`search_examples` |
| `team` | `/internal/team-runs` | 팀 평가 요청 1개 | `get_team_evaluation_snapshot`, `save_team_evaluation_report` (rag 도구 없음) |

**중간 피드백(최근 질문·응답 5쌍 코칭)**. 계약 정본은 Agent 쪽 [docs/recent_five_feedback.md](../docs/recent_five_feedback.md)이고, 응답 칸은 Agent 계약 모델(모르는 칸 거부)과 같습니다.

기존 DB 업그레이드: `db/postgres/002_live_feedback_snapshot.sql`로 스냅샷 열을 먼저 추가한 뒤
business-mcp를 새 코드로 재배포합니다. compose의 `db-init`은 002·003(고객 미지정 업무, 고객 자료 원본 파일 칸)을 매번 같이 적용합니다. 데이터 초기화는 필요 없습니다.
기존 완료 결과는 유지됩니다. 스냅샷이 없는 이전 대기·생성 중 요청은 새 요청으로 다시 실행합니다.
`get_messages`와 결과 저장 시 인용 검증은 현재 대화 뷰 대신 요청의 마스킹 스냅샷을 사용합니다.

1. 백엔드가 요청 행(`대기`)을 만들고 큐에 `LIVE_EVALUATE`를 발행합니다. `/internal/live-evaluations`는 `conversation_pairs(work_id)` 중 요청 시각까지 성공한 업무 공간 대화를 완료 시각·ID 기준 최신 5쌍으로 선택합니다. 유형과 현행 기준 버전, 마스킹된 대화 스냅샷도 같이 고정합니다. 백엔드가 직접 INSERT한다면 이 계약을 동일하게 구현해야 합니다.
2. Worker가 `start_live_evaluation(live_eval_id)` → `get_messages(live_eval_id, work_id, from_seq, to_seq)` → `save_live_evaluation(live_eval_id, status, items, coaching, model, latency_ms, error)` 순서로 부릅니다.

| 항목 | 규칙 |
| --- | --- |
| 권한 | Worker 토큰은 회사 범위. 도구마다 `live_eval_id`로 요청 행을 찾아 토큰의 회사와 같은지 확인(다르면 `FORBIDDEN`) |
| 범위 | 완료 쌍 = 직원 질문 바로 다음 seq가 AI 답변. 답이 없는 질문(끊김·실패)은 빠짐. `exchange_id`는 그 쌍에 답한 호출(실패 뒤 재전송이면 재전송 호출) |
| `start` | `대기 → 생성 중`. 재전달된 `생성 중`은 같은 문맥, `완료·실패`는 바꾸지 않고 그 상태로 돌려줌(오류 아님). 고정 기준이 있으면 `rubric{doc_id, version, items[{item_id, item, criterion}]}` 포함 |
| `get_messages` | `work_id·from_seq·to_seq`가 고정 범위와 같아야 함. `생성 중`일 때만. 마스킹본, `masking_version`, `complete` |
| `save` (완료) | 진단 1~3개 `{item_id, criterion_id(고정 기준 항목\|null), kind(strength·improvement·needs_check), diagnosis, evidence[{message_id, quote}]}` — 인용은 범위 안 메시지의 실제 문장. `coaching{schema_version:'1', provenance, suggestions[{item_id, target_message_id(직원 질문), next_action, suggested_prompt}]}` — 개선 항목엔 추천 필수 |
| `save` (실패) | `error`는 코드만(예 `MODEL_TIMEOUT`) |
| 중복 | 키 없이 상태로 처리. 이미 `완료·실패`면 덮어쓰지 않고 `{status: 현재, duplicate: true}` |

**평가 기준(규칙) 저장 흐름**: 관리자 화면(운영 설정 > 평가 기준 관리)과 같은 초안 → 승인 순서입니다.
1. 평가 기준 생성 Agent가 `save_rubric`으로 **초안**(`status='초안'`)을 저장합니다. 관리자가 AI 없이 고치려면 「수정 초안 만들기」(`POST :8001/internal/rubrics/drafts`)로 현행을 복사한 초안을 만듭니다. 현행은 바뀌지 않고, 초안은 승인 전까지 어떤 평가에도 쓰이지 않습니다(현행을 고르는 rag `get_current_rubric`·중간 피드백·백엔드 평가 건 생성은 `현행`만 봄). 초안 내용은 `search_rubric(work_type, 초안 버전)`으로 볼 수 있습니다.
2. 관리자는 「항목 저장」(`POST :8001/internal/rubrics/drafts/items`)으로 초안의 항목 하나씩 고칩니다. 보낸 칸만 바뀌고, 고친 항목은 `save_rubric`과 같은 검사를 거칩니다. 「이전 기준으로 되돌리기」는 이전 버전 항목 값을 같은 API로 보내면 됩니다.
3. 관리자가 「승인 후 적용」을 누르면 백엔드가 `POST :8001/internal/rubrics/approve`를 부릅니다. 초안이 `현행`(시행일 = 승인일)이 되고 같은 기준의 이전 `현행`은 `폐기`가 됩니다.
4. 「초안 폐기」는 `POST :8001/internal/rubrics/discard`입니다. 초안을 지우고 현행은 그대로 둡니다.

**저장된 기준으로 최종 평가**: Agent의 `cli.final_evaluation`은 evaluation 토큰으로
`get_evaluation_context(evaluation_id)`를 먼저 호출합니다. 이 읽기 도구는 토큰에 귀속된
평가 건의 업무·산출물·루브릭 버전·상태와 현재 `draft_revision`·`coaching_revision`을 반환합니다.
Agent는 해당 버전의 `search_rubric`을 조회하고 검증 후 `save_evaluation_draft`·`save_coaching`을
호출합니다. 최종 확정 상태는 서버 소유입니다. [Agent 실행 안내](../docs/evaluation_agent.md).

| 검증 | 규칙 |
| --- | --- |
| 항목 | 5개, 순서 고정(문제 정의·지시 품질·결과 검증·보안 준수·결과물 품질). `item_id`는 서버가 `{work_type}.{순번}`으로 붙임 |
| 필수 칸 | `criterion`, `score_2`, `score_1`, `score_0`, `evidence_sources[]`, `related_clauses[]`. 보안 준수의 `score_1`은 `해당 없음` |
| 조항 | `related_clauses`는 현행 내규(`policies`)에 있는 조항만. 승인할 때도 다시 확인(초안 뒤 내규가 바뀌어 조항이 없어졌으면 400) |
| 초안 개수 | 회사·업무 유형마다 하나. 초안이 있으면 `save_rubric`이 같은 버전으로 덮어씀(`replaced_draft:true`, 관리자 수정도 덮어씀), 없으면 새 버전. 「수정 초안 만들기」는 초안이 있으면 `VERSION_CONFLICT` |
| 항목 저장 | body `{company_id, work_type, item(항목 이름), fields{criterion·score_2·score_1·score_0·unobserved·needs_review·evidence_sources[]·related_clauses[] 중 일부}, edited_by?}`. `item_id`·순서는 못 바꿈. 응답에 `changed_fields[]` |
| 동시 수정 | `based_on_version`이 이미 현행이 아니면 `VERSION_CONFLICT`. 같은 회사의 저장·승인·폐기는 한 줄로 처리 |
| 승인·폐기 범위 | body `{company_id, work_types?, approved_by?}`. `work_types`를 빼면 회사의 모든 초안(화면 버튼 하나 = 호출 하나). 대상 초안이 없으면 404 |
| 저장 위치 | Postgres `documents`·`rubric_items`와 Chroma `rubrics`(rag-mcp가 읽음)를 함께 바꿈. Chroma 반영이 실패하면 Postgres도 되돌리고 `TEMPORARY_FAILURE` |
| 기존 평가 | 평가 건은 자기 `rubric_version`을 그대로 쓰므로 영향 없음 |

`save_rubric` 응답은 `{doc_id, version, work_type, status:'초안', based_on_version(저장 때 현행), replaced_draft, items_saved}`, 수정 초안 만들기는 `{created[{work_type, doc_id, version, based_on_version}]}`, 항목 저장은 `{work_type, doc_id, version, item{...}, changed_fields[]}`, 승인은 `{approved[{work_type, doc_id, version, retired_versions[]}]}`, 폐기는 `{discarded[{work_type, doc_id, version}]}`입니다. 폐기한 초안의 버전 번호는 다음 초안이 다시 씁니다(어떤 평가 건도 초안을 가리키지 않음).

**팀 평가(팀 집계 코칭)**. 계약 정본은 Agent 쪽 [docs/team_evaluation.md](../docs/team_evaluation.md)이고, 응답 칸은 Agent 계약 모델(`TeamSnapshot`·`TeamSaveReceipt`, 모르는 칸 거부)과 같습니다. 기존 DB는 `db/postgres/004_team_evaluation.sql`(비파괴, 여러 번 실행해도 같음)을 적용한 뒤 business-mcp를 재시작합니다.

1. 백엔드가 관리자 권한과 공개 정책을 확인한 뒤 `POST :8001/internal/team-evaluations`를 부릅니다. body `{company_id, team_id, period_start, period_end(미포함), release_approved, request_id?, requested_by?, min_members?}`. business-mcp가 이때 집계를 한 번 계산해 `team_evaluations.snapshot`으로 고정합니다.
2. `POST :8001/internal/team-runs {request_id}`로 받은 team 토큰으로 팀 Agent가 `get_team_evaluation_snapshot(request_id)` → `save_team_evaluation_report(request_id, snapshot_id, idempotency_key, report_sha256, report)`를 부릅니다.
3. 관리자 화면은 `GET :8001/internal/team-evaluations/{request_id}`로 상태(`대기`·`완료`)와 리포트를 읽습니다.

| 항목 | 규칙 |
| --- | --- |
| 대상 | 팀 업무 중 `work_cases.completed_at`이 `[period_start, period_end)`(KST)인 업무마다 최신 `확정` 평가 하나. 확정 전·기간 밖·다른 팀은 세지 않음 |
| 그룹 | 업무 유형·평가 기준 버전·기준 내용 해시(`rubric_sha256`)별. 기준 항목은 이름으로 공통 5개 항목에 맞추고, 맞지 않는 기준·초안이 섞이면 요청 생성이 400 |
| 집계 | 최신 초안에서 `채점`이고 점수가 유효한 항목만 `score_sum`·`scored_evaluations`에 넣음(미관측·검토 필요 제외). 구성원 수는 서로 다른 담당자 수 |
| 공개 | `min_members` = max(2, 회사 `min_aggregation_size`, 요청 값). 구성원이 그보다 적은 그룹은 빼고, 항목 관측 구성원이 적으면 `statistics: null`. `release_approved:false`면 `groups: []` |
| 식별자 | Agent에는 팀 코드 대신 회사·팀으로 만든 고정 UUID(`team_id`). 이름·평가 건 ID·개별 점수는 스냅샷에 없음(쓴 평가 건 ID는 `source_evaluation_ids`에만) |
| 저장 검증 | `report_sha256` = report의 키 정렬 JSON SHA-256. 요청·스냅샷 ID·`snapshot_sha256`·기간이 고정값과 같고, `metrics`는 스냅샷으로 다시 계산한 값과 같아야 함. 강점/개선 지표의 band, 개선 활동과 항목의 짝, 문장의 민감정보 패턴도 확인 |
| 중복 | 요청 하나에 리포트 하나. 같은 키·같은 내용은 처음 영수증(`meta.duplicate`), 다른 내용은 `VERSION_CONFLICT`. 다시 만들려면 새 요청 |

문장(observation·summary 등)은 형식·길이·민감정보만 확인하고 문구는 대조하지 않습니다. Agent가 코드로 만든 문장이며 수치는 `metrics`로 검증합니다.

**내규 적재 (백엔드 → `POST :8001/internal/documents`)**: 관리자가 「운영 설정」에서 올린 내규 파일을 새 버전으로 저장하고 바로 `현행`으로 바꿉니다. 이전 `현행`은 `폐기`로 바꾸고 구간은 남깁니다(지난 평가의 근거 ID가 가리킴). 백엔드는 파일을 받아 이 API에 넘기기만 하고, 저장은 business-mcp가 합니다.

```json
{"company_id": "C-LGCNS-SALES", "version": "v1.1", "source_file": "영업_AI활용지침_v1.1.pdf",
 "content_base64": "<파일 base64>", "uploaded_by": "AD-4001",
 "doc_id": "생략하면 현행 내규", "title": "생략하면 현행 내규", "grade": "사내|대외비", "effective_from": "YYYY-MM-DD(생략하면 오늘)"}
```

| 항목 | 규칙 |
| --- | --- |
| 파일 | PDF(30MB·100쪽 이하, 텍스트 추출 가능) 또는 UTF-8 TXT. 그 밖의 형식은 400 |
| 구간 | `제N조 (제목)`·`①`·`별표 N.`·`부칙`으로 나눔. 평가 기준 생성 Agent(`criteria_agent/policy.py`)와 같은 규칙이고 `common/policy_doc.py`에 복사돼 있음(루트 `tests/test_policy_ingest_parity.py`가 대조) |
| 필수 조항 | Agent가 읽는 조항(`AGENT_REQUIRED_CLAUSES`)과 회사의 현행 평가 기준이 인용하는 조항이 모두 있어야 함. 빠지면 400과 빠진 조항 목록 |
| 저장 위치 | Postgres `documents`·`document_chunks`와 Chroma `policies`(마스킹본, 이전 버전은 메타데이터만 `폐기`)를 함께 바꿈. Chroma가 실패하면 Postgres도 되돌리고 `TEMPORARY_FAILURE` |
| 재시도 | 같은 `(doc_id, version)`에 같은 내용이면 `duplicate:true`로 성공, 다른 내용이면 `VERSION_CONFLICT` |
| 검색 반영 | 저장 뒤 `RAG_MCP_URL`의 rag-mcp `/internal/reload`를 부름. 실패하면 저장은 유지하고 `rag_reloaded:false`(rag-mcp reload만 다시 부르면 됨) |
| 응답 | `{doc_id, version, status, title, chunks, clauses, retired_docs[], diff{base, added[], removed[], changed[]}\|null, duplicate, rag_reloaded}`. `diff`는 직전 현행과 구간 단위 대조(공백·줄바꿈 무시) |

rag-mcp `search_policy`는 `현행` 버전 구간만 돌려줍니다. Agent는 한 내규의 한 버전만 받으므로(`PolicyBundle`), 이전 버전이 섞이면 기준 생성이 멈추기 때문입니다. 이전 버전 구간은 `get_reference(doc_id, version)`로 그대로 열립니다.

최종 평가·평가 기준 저장 도구는 `idempotency_key`가 필수이고, 같은 키는 기존 결과를 돌려줍니다(`meta.duplicate`). 키는 평가 건(평가 기준은 회사) 범위이고 권한을 확인한 뒤에 찾으므로, 다른 평가 건에서 같은 키를 써도 남의 결과가 나가지 않습니다. 중간 피드백은 키 없이 상태로 중복을 처리합니다. 호출은 감사 기록(`audit_log`)에 남습니다.

## 4. 명세를 구현하면서 정한 것

| 항목 | 구현 |
| --- | --- |
| 실행 토큰 | `Authorization: Bearer`(또는 `X-Run-Token`). 두 서버가 `RUN_TOKEN_SECRET`의 HMAC으로 검증. 서명 오류·만료·토큰 없음은 `FORBIDDEN` |
| 범위 밖 업무 | 토큰의 업무가 아니면 존재 여부와 관계없이 `FORBIDDEN`. 연결 업무의 산출물은 조회 가능 |
| 대화 범위 밖 요청 (7장-8) | `VERSION_CONFLICT`로 통일 (명세 문서도 수정함) |
| 이전 버전 조회 | 스냅샷 버전과 그 이전 버전(AI 초안 비교용)을 허용. 이후 버전은 `VERSION_CONFLICT` |
| 고객 첨부 | 스냅샷 저장 시각 이후에 올라온 첨부는 보이지 않음 |
| 근거 ID 추가 | `{work_id}:source_message[:Lnn]`, `{work_id}:ACC-nn`, `{work_id}:reference_quote`, `{work_id}:policy_check`, `{doc_id}@{version}` (명세 근거 ID 표에도 반영) |
| 코칭 항목 | `{item_id, kind(강점·보완점·기록 안내), target_span_id, text}` |
| 중복 저장 | 같은 키는 `ok:true` + `meta.duplicate:true`로 기존 결과를 돌려줌. 키 확인과 저장을 한 트랜잭션에서 처리 |
| 검토 요청 후 상태 | 서버가 `검토 대기`로 바꾸고 업무 담당자 팀의 `team_lead`를 검토자로 배정 |
| 장애 주입 | `FAULT_MODE=case`면 `fault_injections` 테이블의 계획을 실행·도구별 호출 횟수로 적용 |

## 5. 검증 결과

처음에는 실제 PostgreSQL 16과 psycopg 3.2.3으로 검증하면서 ChromaDB 자리에 `tests/fake_chroma.py`를 썼습니다. 이 대역은 chromadb Collection과 같은 메서드를 갖고 있고, 같은 제약(metadata 타입, `where` 연산자 규칙)을 걸어 둔 것입니다. 이후 2026-10-21에 Docker Compose 전체 스택(`python:3.12-slim` 이미지, `postgres:16-alpine`, 실제 `chromadb/chroma:0.6.3`)으로 `verify`를 다시 돌렸고, 아래 결과가 같게 나왔습니다.

- **명세 대조 861건 통과:** 평가 건 43건 전부에서 조회 도구를 돌렸고, 응답의 근거 ID로 초안을 저장해 봤습니다. 정책 위반 9건, 기준 견적 10건, 장애 주입 3건, 6.3 확인 항목, 저장 검증이 모두 기대대로 나왔습니다.
- **DB 검증 42건 통과:** 원본과 행 수가 일치합니다. 마스킹본에는 민감정보가 없고 원문은 보존돼 있습니다. Chroma에는 기밀 문서가 없고, Chroma ID는 Postgres 구간 ID와 일치합니다. 초안·코칭·검토 요청·진행·중복 키·감사 기록이 모두 저장됩니다.
- **재시작 시 보존:** 서버를 재시작해도 초안과 중복 키가 남아 있고, 재시작 전 키로 다시 저장하면 `duplicate`가 나옵니다.
- **적재기 재실행:** 평가 결과가 유지됩니다.
- **DB 직접 수정:** Postgres를 직접 고친 내용이 재시작 없이 다음 호출에 반영됩니다.
- **EC2와 같은 조건 (2026-10-22):** 저장소를 새로 복사하고 `.env.example`로 `.env`를 만든 뒤, EC2와 같은 x86_64(`linux/amd64`) 이미지로 전체 스택을 올렸습니다(`--profile mockllm`, vLLM 자리에 `tests/mock_llm.py`). `verify` 4종 1,004건이 모두 통과했습니다. GPU에서 실제 vLLM·Gemma를 띄우는 부분은 이 환경에서 확인하지 못했고, 설정은 `check_deploy.py`로만 확인했습니다.
- **내규 적재 (verify_documents 30건, 2026-10-23):** 시드 내규 구간으로 만든 PDF·TXT를 새 버전으로 올려 거절 경우(키·형식·필수 조항 누락), 현행·폐기 전환, 구간 diff, `search_policy`가 현행 한 버전만 돌려주는지, 이전 버전 `get_reference`, 재전송·충돌, 초안이 인용한 조항이 빠진 내규를 올린 뒤의 승인 거절을 확인했습니다. 로컬 `postgres:16-alpine`·`chromadb/chroma:0.6.3`과 venv의 두 서버로 돌렸고, 같은 스택에서 기존 `verify_spec`·`verify_db`·`verify_live_rubric`도 그대로 통과했습니다. Chroma를 멈춘 채 올리면 `TEMPORARY_FAILURE`이고 Postgres에 아무것도 남지 않는 것도 손으로 확인했습니다.
- **평가 기준 초안·승인 (verify_live_rubric 중 기준 부분, 2026-10-23):** 초안 저장 뒤에도 현행 유지, 초안 덮어쓰기, 승인(현행·폐기 전환), 승인 대상 없음 404, 초안 폐기(Postgres·Chroma 모두 삭제), 관리자 수정 초안 만들기(현행 복사·초안 중복 거절)와 항목 저장(바꾼 칸만 반영, 잘못된 칸·빈 칸·없는 조항 거절) 뒤 승인까지 확인했습니다. 모든 기준 행의 Postgres 상태와 Chroma 메타데이터가 일치했고, Chroma를 멈춘 채 승인하면 `TEMPORARY_FAILURE`로 초안·현행이 그대로인 것도 손으로 확인했습니다.
- **앱 경로만으로 평가 (verify_e2e 60건):** 프록시 기록 → `messages` 뷰(실패 뒤 재전송 질문 중복 없음, 마스킹) → 중간 피드백(최근 완료 5쌍·Worker 토큰·재전송 질문의 쌍 처리·담당자 확인·5분 제한) → 산출물 본문만 저장 → 평가 건 생성 → 조회·정책 검사·diff·초안 저장까지 통과합니다. 산출물 구간을 본문에서 나누는 처리가 없으면 이 중 6건이 실패합니다(정책 위반 위치를 근거로 인용할 수 없어 초안 저장이 거부됨).

## 6. Docker 없이 실행 (로컬 개발)

Python 3.10~3.12가 필요합니다(`mcp` 2.3.0은 3.10 이상, numpy 1.26.x는 3.12까지 휠 제공). 시스템 Python이 3.13 이상이면 `python3.12 -m venv .venv`로 가상환경을 만들어 쓰세요.

```bash
pip install -r requirements.txt
export DATABASE_URL=postgresql://user:pw@localhost:5432/axeval RUN_TOKEN_SECRET=... INTERNAL_API_KEY=... SEED_USER_PASSWORD=...
export CHROMA_HOST=localhost CHROMA_PORT=8000          # 또는 Chroma 없이: export CHROMA_FAKE_PATH=./chroma.json
python -m db.loader.load_postgres --data data --schema db/postgres/001_schema.sql \
  --schema db/postgres/002_live_feedback_snapshot.sql --schema db/postgres/003_frontend_upload.sql \
  --schema db/postgres/004_team_evaluation.sql --reset
python -m db.loader.load_chroma
python -m business_mcp.server &   # :8001
python -m rag_mcp.server &        # :8002
python tests/verify_spec.py && python tests/verify_db.py && python tests/verify_app_work.py
```

## 7. 구성

```
common/          실행 토큰, 응답 봉투·오류, 마스킹, 장애 주입, 감사, 임베딩, Chroma 연결, policy_doc.py(내규 조·항 분할)
business_mcp/    pg.py(DB 접근) store.py(조회·근거 ID 풀·diff) spans.py(산출물 구간 나누기) policy.py(정책 검사) quote.py(기준 견적)
                 writes.py(최종 평가 저장) live.py(중간 피드백) rubrics.py(평가 기준 저장) documents.py(내규 적재) team.py(팀 평가)
                 checks.py(평가 항목·코칭 검증, 공용) idempotency.py(중복 키, 공용) server.py(도구·내부 API)
rag_mcp/         index.py(BM25) server.py(Chroma 하이브리드 검색)
db/postgres/     001_schema.sql
db/loader/       load_postgres.py, load_chroma.py
data/            seed/ + all_cases.jsonl
tests/           harness.py, verify_spec.py, verify_db.py, verify_live_rubric.py, verify_e2e.py(대화→프록시→DB→MCP),
                 verify_documents.py(내규 적재), verify_team.py(팀 평가), policy_pdf.py(검증용 내규 PDF), verify_app_work.py(앱이 만든 업무 모양),
                 check_deploy.py(배포 설정 점검), mock_llm.py(vLLM 대역), fake_chroma.py
```

## 8. 제한

- business-mcp는 연결 하나를 잠금으로 공유합니다. 평가를 동시에 많이 돌릴 때는 `psycopg_pool`로 바꾸는 것을 권합니다.
- rag-mcp의 감사 기록은 표준 출력(JSON 한 줄)에만 남습니다. business-mcp의 감사 기록은 `audit_log` 테이블에 저장됩니다.
- `ALLOWED_HOSTS`를 비우면 DNS 리바인딩 보호가 꺼집니다. 내부망 전용으로 쓰는 것을 전제로 한 설정이며, 외부에 노출할 때는 반드시 채웁니다. Postgres(5433)·Chroma(8000)·vLLM(8010) 포트는 서버 안(127.0.0.1)에만 열립니다.
