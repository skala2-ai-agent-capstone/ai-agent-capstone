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
| PostgreSQL (`db/postgres/001_schema.sql`, 42개 테이블) | 조직·고객·가격·정책 규칙, 사내 문서 원본, 업무 데이터(대화·산출물·구간·첨부·이벤트…), 평가 결과(초안·코칭·검토 요청·진행·중복 키), 감사 기록 |
| ChromaDB | rag-mcp 검색 색인. 컬렉션 `axeval_references`(34)·`axeval_policies`(77)·`axeval_rubrics`(20)·`axeval_coaching_examples`(10). 기밀 문서는 넣지 않음 |
| business-mcp | 조회 13 + 저장 4. 업무 데이터는 도구 호출마다 Postgres에서 읽고, 저장 도구는 Postgres에 트랜잭션으로 씀 |
| rag-mcp | 읽기 5. Chroma 벡터 질의 + BM25 키워드 점수 |
| db-init | 적재기 2개를 순서대로 실행 (멱등) |

## 1. 실행

```bash
cp .env.example .env     # POSTGRES_PASSWORD, RUN_TOKEN_SECRET, INTERNAL_API_KEY 채우기
docker compose up -d --build
docker compose logs db-init        # {"counts": {...,"evaluations": 43}} / {"collections": {...}}
curl localhost:8001/health         # {"ok":true,"store":"postgresql","cases":40,"evaluations":43}
curl localhost:8002/health         # {"ok":true,"store":"chromadb","references":34,"policies":77,...}
```

> 이미지는 `python:3.12-slim` 기반입니다. 3.13 이상은 쓸 수 없습니다. `chromadb-client==0.6.3`이 `numpy<2`를 요구하는데 numpy 1.26.x에는 3.13용 휠이 없어서, slim 이미지(컴파일러 없음)에서는 소스 빌드가 실패합니다.
>
> 컨테이너 Postgres는 호스트에 **5433**으로 엽니다(`localhost:5433`). 호스트에 설치된 PostgreSQL(Homebrew 등)이 보통 5432를 쓰고 있어 겹치기 때문입니다. MCP 서버는 compose 내부망(`postgres:5432`)으로 붙으므로 이 포트와 관계없습니다. 직접 들여다볼 때는 `docker compose exec postgres psql -U axeval axeval`로 들어가는 것이 가장 간단합니다.

검증은 깨끗한 DB에서 돌립니다. 이 검증은 평가 건 상태와 초안을 바꿉니다.

```bash
docker compose down -v && docker compose up -d --build
docker compose --profile test run --rm verify
# 검사 861건: 통과 861, 실패 0   (명세 대조, tests/verify_spec.py)
# 검사 42건: 통과 42, 실패 0     (DB 적재·저장, tests/verify_db.py)
```

## 2. 데이터 적재

`db-init`은 `up` 할 때마다 실행되며 여러 번 돌려도 결과가 같습니다.

| 하고 싶은 일 | 방법 |
| --- | --- |
| 데이터 교체 (평가 결과 유지) | `data/`를 바꾼 뒤 `docker compose run --rm db-init`. 업무는 업무 단위로 교체되고 평가 건은 새 `work.completed`만 추가됨 |
| 전부 비우고 다시 적재 | `docker compose run --rm -e LOAD_ARGS=--reset db-init` |
| 서버 재시작 없이 반영 | business-mcp는 업무 데이터를 매번 DB에서 읽으므로 바로 반영됨. 기준 데이터(가격·정책 규칙 등)를 바꿨다면 `POST :8001/internal/reload`. rag-mcp는 컬렉션이 다시 만들어진 것을 스스로 감지해 다시 연결하고, `POST :8002/internal/reload`로도 갱신할 수 있음 |
| zip을 바로 적재 | `python -m db.loader.load_postgres --data e2e_raw_40.zip` (zip 안에서 `seed/`를 찾음) |

> `quote_data.exceptions`가 없는 이전 버전 데이터를 적재하면 경고가 나옵니다. 이 경우 SLA 상향·결제 조건 변경 같은 예외 승인 대상을 계산하지 못해 QT-04·QT-08의 기준값이 명세와 달라집니다. `data/`에는 `exceptions`가 들어간 최신 버전이 들어 있습니다.

### PostgreSQL에서 볼 만한 테이블

| 묶음 | 테이블 |
| --- | --- |
| 기준 | `companies` `teams` `users` `customers` `products` `price_tables` `price_table_items` `discount_components` `approval_tiers` `exception_rules` `policy_rules` |
| 문서 | `documents`(indexed=false = 기밀) `document_chunks` `rubric_items` |
| 업무 | `work_cases` `source_messages` `source_documents` `attached_references` `messages` `artifact_versions` `artifact_spans` `reference_access_logs` `evidence_links` `work_events` `human_actions` `quote_data` `approval_requests` `fault_injections` `access_attempts` `historical_works` |
| 평가 | `evaluations` `evaluation_drafts` `evaluation_coaching` `review_requests` `progress_log` `idempotency_keys` `audit_log` |

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

### ③ 내부 API (`X-Internal-Key` 필요)

