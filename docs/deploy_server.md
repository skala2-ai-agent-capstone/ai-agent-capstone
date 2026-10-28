# 서버 배포: MCP + 백엔드 + 평가 Worker

2026-10-26 코드 기준 안내입니다. 기준 생성·중간 피드백·최종·팀 평가 Worker가 구현되어 있습니다.
아래는 배포 구성과 절차이며, 현재 서버에서 어떤 이미지가 실행 중인지 보증하는 목록은 아닙니다.

한 서버(GPU VM)에 Docker Compose로 다음을 같이 올립니다. 모두 한 내부망에서 서비스 이름으로 붙습니다.

| 서비스 | 저장소 | 파일 | 서버 밖 포트 |
| --- | --- | --- | --- |
| postgres · chroma · db-init · business-mcp · rag-mcp | ai-agent-capstone | `mcp_servers/docker-compose.yml` | 8001 · 8002 (Postgres·Chroma는 127.0.0.1) |
| vllm · llm-proxy (`--profile llm`) | ai-agent-capstone | 같은 파일 | 4000 (vLLM은 127.0.0.1) |
| rabbitmq · backend · feedback-worker | aworko-server · ai-agent-capstone | `mcp_servers/docker-compose.app.yml` | 8080 (RabbitMQ는 127.0.0.1) |
| final-worker · team-worker · rubric-worker | ai-agent-capstone | 같은 파일의 `final` · `team` · `rubric` 프로필 | 없음 |

기동 순서는 compose가 지킵니다: postgres → db-init(스키마·적재) → business-mcp → rabbitmq → backend(큐 선언·Flyway) → feedback-worker.

## 0. 서버 준비

- Docker Compose v2.20 이상, GPU 서버면 NVIDIA Container Toolkit
- 두 저장소를 **나란히** 받습니다. aworko-server는 비공개라 서버에 읽기 권한(배포 키 등)이 필요합니다.

```bash
git clone https://github.com/skala2-ai-agent-capstone/ai-agent-capstone.git
git clone https://github.com/skala2-ai-agent-capstone/aworko-server.git
```

- 과거 `fix/sync-mcp-schema` 작업은 백엔드 `main`에 통합됐습니다. 새 환경에서는 `main`을 사용하고,
  배포할 Agent·백엔드 커밋을 함께 기록합니다. 이 안내 대조 기준은 Agent `1c2dedb`, 백엔드 `b943709`입니다.
- Worker 이미지는 시작할 때 git으로 코드 출처를 기록하므로 **일반 clone**에서 빌드합니다(git worktree는 안 됨).
- 이미지 빌드 중 의존성 저장소(Maven Central·PyPI)에 접속하지 못하면 빌드가 실패합니다. 잠시 뒤 다시 빌드합니다.

## 1. `.env` 만들기

```bash
cd ai-agent-capstone/mcp_servers
cp .env.example .env
python3 -c "import secrets;print(secrets.token_urlsafe(32))"   # 비밀값마다 새로 만든다
```

채울 값:

| 값 | 내용 |
| --- | --- |
| `POSTGRES_PASSWORD` `RUN_TOKEN_SECRET` `INTERNAL_API_KEY` `SEED_USER_PASSWORD` | MCP 기존 값. `INTERNAL_API_KEY`는 백엔드도 같이 씀 |
| `VLLM_API_KEY` `PROXY_KEYS` | LLM. `PROXY_KEYS`에 `workspace-backend:<WORKSPACE_LLM_API_KEY 값>`과 `feedback-worker:<FEEDBACK_LLM_API_KEY 값>`을 넣는다 |
| `OPENAI_API_KEY` | 업무 공간 대화 외 단계(`OPENAI_STAGES`: 분류·기준 생성·평가·중간 피드백)를 llm-proxy가 OpenAI로 보낼 때 쓰는 키. 프록시만 안다 |
| `JWT_SECRET` `RABBITMQ_PASSWORD` `WORKSPACE_LLM_API_KEY` `FEEDBACK_LLM_API_KEY` | 백엔드(업무 공간 대화)·Worker |
| `FEEDBACK_MCP_TOKEN` | 2단계에서 발급 |
| `FAULT_MODE=off` | 실사용 서버에서는 장애 주입을 끈다(`case`는 시험용) |
| `COMPOSE_FILE=docker-compose.yml:docker-compose.app.yml` | 주석을 풀면 `docker compose`가 두 파일을 같이 읽는다 |

