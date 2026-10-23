# 서버 배포: MCP + 백엔드 + 중간 피드백 Worker

한 서버(GPU VM)에 Docker Compose로 다음을 같이 올립니다. 모두 한 내부망에서 서비스 이름으로 붙습니다.

| 서비스 | 저장소 | 파일 | 서버 밖 포트 |
| --- | --- | --- | --- |
| postgres · chroma · db-init · business-mcp · rag-mcp | ai-agent-capstone | `mcp_servers/docker-compose.yml` | 8001 · 8002 (Postgres·Chroma는 127.0.0.1) |
| vllm · llm-proxy (`--profile llm`) | ai-agent-capstone | 같은 파일 | 4000 (vLLM은 127.0.0.1) |
| rabbitmq · backend · feedback-worker | aworko-server · ai-agent-capstone | `mcp_servers/docker-compose.app.yml` | 8080 (RabbitMQ는 127.0.0.1) |

기동 순서는 compose가 지킵니다: postgres → db-init(스키마·적재) → business-mcp → rabbitmq → backend(큐 선언·Flyway) → feedback-worker.

## 0. 서버 준비

- Docker Compose v2.20 이상, GPU 서버면 NVIDIA Container Toolkit
- 두 저장소를 **나란히** 받습니다. aworko-server는 비공개라 서버에 읽기 권한(배포 키 등)이 필요합니다.

```bash
git clone https://github.com/skala2-ai-agent-capstone/ai-agent-capstone.git
git clone https://github.com/skala2-ai-agent-capstone/aworko-server.git
```

- 백엔드는 main이 아니라 `fix/sync-mcp-schema` 브랜치로 배포합니다(백엔드 작업은 이 브랜치에서만 이어가고 main에는 합치지 않음). `git -C aworko-server switch fix/sync-mcp-schema`가 원격 브랜치를 따라가는 로컬 브랜치를 만듭니다. main은 중간 피드백 요청이 모두 실패합니다(대화 스냅샷 없음).
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
- **Worker 토큰**은 회사 1곳 범위이고 기본 30일 뒤 만료됩니다. 만료되거나 `RUN_TOKEN_SECRET`을 바꾸면 2단계 `curl`로 다시 발급하고 `docker compose up -d feedback-worker`.
- **db-init**은 `up` 때마다 다시 돕니다. 시드 업무의 대화·산출물은 시드로 되돌리고(시드 업무에 쌓인 Agent·팀원 LLM 호출 기록도 지워짐), 앱에서 만든 업무는 건드리지 않습니다. 운영 중인 서버에서 코드만 바꿀 때는 `docker compose up -d --no-deps <서비스>`로 db-init을 건너뜁니다. `--reset`은 백엔드 테이블(아웃박스·DLQ 기록·집계 승인)까지 비우므로 백엔드를 멈추고 RabbitMQ 큐를 비운 뒤 씁니다.
- **기존 DB**에 올릴 때: `db/postgres/002_live_feedback_snapshot.sql`(중간 피드백 스냅샷)과 `003_frontend_upload.sql`(고객 미지정 업무, 고객 자료 원본 파일), `004_team_evaluation.sql`(팀 평가 요청·리포트)을 적용합니다. db-init이 매번 같이 적용하며, db-init 없이 올릴 때는 `docker exec -i ax-eval-mcp-postgres-1 psql -U axeval -d axeval < db/postgres/003_frontend_upload.sql`(004도 같은 방식). 예전 백엔드의 Flyway V2(역할 제한을 넓히던 임시 파일)가 적용된 DB는 볼륨을 지우고 새로 만듭니다(`docker compose down -v`).
- **포트**: 방화벽에서는 8080(백엔드)만 엽니다. ax-eval-gpu는 VM 태그 `aworko-backend` + 규칙 `allow-aworko-backend-8080`(tcp:8080, 0.0.0.0/0)으로 열려 있습니다(HTTP, 개발용). 8001·8002·4000은 팀원 개발용이며 `/internal/*`은 `X-Internal-Key`로만 보호되므로 팀 IP로 제한합니다.
- **업데이트**: 두 저장소에서 `git pull`(백엔드는 `fix/sync-mcp-schema`에서) 뒤 `docker compose --profile llm up -d --build`. MCP 의존성(내규 PDF용 `pdfplumber`)이 바뀌면 `--build`가 있어야 이미지에 들어갑니다.
- **검증 프로필은 운영 DB에서 돌리지 않습니다**: `docker compose --profile test run --rm verify`는 평가 건 상태·초안을 바꾸고 현행 내규·평가 기준을 새 버전으로 바꿉니다(`verify_live_rubric`·`verify_documents`). 새로 만든 DB(`down -v` 뒤)에서만 씁니다. 운영 서버에서 새 내부 API가 떴는지는 데이터를 바꾸지 않는 요청으로 봅니다: `POST /internal/documents`에 `{"company_id":"NOPE"}` → `NOT_FOUND`.

## 아직 배포해도 동작하지 않는 것

| 기능 | 이유 | 맡을 쪽 |
| --- | --- | --- |
| 최종 평가(EVALUATE → 확인 대기) | 코드는 맞춰 둠: Agent `backend-v1` Worker, 백엔드 완료 큐 소비자·RESUME 폐지(이의·검토는 백엔드가 바로 처리)·평가별 토큰 파일 공급, compose `final-worker`(`--profile final`). 아직 서버에 안 올라감. 켜기 전 확인(쌓인 RESUME 비우기, `PROXY_KEYS`의 final-worker 키): [backend_worker_alignment.md](backend_worker_alignment.md) 5절 | 배포 |
| 팀 평가(팀 코칭 리포트) | 코드는 맞춰 둠: MCP 팀 도구·내부 API, Agent 큐 Worker(`ai_agents.cli.team_worker`), 백엔드 요청·목록·상세 API·공개 승인 판단·요청별 team 토큰 파일 공급·DLQ 기록, compose `team-worker`(`--profile team`). 아직 서버에 안 올라감, 관리자 화면 없음. 켜는 절차: [team_evaluation.md](team_evaluation.md) 「서버에서 켜기」 | 배포·프론트 |
| 운영 설정: 자료 업로드 → 평가 기준 자동 생성·승인 | MCP는 준비됨(`/internal/documents` 내규 적재, `/internal/rubrics/drafts`·`drafts/items`·`approve`·`discard`). 백엔드 쓰기 API(조회만 있음)와 평가 기준 생성 Agent Worker·요청 큐가 없음 | Agent·백엔드 |
| 대화 중 사내 자료 자동 참고(RAG) | rag-mcp가 Agent 토큰으로만 열려 있음 | MCP |
| 브라우저에서 백엔드 호출 | CORS 설정이 없어 프론트가 다른 출처면 막힘 | 백엔드 |
| 형식이 깨진 피드백 이벤트 | Worker가 `live_eval_id` 없이 DLQ로 보내 요청이 `대기`로 남음 | Agent |