| 경로 | 용도 |
| --- | --- |
| `POST :8001/internal/runs` | 실행 토큰 발급 |
| `GET/POST :8001/internal/evaluations` | 평가 건 목록 / `work.completed` 이벤트로 평가 건 생성 (같은 키는 `created:false`) |
| `GET :8001/internal/evaluations/{id}` | 저장된 초안·코칭·검토 요청·진행 이력 |
| `POST :8001/internal/evaluations/{id}/status` | 서버 전용 상태 전환 (`확인 대기`·`확정`·`처리 실패`·`자료 보류`) |
| `POST :8001/internal/reload`, `:8002/internal/reload` | 기준 데이터·색인 사본·장애 주입 계획 다시 읽기 |
| `POST :800{1,2}/internal/faults/reset` | 장애 주입 호출 횟수 초기화 |

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
| 검토 요청 후 상태 | 서버가 `검토 대기`로 바꾸고 `reviewer` 역할 사용자를 배정 |
| 장애 주입 | `FAULT_MODE=case`면 `fault_injections` 테이블의 계획을 실행·도구별 호출 횟수로 적용 |

## 5. 검증 결과

처음에는 실제 PostgreSQL 16과 psycopg 3.2.3으로 검증하면서 ChromaDB 자리에 `tests/fake_chroma.py`를 썼습니다. 이 대역은 chromadb Collection과 같은 메서드를 갖고 있고, 같은 제약(metadata 타입, `where` 연산자 규칙)을 걸어 둔 것입니다. 이후 2026-10-21에 Docker Compose 전체 스택(`python:3.12-slim` 이미지, `postgres:16-alpine`, 실제 `chromadb/chroma:0.6.3`)으로 `verify`를 다시 돌렸고, 아래 결과가 같게 나왔습니다.

- **명세 대조 861건 통과:** 평가 건 43건 전부에서 조회 도구를 돌렸고, 응답의 근거 ID로 초안을 저장해 봤습니다. 정책 위반 9건, 기준 견적 10건, 장애 주입 3건, 6.3 확인 항목, 저장 검증이 모두 기대대로 나왔습니다.
- **DB 검증 42건 통과:** 원본과 행 수가 일치합니다. 마스킹본에는 민감정보가 없고 원문은 보존돼 있습니다. Chroma에는 기밀 문서가 없고, Chroma ID는 Postgres 구간 ID와 일치합니다. 초안·코칭·검토 요청·진행·중복 키·감사 기록이 모두 저장됩니다.
- **재시작 시 보존:** 서버를 재시작해도 초안과 중복 키가 남아 있고, 재시작 전 키로 다시 저장하면 `duplicate`가 나옵니다.
- **적재기 재실행:** 평가 결과가 유지됩니다.
- **DB 직접 수정:** Postgres를 직접 고친 내용이 재시작 없이 다음 호출에 반영됩니다.

## 6. Docker 없이 실행 (로컬 개발)

Python 3.10~3.12가 필요합니다(`mcp` 2.3.0은 3.10 이상, numpy 1.26.x는 3.12까지 휠 제공). 시스템 Python이 3.13 이상이면 `python3.12 -m venv .venv`로 가상환경을 만들어 쓰세요.

```bash
pip install -r requirements.txt
export DATABASE_URL=postgresql://user:pw@localhost:5432/axeval RUN_TOKEN_SECRET=... INTERNAL_API_KEY=...
export CHROMA_HOST=localhost CHROMA_PORT=8000          # 또는 Chroma 없이: export CHROMA_FAKE_PATH=./chroma.json
python -m db.loader.load_postgres --data data --schema db/postgres/001_schema.sql --reset
python -m db.loader.load_chroma
python -m business_mcp.server &   # :8001
python -m rag_mcp.server &        # :8002
python tests/verify_spec.py && python tests/verify_db.py
```

## 7. 구성

```
common/          실행 토큰, 응답 봉투·오류, 마스킹, 장애 주입, 감사, 임베딩, Chroma 연결
business_mcp/    pg.py(DB 접근) store.py(조회·근거 ID 풀·diff) policy.py(정책 검사) quote.py(기준 견적) writes.py(저장 도구) server.py
rag_mcp/         index.py(BM25) server.py(Chroma 하이브리드 검색)
db/postgres/     001_schema.sql
db/loader/       load_postgres.py, load_chroma.py
data/            seed/ + all_cases.jsonl
tests/           harness.py, verify_spec.py, verify_db.py, fake_chroma.py
```

## 8. 제한

- business-mcp는 연결 하나를 잠금으로 공유합니다. 평가를 동시에 많이 돌릴 때는 `psycopg_pool`로 바꾸는 것을 권합니다.
- rag-mcp의 감사 기록은 표준 출력(JSON 한 줄)에만 남습니다. business-mcp의 감사 기록은 `audit_log` 테이블에 저장됩니다.
- `ALLOWED_HOSTS`를 비우면 DNS 리바인딩 보호가 꺼집니다. 내부망 전용으로 쓰는 것을 전제로 한 설정이며, 외부에 노출할 때는 반드시 채웁니다. Postgres(5433)·Chroma(8000) 포트도 개발 편의로 열어 둔 것이니 운영에서는 닫아 주세요.