- `ALLOWED_HOSTS`를 쓰면 `business-mcp:8001`을 꼭 넣습니다(백엔드·Worker가 이 이름으로 부름).
- 설정 점검(컨테이너를 띄우지 않음): `python3 tests/check_deploy.py`

## 2. MCP·LLM 먼저 올리고 Worker 토큰 발급

Worker 토큰은 business-mcp가 떠 있어야 발급되므로, 처음 한 번은 MCP 파일만 줍니다.

```bash
docker compose -f docker-compose.yml --profile llm up -d --build
curl -s -X POST localhost:8001/internal/live-worker-token \
  -H "X-Internal-Key: $(grep ^INTERNAL_API_KEY= .env | cut -d= -f2-)" \
  -H 'Content-Type: application/json' -d '{"company_id":"C-LGCNS-SALES"}'
```

응답의 `data.run_token`을 `.env`의 `FEEDBACK_MCP_TOKEN`에 넣습니다.

## 3. 백엔드·RabbitMQ·Worker까지 올리기

```bash
docker compose --profile llm up -d --build      # COMPOSE_FILE을 풀었으면 두 파일이 같이 적용됨
docker compose ps
```

확인:

```bash
curl -s localhost:8080/actuator/health        # backend {"status":"UP"}
curl -s localhost:8001/health; curl -s localhost:8002/health; curl -s localhost:4000/health
curl -s -X POST localhost:8080/api/auth/login -H 'Content-Type: application/json' \
  -d '{"employee_no":1021,"password":"<SEED_USER_PASSWORD>"}'
docker compose logs -f feedback-worker backend
```

RabbitMQ 관리 화면은 서버 안에만 열려 있습니다: `ssh -L 15672:localhost:15672 <서버>` 뒤 브라우저에서 `localhost:15672`.

## 운영 메모

- **LLM 나눠 쓰기**: 업무 공간 대화(백엔드)만 서버 vLLM, 나머지 단계(중간 피드백 Worker 포함)는 llm-proxy가 OpenAI로 보냅니다(`OPENAI_STAGES`, [llm_proxy/README.md](../llm_proxy/README.md)). 모든 호출이 프록시를 거치므로 `llm_exchanges`에 기록됩니다. 피드백도 vLLM으로 돌리려면 `OPENAI_STAGES`에서 `live_evaluate`를 빼고 `FEEDBACK_LLM_MODEL`을 `SERVED_MODEL_NAME`으로 바꾼 뒤 `docker compose up -d --no-deps llm-proxy feedback-worker`.
- **Worker 토큰**은 회사 1곳 범위이고 기본 30일 뒤 만료됩니다. 만료되거나 `RUN_TOKEN_SECRET`을 바꾸면 2단계 `curl`로 다시 발급하고 `docker compose up -d --no-deps feedback-worker`.
- **db-init**은 `--no-deps` 없이 `up` 할 때마다 다시 돕니다. 시드 업무의 대화·산출물은 시드로 되돌리고(시드 업무에 쌓인 Agent·팀원 LLM 호출 기록도 지워짐), 앱에서 만든 업무는 건드리지 않습니다. 사내 문서도 시드에 있는 버전만 시드 내용으로 다시 넣고, 관리자가 올린 내규와 평가 기준 초안·승인본은 남깁니다(앱이 폐기한 시드 문서는 폐기로 둠). 운영 중인 서버에서 코드만 바꿀 때는 `docker compose up -d --no-deps <서비스>`로 db-init을 건너뜁니다. `--reset`은 앱이 올린 문서와 백엔드 테이블(아웃박스·DLQ 기록·집계 승인·기준 생성 요청·결과 영수증·팀 평가 실패 기록)까지 비우므로 백엔드를 멈추고 RabbitMQ 큐를 비운 뒤 씁니다.
- **기존 DB**에 올릴 때: `db/postgres/002_live_feedback_snapshot.sql`(중간 피드백 스냅샷)과 `003_frontend_upload.sql`(고객 미지정 업무, 고객 자료 원본 파일), `004_team_evaluation.sql`(팀 평가 요청·리포트), `005_team_min_members.sql`(팀 평가 공개 기준 하한 2명)을 적용합니다. db-init이 매번 같이 적용하며, db-init 없이 올릴 때는 `docker exec -i ax-eval-mcp-postgres-1 psql -U axeval -d axeval < db/postgres/003_frontend_upload.sql`(004도 같은 방식). 예전 백엔드의 Flyway V2(역할 제한을 넓히던 임시 파일)가 적용된 DB는 볼륨을 지우고 새로 만듭니다(`docker compose down -v`).
- **포트**: 방화벽에서는 8080(백엔드)만 엽니다. ax-eval-gpu는 VM 태그 `aworko-backend` + 규칙 `allow-aworko-backend-8080`(tcp:8080, 0.0.0.0/0)으로 열려 있습니다(HTTP, 개발용). 8001·8002·4000은 팀원 개발용이며 `/internal/*`은 `X-Internal-Key`로만 보호되므로 팀 IP로 제한합니다.
- **업데이트**: 로컬 변경과 배포 대상 브랜치를 확인한 뒤 두 저장소의 합의한 커밋을 받습니다.
  MCP 의존성이 바뀌면 이미지 재빌드가 필요합니다. 운영 중에는 위 `db-init` 주의사항에 따라
  변경한 서비스만 `--no-deps`로 갱신하고, 필요한 Worker 프로필도 명시합니다.
