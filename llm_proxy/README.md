# 기록 프록시 (vLLM 앞단)

vLLM 앞에서 OpenAI 호환 요청을 그대로 넘기고, 주고받은 내용을 **업무 DB(`axeval`)의 `llm_exchanges`**에 기록합니다.
업무 공간 대화의 원본이 이 테이블이고, 업무 DB의 `messages`는 이것을 펼쳐 보여주는 뷰입니다.
팀원은 OpenAI SDK를 그대로 쓰고 `.env`의 주소와 키만 바꿉니다.

```
팀원·업무 공간 백엔드·평가 Agent (OpenAI SDK, 프록시 키)
   │  Tailscale 사설망
   ▼
llm-proxy :4000 ── vLLM 키로 바꿔 그대로 전달 ──▶ vllm (컨테이너 내부망, GPU)
   │
   ▼
postgres axeval: llm_exchanges ──(뷰)──▶ messages ──▶ business-mcp ──▶ 평가 Agent
```

| 구성 | 역할 |
| --- | --- |
| `vllm` | `vllm/vllm-openai` 이미지. 모델 서빙, 도구 호출 파서 포함 |
| `llm-proxy` | 프록시 키 확인 → 기록용 헤더 검증 → vLLM 키로 바꿔 요청을 그대로 전달 → 요청·응답을 업무 DB에 기록. 요청 본문을 고치지 않으므로 Chat Completions·Responses API·스트리밍 모두 vLLM 동작 그대로 |
| `postgres` | 업무 DB. 스키마는 [`mcp_servers/db/postgres/001_schema.sql`](../mcp_servers/db/postgres/001_schema.sql) |

기록하지 않는 것: 키(`Authorization`), 모델 목록 같은 GET 요청. 기록 저장이 실패해도 LLM 응답은 그대로 돌려줍니다.

## 1. EC2에서 실행

vLLM·프록시는 `mcp_servers/docker-compose.yml`의 `llm` 프로필로 업무 DB·MCP 서버와 함께 뜹니다.
GPU 드라이버와 NVIDIA Container Toolkit이 들어 있는 Deep Learning Base OSS Nvidia Driver GPU AMI(Ubuntu)를 전제로 합니다.
저장소 전체(`mcp_servers/`와 `llm_proxy/`가 같이 있어야 함)를 EC2에 올린 뒤:

```bash
cd mcp_servers
cp .env.example .env     # POSTGRES_PASSWORD, RUN_TOKEN_SECRET, INTERNAL_API_KEY, SEED_USER_PASSWORD, VLLM_API_KEY, PROXY_KEYS 채우기
docker compose --profile llm up -d --build
docker compose logs -f vllm          # 모델 다운로드·로딩. "Application startup complete"가 나오면 준비 완료
curl localhost:4000/health           # {"ok":true}
```

키는 `python3 -c "import secrets;print(secrets.token_urlsafe(32))"`로 만듭니다.
`PROXY_KEYS`는 `이름:키,이름:키` 형식이며 이름이 `llm_exchanges.member`에 남습니다(예: `workspace-backend`, `eval-agent`, 팀원 이름).
vLLM이 준비되기 전에는 프록시가 `502 upstream_unreachable`을 돌려줍니다.

### 접속 범위

- 보안그룹에서 4000·8000~8002·8010 포트를 열지 않습니다. 팀원은 Tailscale로만 접속합니다.
- `.env`의 `PROXY_BIND`를 서버의 Tailscale IP(`tailscale ip -4`)로 바꾸면 프록시가 사설망 주소에서만 열립니다.
- vLLM(`127.0.0.1:8010`)과 Postgres(`127.0.0.1:5433`)는 서버 안에서만 접근됩니다.

## 2. 팀원 설정

팀원 프로젝트 `.env`:

```
OPENAI_BASE_URL=http://100.x.y.z:4000/v1
OPENAI_API_KEY=<본인 프록시 키>
OPENAI_MODEL=gemma4
```

업무·사용자·단계는 요청 헤더로 남깁니다. 프록시만 읽고 vLLM에는 전달되지 않습니다.

| 헤더 | 기록 칸 | 값 |
| --- | --- | --- |
| `X-Work-Id` | `work_id` | 업무 DB `work_cases.work_id` |
| `X-User-Id` | `user_id` | 업무 DB `users.user_id`(로그인한 직원) |
| `X-Stage` | `stage` | `workspace_chat`(직원 대화), `classify`·`rubric`·`evaluate`(최종 평가 Agent), `live_evaluate`(중간 피드백) |

`llm_exchanges`는 업무 DB의 외래 키와 CHECK를 따르므로, 프록시는 **저장할 수 없는 헤더를 vLLM에 넘기기 전에 `400 invalid_trace_headers`로 거절**합니다.
답은 받았는데 기록만 사라지는 일을 막기 위해서입니다.

| 거절하는 경우 | 이유 |
| --- | --- |
| `X-Stage`가 위 5개가 아님 | `stage` CHECK 위반 |
| `X-Stage: workspace_chat`인데 `X-Work-Id`나 `X-User-Id`가 없음 | 대화가 어느 업무·직원에도 붙지 않음 |
| `X-Work-Id`·`X-User-Id`가 업무 DB에 없음 | 외래 키 위반 |