- **검증 프로필은 운영 DB에서 돌리지 않습니다**: `docker compose --profile test run --rm verify`는 평가 건 상태·초안을 바꾸고 현행 내규·평가 기준을 새 버전으로 바꿉니다(`verify_live_rubric`·`verify_documents`). 새로 만든 DB(`down -v` 뒤)에서만 씁니다. 운영 서버에서 새 내부 API가 떴는지는 데이터를 바꾸지 않는 요청으로 봅니다: `POST /internal/documents`에 `{"company_id":"NOPE"}` → `NOT_FOUND`.

## 추가 Worker 활성화

| 기능 | 구현된 연결 | 실행 전 준비 |
| --- | --- | --- |
| 최종 평가 | `EVALUATE` 수신, MCP 초안·코칭 저장, `evaluation.results` 소비, 평가별 토큰 파일 | `--profile final`, 프록시 키, 백엔드 토큰 공급. [백엔드 연결](backend_worker_alignment.md) 5절 |
| 팀 평가 | 팀 집계 조회·리포트 저장, `TEAM_EVALUATE` 수신, 요청별 토큰 파일 | `--profile team`, 프록시 키, 팀 토큰 공급. [팀 평가](team_evaluation.md)의 서버 실행 절차 |
| 기준 생성 | 내규 업로드 API, `RUBRIC_GENERATE` 수신, 기준 초안 저장·승인 | `--profile rubric`, 회사 범위 rubric 토큰, 프록시 키. [기준 생성 Worker](rubric_generation_worker.md) |

추가 Worker는 `feedback-worker`와 같은 이미지를 사용합니다. 먼저 해당 커밋으로 이미지를 빌드하고,
각 프로필의 환경변수와 토큰을 준비한 뒤 필요한 서비스만 켭니다. `COMPOSE_FILE` 또는 두 `-f` 옵션을
사용해야 앱 구성을 함께 읽습니다. 일반 `up`은 시드 적재기(db-init)를 다시 돌리므로 Worker는 `--no-deps`로 켭니다.

기능 코드는 있으나 배포 여부와 서버 통합 검증은 별도로 확인해야 합니다.
`docker compose ps`, Worker 로그, 요청 한 건의 저장 결과를 함께 확인하세요.
프론트가 다른 출처에서 백엔드에 접근한다면 개발 프록시 또는 해당 배포의 CORS 설정도 확인합니다.