헤더는 호출하는 쪽이 붙입니다. 프록시가 알아서 채우지 않으므로, 빠지면 기록의 `stage`·`work_id`·`user_id`가 비어 남습니다.

| 호출하는 쪽 | 붙일 헤더 |
| --- | --- |
| 업무 공간 백엔드(직원 대화) | `X-Stage: workspace_chat`, `X-Work-Id`, `X-User-Id`(JWT에서 꺼낸 사용자) |
| 최종 평가 Agent(분류·기준 생성·평가) | `X-Stage: classify`·`rubric`·`evaluate`, `X-Work-Id` |
| 중간 피드백 Worker | `X-Stage: live_evaluate`, `X-Work-Id`, `X-User-Id`(요청한 직원 `requested_by`) |

중간 피드백 Agent(`src/ai_agents/evaluation_agent/feedback/agent.py`)는 MCP에서 검증한 문맥의
업무·요청자 ID를 `X-Work-Id`, `X-User-Id`로 보내고, `X-Stage: live_evaluate`를 붙입니다.
기준 생성·최종 평가 호출의 자동 헤더 연결은 아직 구현되지 않았습니다.
가상 대화 품질 실험(`validation.feedback_targets:model_feedback`)은 `X-Stage`만 보냅니다.
가상 업무·사용자 ID를 실제 DB 외래 키에 넣지 않기 위해서이며, 이 실행은 해당 ID가 null로 기록됩니다.

중간 피드백 실행에서는 일반 `OPENAI_*` 대신 다음 환경변수를 설정합니다.

```dotenv
FEEDBACK_LLM_BASE_URL=http://100.x.y.z:4000/v1
FEEDBACK_LLM_API_KEY=<본인 프록시 키>
FEEDBACK_LLM_MODEL=gemma4
```

이 헤더들은 기록용이며 권한을 증명하지 않습니다. 프록시 키로 인증하고, 실제 요청자의 권한은
백엔드·MCP에서 검증해야 합니다. 프록시는 참조 ID 존재 여부를 검사합니다.
업무 공간 백엔드는 대화를 보낼 때 세 헤더를 직접 붙이고, `X-User-Id`에는 JWT에서 꺼낸 사용자를 넣습니다.
대화 이력은 `messages` 뷰에서 읽어 그대로 보내고, 첨부 본문 같은 백엔드가 붙이는 자료는 `system` 메시지로 넣습니다.

```python
await client.chat.completions.create(..., stream=True, extra_headers={
    "X-Work-Id": work_id, "X-User-Id": user_id, "X-Stage": "workspace_chat",
})
```

## 3. 기록 보기

```bash
docker compose exec postgres psql -U axeval axeval     # mcp_servers/에서
```

```sql
-- 한 업무의 대화 (뷰)
SELECT seq, role, left(content, 60), created_at FROM messages WHERE work_id = 'W-E2E-RFP-01' ORDER BY seq;

-- 한 업무의 LLM 사용 전체(직원 대화 → 분류 → 기준 생성 → 평가) 순서대로
SELECT id, created_at, stage, user_id, status_code, error, latency_ms
FROM llm_exchanges WHERE work_id = 'W-E2E-RFP-01' ORDER BY id;

-- 호출 주체별 사용량
SELECT member, count(*), sum(input_tokens), sum(output_tokens), avg(latency_ms)::int
FROM llm_exchanges GROUP BY member;
```

컬럼 타입·코드값·JSONB 구조 전체는 [데이터 명세](../docs/llm_exchanges_data_spec.md)에 있습니다.

### 모의 데이터

`mock/build_mock.py`가 업무 DB 가상 40건을 바탕으로 237행을 만들어 [`mcp_servers/data/llm_exchanges.jsonl`](../mcp_servers/data/llm_exchanges.jsonl)에 씁니다.
업무 DB 적재기(`db-init`)가 이 파일을 `llm_exchanges`에 넣으므로, 따로 넣을 필요가 없습니다.
대화 변환 규칙은 적재기와 같은 [`chat_log.py`](../mcp_servers/db/loader/chat_log.py)를 써서, `messages` 뷰가 원천 대화 182개를 그대로 돌려줍니다.
다시 만들 때는 프로젝트 루트에서 `.venv/bin/python llm_proxy/mock/build_mock.py`를 실행한 뒤 `db-init`을 다시 돌립니다.

## 4. 서버를 지우기 전에 백업

`llm_exchanges`는 업무 대화의 원본입니다. EC2를 Terminate하기 전에 업무 DB 전체를 덤프해 내려받습니다.

```bash
docker compose exec -T postgres pg_dump -U axeval -Fc axeval > axeval.dump     # mcp_servers/에서
```

로컬에서 `scp`로 가져온 뒤 `pg_restore`로 복원합니다. 업무 데이터와 사용자 비밀번호(평문)가 들어 있으므로 Git이나 공유 드라이브에 올리지 않습니다.

## 5. 테스트

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q tests
```

실제 Postgres 검증은 시드를 적재한 업무 DB 주소를 줄 때만 실행됩니다. 테스트가 넣은 행은 스스로 지웁니다.

```bash
LLM_PROXY_TEST_DATABASE_URL=postgresql://axeval:<비밀번호>@127.0.0.1:5433/axeval python -m pytest -q tests
```
