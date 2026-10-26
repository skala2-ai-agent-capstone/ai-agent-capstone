# 팀 업무 평가·코칭 Agent

브랜치: `feat/team-evaluation`. 개인 중간 피드백·최종 평가와 같은 패키지의
`evaluation_agent/team/`에 별도 실행 경로를 추가했다. 기준 생성 로직은 변경하지 않는다.

## 동작과 범위

```text
화면:   팀장이 기간 선택 → POST /api/teams/{team_id}/evaluations
백엔드: 담당 팀장 확인 → 공개 승인 판단(같은 기간 집계 승인) → POST /internal/team-evaluations (요청 행)
MCP:   업무별 최신 확정 평가 선택 → 업무 유형·기준 버전·기준 해시별 집계
       → 공개 기준 적용 → 스냅샷 고정 (team_evaluations, 대기)
백엔드: outbox TEAM_EVALUATE → team_evaluation.requests
백엔드: 3초마다 대기 요청마다 POST /internal/team-runs → team 토큰을 토큰 파일(team-tokens.json)에 공급
Worker: 토큰 파일에서 request_id 토큰 → get_team_evaluation_snapshot
Agent: 입력 검증 → 항목 평균·관측 비율 계산 → LLM 근거/코칭 선택
       → 선택 검증 → 팀 리포트 구성 → 체크포인트
MCP:   save_team_evaluation_report → 재검증 후 team_evaluations 저장(완료)
       실패: Worker가 retry 또는 team_evaluation.dlq(error_code) → 백엔드 team_evaluation_failures
화면:   GET /api/teams/{team_id}/evaluations[/{request_id}] 폴링 → 팀 리포트
```

Agent는 개인 대화, 이름, 이메일, 개인 평가 ID, 원문 코칭을 받지 않는다.
원본 개인 평가를 합산하는 작업은 정본 DB를 읽는 business-mcp가 요청 생성 때 한 번 하고,
관리자 권한과 공개 승인 판단은 백엔드 책임이다.
Agent는 서버 집계의 산술·범위 일관성을 검증하고 평균·관측 비율을 계산한다.
실제 원본과 구성원 수의 일치, 확정 여부, 관리자 권한을 독립적으로 증명하지는 못한다.

현재 구현:

- 입력/출력 계약, 집계 지표 계산, LLM 코칭 선택, 결과 검증, 로컬 CLI.
- 팀 전용 MCP 클라이언트 허용 목록과 조회/저장 orchestration.
- 가상 평가셋 8건, 기존 품질 실행기·사람 검토·LangSmith 정제 추적 연결.
- business-mcp 두 도구와 팀 집계·저장(`mcp_servers/business_mcp/team.py`, `team_evaluations` 테이블),
  내부 API `/internal/team-evaluations`·`/internal/team-runs`, team 토큰.
- 큐 Worker(`python -m ai_agents.cli.team_worker`, 아래 「큐 Worker」)와 compose `team-worker`(`--profile team`).
- 백엔드(aworko-server 로컬 `fix/sync-mcp-schema`, 미커밋): 요청·목록·상세 API, 공개 승인 판단, TEAM_EVALUATE 발행,
  요청별 team 토큰 파일 공급, DLQ 실패 기록.

아직 없는 부분:

- 관리자 화면(프론트) 연결과 서버(GCP) 배포.
- 반복 기간 조회 차감 등 보완 억제(아래 공개 정책 참고). 현재는 그룹·항목 최소 인원만 적용한다.
- GCP의 실제 모델 품질·지연 검증.

## 점수·공개 정책

동일 업무 유형·동일 기준 버전·동일 기준 해시를 한 그룹으로 취급한다.
점수 범위는 기존 평가와 동일한 0~2다. 보안 준수는 0/2만 가능하다.
각 항목은 합계 점수, 관측 평가 건수, 관측 구성원 수로 표현한다.

- 평균 = 점수 합계 / 관측 평가 건수. 평균은 3자리, 관측 비율은 소수 1자리 표시.
- 관측 비율 = 관측 평가 건수 / 같은 그룹 확정 평가 건수 × 100.
- `statistics: null`은 비공개 또는 공개할 관측 자료 없음. 원인·소수 인원 수는 싣지 않는다.
- 자료가 없으면 0점으로 만들지 않는다. 공개할 지표가 없으면 LLM을 호출하지 않는다.
- 다른 업무·기준 버전 간 총점, 순위, 개선 추세는 생성하지 않는다.
- 이 평균은 평가 건수 가중이다. 업무가 많은 구성원이 더 반영될 수 있어 개인별 동일
  가중 평균이나 팀 전원의 역량으로 해석할 수 없다.

**계약 하한은 서로 다른 구성원 2명 이상**이고, 실제 공개 기준은 회사 `min_aggregation_size`(기본 5,
시연 회사 C-LGCNS-SALES는 시연 데이터 규모에 맞춰 2)로 정한다. 서버의 `min_members`는 이보다
강화할 수 있고 Agent는 그룹·항목별 관측 구성원 모두에 적용한다.
구성원 수가 적은 그룹은 서버가 전체를 제외하고, 작은 항목은 통계 자체를 null로 보낸다.
`release_approved=false`이면 `groups=[]`이어야 하며 결과는 `withheld`다.
공개 승인됐지만 지표가 없으면 `insufficient_data`다.

이 기준은 팀과 합의할 임시 제품 설정이며 익명성을 보장하는 인증이 아니다.
반복 기간 조회의 차이, 전체/하위 그룹의 차감, 특정인의 업무 집중, 극단적인 값으로
개인 정보를 추정할 수 있는 집계는 백엔드에서 함께 제한해야 한다. 필요한 보완 억제를
적용한 후에만 공개 승인하며 Agent는 제외된 수치를 복원하지 않는다.

## LLM의 역할

LLM에는 공개된 수치, 업무 유형, 항목, 임시 근거 ID와 고정 코칭 목록만 전달한다.
팀/요청/스냅샷 UUID, 기간, 구성원 수, 기준 자유 문구는 전달하지 않는다.

LLM은 강점 근거 최대 3개, 개선 근거와 해당 항목의 활동 최대 3개를 선택한다.
숫자와 근거 설명은 코드로 렌더링하여 임의 점수·개인 이름·근거 없는 자유 문장을
리포트에 추가할 수 없게 했다. 코칭 문구는 5개 항목별 2개씩, 총 10개다.
따라서 현재 버전은 자유 서술형 분석이 아닌 **집계 근거에 따른 활동 선택 방식**이다.
실제 품질 평가에서는 선택의 유용성·우선순위·코칭 목록의 충분성을 검토한다.

강점 후보는 평균 1.5/2 이상, 개선 후보는 미만이다. 이 경계는 코칭용 임시 기준으로
팀원의 개인 평가 기준을 바꾸지 않는다. 해당 band가 있으면 최소 한 항목을 선택하며,
형식·ID·band·활동 항목이 잘못되면 1회 재생성 후 실패 처리한다.
추후 경계나 활동을 바꾸면 코드·목록 해시를 기록하고 동일 평가셋으로 비교한다.

## 백엔드 없이 실행

```bash
source .venv/bin/activate
python -m ai_agents.cli.team_evaluation check --snapshot samples/team/snapshot.json
python -m ai_agents.cli.team_evaluation schemas
```

`check`, `schemas`는 네트워크 호출이 없다. 루트 `.env`에 실제 모델 정보를 설정한다.

```dotenv
TEAM_LLM_BASE_URL=http://YOUR_MODEL_HOST:8000/v1
TEAM_LLM_MODEL=YOUR_DEPLOYED_MODEL
TEAM_LLM_API_KEY=
TEAM_MODEL_REVISION=
LANGSMITH_API_KEY=
LANGSMITH_PROJECT=ai-agent-quality
```

```bash
python -m ai_agents.cli.team_evaluation run \
  --snapshot samples/team/snapshot.json \
  --output-dir outputs/team/baseline --langsmith
```

이 명령은 모델만 호출하며 MCP/DB에 저장하지 않는다. LangSmith 없이 실행하려면
`--langsmith`를 뺀다. OpenAI 호환 Chat Completions의 JSON mode가 필요하다.
기존 프록시 호환을 위해 `X-Stage: evaluate`를 쓰며 work/user ID를 보내지 않는다.
원문 프록시 로그의 접근 권한은 별도 관리한다. LangSmith에는 정제 추적만 보낸다.

결과 폴더: `snapshot.json`, `report.json`, `save-request.json`, `manifest.json`, `traces/`.
기존 폴더는 덮어쓰지 않는다. 저장용 요청 파일이 있어도 서버 저장 완료를 뜻하지 않는다.
공개 지표가 전혀 없으면 모델 설정 없이도 보류 결과를 생성할 수 있다.

### 실제 모델 품질 평가 8건

기존 `QUALITY_LLM_*` 설정을 사용하며 `TEAM_LLM_*`와 별개다.

```bash
python -m ai_agents.cli.quality check --dataset samples/team/quality-v1.json
python -m ai_agents.cli.quality run \
  --dataset samples/team/quality-v1.json \
  --output-dir outputs/quality/team-v1 --label team-baseline --langsmith
python -m ai_agents.cli.quality report outputs/quality/team-v1/experiment.json \
  --reviews outputs/quality/team-v1/reviews.json \
  --output-dir outputs/quality/team-v1-report
```

평가셋은 혼합/강점만/개선만/공개 제한/관측 없음/일부 누락/서로 다른 업무와 기준/강화된
공개 기준의 8건이다. 이 중 2건은 보류하여 모델 호출이 0회다. 자동 성공 8건은
모델 평가 8건을 뜻하지 않는다. 미호출 2건은 `reviews.json`에서 `not_applicable`이며
LLM 품질 점수 대상에서 제외한다. 나머지 6건의 선택 품질을 사람이 검토한다.
사람 검토·전후 비교는 [품질 실행기 안내](offline_llm_quality.md)를 따른다.

## MCP·백엔드 담당자에게 전달할 계약

business-mcp에 구현돼 있다(`mcp_servers/business_mcp/team.py`, 사용법은 `mcp_servers/README.md`
「팀 평가」). 정확한 JSON Schema는 `python -m ai_agents.cli.team_evaluation schemas`로 출력한다.

| 도구 | 요청 | 응답 data |
| --- | --- | --- |
| `get_team_evaluation_snapshot` | `request_id` UUID 하나 | `TeamSnapshot` |
| `save_team_evaluation_report` | `TeamSaveRequest` | `TeamSaveReceipt` |

MCP 공통 응답은 `ok`, `data`, `meta.run_id`, `meta.snapshot`이다. team 토큰의 `meta.snapshot`은
`{request_id, snapshot_id, snapshot_sha256}`로 고정이며, 조회와 저장에서 이 문맥이 바뀌면 클라이언트는 거부한다.

business-mcp가 하는 일(요청 생성 `/internal/team-evaluations` 때 한 번 계산해 고정):

1. request UUID(백엔드가 주거나 서버가 발급)와 snapshot UUID 발급. 같은 request_id·같은 범위는
   기존 행, 다른 범위는 `VERSION_CONFLICT`.
2. `[period_start, period_end)`(KST)의 업무 완료 시각(`work_cases.completed_at`) 기준으로
   업무별 최신 `확정` 평가 하나만 선택.
3. 업무 유형·rubric 버전·기준 내용 해시별로 그룹화. 최신 초안에서 `채점`이고 점수가 유효한
   항목만 `score_sum/scored_evaluations`에 포함하고, 구성원 수는 서로 다른 담당자 수.
4. 기준 항목 이름(문제 정의·지시 품질·결과 검증·보안 준수·결과물 품질)으로 5개 공통 항목에 매핑.
   맞지 않는 기준·초안이 있으면 요청 생성을 `VALIDATION_ERROR`로 거부(LLM에 맡기지 않음).
5. 공개 기준 적용: `min_members` = max(2, 회사 `min_aggregation_size`, 요청 값). 미달 그룹은 제외,
   미달 항목은 `statistics: null`, `release_approved=false`면 `groups=[]`.
   Agent에는 팀 코드 대신 회사·팀으로 만든 고정 UUID를 준다.
6. 저장 시 team 토큰의 요청·회사·팀, snapshot_id, `snapshot_sha256`, `report_sha256`을 확인하고
   `metrics`를 스냅샷으로 다시 계산해 대조한다. 강점/개선 band, 개선 활동과 항목의 짝, 문장의
   민감정보 패턴도 본다. 문장 문구 자체는 대조하지 않는다. 요청 하나에 리포트 하나이며 같은 키·같은
   내용은 기존 receipt(`meta.duplicate`), 다른 내용은 `VERSION_CONFLICT`. 재생성은 새 요청 ID.

백엔드가 할 일:

1. 인증된 관리자와 요청의 회사·팀·기간 권한을 확인하고 공개 승인 여부(`release_approved`)를 정해
   `/internal/team-evaluations`를 부른다. 권한 실패는 오류로 돌려주고 빈 데이터로 위장하지 않는다.
2. 반복 기간 조회 차감 등 보완 억제가 필요하면 공개 승인 전에 적용한다.
3. `/internal/team-runs`로 받은 team 토큰을 팀 Agent 실행에 넘긴다.
4. 관리자에게 팀 리포트만 제공하고 개인 상세 리포트 조회 권한을 추가하지 않는다.

Agent 연동 API:

```python
async with TeamMCPGateway(team_scoped_token, business_url) as gateway:
    workflow = TeamEvaluationWorkflow(gateway, TeamAgent(client, model))
    snapshot, report = await workflow.prepare(request_id)
    # 호출자가 두 객체를 원자적으로 영속화한 뒤 저장한다.
    receipt = await workflow.save(snapshot, report)
    # 응답 유실 시 영속화한 동일 객체로 save만 재호출한다. LLM 재생성 금지.
```

이 흐름을 큐 Worker가 그대로 쓴다(아래). 영속화는 체크포인트 파일이다.
팀 gateway는 두 도구만 허용하며 개인 평가·대화 조회와 기준 생성·저장 도구는 막는다.
읽기와 저장은 분리해 호출자가 실패한 저장만 재시도할 수 있게 했다.
로컬 파일을 다시 읽어 저장 인자만 만들려면 다음을 사용한다(네트워크 호출 없음).

```bash
python -m ai_agents.cli.team_evaluation save-request \
  --snapshot outputs/team/baseline/snapshot.json \
  --report outputs/team/baseline/report.json \
  --output outputs/team/baseline/save-retry.json
```

서버 저장 구조는 `team_evaluations`(`mcp_servers/db/postgres/001_schema.sql`, 기존 DB는
`004_team_evaluation.sql`)다. 쓴 평가 건 ID는 `source_evaluation_ids`에만 남기고 Agent에 주지 않는다.

## 백엔드 API (aworko-server, 담당 팀장만)

| 화면 동작 | API |
| --- | --- |
| 팀 평가 요청 | `POST /api/teams/{team_id}/evaluations` `{from, to}`(끝 포함, 최대 93일) → 202 `{request_id, status:대기, release_approved, unapproved_work_types[]}` |
| 목록 | `GET /api/teams/{team_id}/evaluations` → 최근 20건 `{request_id, status(대기·완료·실패), error, period_from, period_to, release_approved, report_status, …}` |
| 상세 | `GET /api/teams/{team_id}/evaluations/{request_id}` → `{evaluation, report}`(완료일 때 TeamReport) |

- **공개 승인:** 기간에 확정 평가가 있고 최소 인원을 넘는 업무 유형이 모두 팀 대시보드에서 **같은 기간으로 집계 승인**돼 있어야
  `release_approved=true`다. 하나라도 빠지면 false로 요청해 수치 없는 보류(`withheld`) 리포트가 되고, 빠진 유형을
  `unapproved_work_types`로 돌려준다. 공개할 유형이 없어도 false다.
- **상태:** `team_evaluations.status='완료'`면 완료, 백엔드 `team_evaluation_failures`(Flyway V6)에 기록이 있으면 실패(`error`=Worker 오류 코드),
  30분(`app.team-evaluation.stale-after`) 넘게 대기면 실패(`TIMED_OUT`)로 보이고 토큰 공급도 멈춘다.
- **토큰:** `TEAM_TOKEN_FILE`을 주면 백엔드가 3초마다 대기 요청마다 `/internal/team-runs`(`ttl_sec` 7200)로 발급해 둔다.
  남은 수명이 30분 아래일 때만 다시 발급하고, 끝난 요청은 뺀다. 형식·권한(600)·원자적 교체는 최종 평가 토큰 파일과 같다.
- MCP 요청 만들기가 거절되면(`VALIDATION_ERROR` 등) 400·404·409로 돌려주고 TEAM_EVALUATE는 보내지 않는다.

## 큐 Worker

큐는 백엔드가 선언한다(`team_evaluation.exchange`, direct). Worker는 선언하지 않고 없으면 소비 전에 실패한다.

| 큐 | 방향 | 내용 |
| --- | --- | --- |
| `team_evaluation.requests` | 백엔드 → Worker | `TEAM_EVALUATE` `{message_id, type, request_id(UUID), company_id, attempt}` (routing key `team_evaluate`) |
| `team_evaluation.retry` | Worker → (30초 뒤 requests) | 원래 요청, `attempt + 1` |
| `team_evaluation.dlq` | Worker → 백엔드 | 원래 요청 + `error_code` 헤더. 형식 오류면 `request_id`만 |

결과 큐는 없다. `save_team_evaluation_report`가 성공하면 MCP가 요청을 완료로 바꾸고 Worker는 ack한다.
스냅샷·리포트는 저장 전에 `--checkpoint-dir/{request_id}.json`(600)에 남기고, 재시도 때는 LLM을 부르지 않고 저장만 다시 한다.

| 코드 | 재시도 | 원인 |
| --- | --- | --- |
| `TOKEN_NOT_READY`·`TOKEN_EXPIRED`·`TOKEN_FILE_UNAVAILABLE` | O | 토큰 파일에 아직 그 요청이 없음 등(백엔드 공급 주기 3초) |
| `MCP_UNAVAILABLE` | O | MCP 연결 실패·일시 오류 |
| `LLM_UNAVAILABLE` | O | 모델 API 연결·시간 초과·5xx |
| `LLM_REJECTED`·`INVALID_TEAM_DECISION` | X | 모델 거절·선택 형식 오류(재생성 1회 뒤) |
| `REPORT_CONFLICT` | X | 이 요청에 다른 리포트가 이미 저장됨. 다시 만들려면 새 요청 |
| `REPORT_REJECTED`·`INVALID_RESULT`·`CHECKPOINT_INVALID` | X | MCP 재검증 실패·리포트/스냅샷 불일치·체크포인트 손상 |
| `TOKEN_REJECTED`·`REQUEST_NOT_FOUND`·`SNAPSHOT_CHANGED` | X | 토큰 범위·서명 불일치, 요청 없음, 실행 중 스냅샷 문맥 변경 |
| `INVALID_EVENT`·`RETRY_EXHAUSTED`·`MCP_REJECTED`·`UNEXPECTED_ERROR` | X | 그 밖 |

```dotenv
TEAM_AMQP_URL=amqp://ACCOUNT:PASSWORD@TAILSCALE_IP:5672/
TEAM_BUSINESS_MCP_URL=http://TAILSCALE_IP:8001/mcp
TEAM_LLM_BASE_URL=http://TAILSCALE_IP:4000/v1
TEAM_LLM_MODEL=gpt-4o-mini
TEAM_LLM_API_KEY=ISSUED_PROXY_KEY
```

```bash
python -m ai_agents.cli.team_worker check-event --event samples/integration/team-evaluate-v1.json
python -m ai_agents.cli.team_worker worker --token-file outputs/run-tokens/team-tokens.json \
  --checkpoint-dir outputs/team-checkpoints --trace-dir outputs/team-worker-traces --langsmith
```

### 서버에서 켜기

1. 백엔드(이 변경 포함)를 올린다. Flyway V6이 돌고 `team_evaluation.*` 큐가 생긴다. V4·V5와 함께 올린다(번호 순서).
2. llm-proxy `PROXY_KEYS`에 `team-worker:<TEAM_LLM_API_KEY 값>`을 넣고 `.env`에 `TEAM_LLM_API_KEY`를 둔다.
3. `docker compose --profile team up -d team-worker`. 팀 대시보드에서 유형별 집계를 승인한 뒤 같은 기간으로 팀 평가를 요청하고,
   목록이 `대기 → 완료`로 바뀌는지 본다.

## 백엔드·큐 Worker 연결 검증 기록 — 2026-10-23

- Agent(브랜치 `feat/team-evaluation-worker`, 기준 `990a367`): `tests/test_team_worker.py` 10개 포함 전체 947 통과·4 건너뜀,
  Ruff 통과. 가짜 MCP·모델로 요청 토큰으로 조회→리포트→체크포인트(600, 토큰 없음)→저장→ack, 저장 응답 유실 뒤 재시도 때
  LLM·스냅샷 재호출 없이 저장만, 빈 토큰 목록 → `TOKEN_NOT_READY` 재시도 → 공급 뒤 성공, 비재시도 오류·재시도 한도 DLQ,
  형식 오류 시 request_id만 남기기, 손상된 체크포인트 저장 거부를 확인했다.
- 백엔드(aworko-server 로컬, 미커밋): 전체 157 통과(Testcontainers Postgres·RabbitMQ). 요청 → MCP 대역 호출(기간 끝+1일)·
  TEAM_EVALUATE 아웃박스, 담당 팀장 외 403, MCP 거절 시 메시지 없음, 공개 승인 판단 3가지, DLQ → 실패 기록, `TIMED_OUT`,
  실제 RabbitMQ로 발행 칸·DLQ 소비, team 토큰 파일 발급·정리, `/internal/team-runs` HTTP 계약.
- `docker compose ... --profile team config` 통과. 실제 business-mcp·RabbitMQ·모델을 한 번에 잇는 실행은 하지 않았다.

## MCP 서버 연동 검증 기록 — 2026-10-23

브랜치 `feat/team-evaluation-mcp`, 기준 커밋 `ed2218b`에 미커밋 변경을 포함해 검증했다.
로컬 `postgres:16-alpine` 컨테이너와 venv의 business-mcp·rag-mcp(가짜 Chroma)로 돌렸다.

| 검사 | 결과 |
| --- | --- |
| `tests/test_team_mcp_postgres.py` (실제 PostgreSQL, Agent 계약 모델로 스냅샷 읽기·리포트 저장) | 6 통과 |
| 전체 Agent·프록시 회귀 검사(테스트 DB 지정) | 937 통과 / 8 건너뜀(fastapi·aio_pika 미설치) |
| `mcp_servers/tests/verify_team.py` (HTTP, 토큰 종류·범위·집계·저장·재전송·충돌·정리) | 39/39 |
| 기존 `verify_spec`·`verify_db`·`verify_app_work`·`verify_live_rubric`·`verify_documents` | 861·42·10·92·30 모두 통과 |
| 실제 `TeamMCPGateway`·`TeamEvaluationWorkflow`로 HTTP 조회→리포트→저장→재전송 | 스냅샷 해시 일치, 같은 영수증, 서버 저장본 일치 |

종단 확인의 리포트는 LLM 대신 고정 선택(`build_report`)으로 만들었다. 실제 모델 호출, 서버(GCP) 배포,
백엔드 호출 경로는 확인하지 않았다. `verify_e2e`는 llm-proxy가 필요해 돌리지 않았다.

## 구현 검증 기록 — 2026-10-23

기준 커밋 `64d8117`에 브랜치의 미커밋 변경을 포함하여 검증했다.

| 검사 | 결과 |
| --- | --- |
| Ruff (`src`, `tests`) | 통과 |
| 전체 Agent·프록시 회귀 검사 | 908 통과 / 16 건너뜀 |
| 팀 기능 테스트 | 38개, 위 전체 결과에 포함 |
| 건너뜀 사유 | 테스트용 PostgreSQL 주소 미지정 |
| 팀 평가셋 실행 A/B | 각각 8/8 기대 실행 성공, 모델 응답 대역 호출 6회 |
| 자료 부족/공개 제한 | 각각 1건, 모델 미호출·품질 점수 대상 제외 |
| 사람 품질 점수 | 미검토, 모델 개선 효과 주장 없음 |

실제 OpenAI SDK의 HTTP 전송을 대역으로 교체하여 요청 형식, 선택 검증, 재생성,
API 오류 분류, 취소, 개인 필드/소수 인원 거부, 버전 분리, 저장 재시도와 결과 변조 거부를
검증했다. MCP 도구는 모의 gateway이며 실제 서버·모델·LangSmith 원격 호출은 하지 않았다.

로컬 증빙(Git 제외):

- `outputs/validation/team-evaluation/pytest.xml`
- `outputs/validation/team-evaluation/smoke-20261023T051823/team-a-report/`
- `outputs/validation/team-evaluation/smoke-20261023T051823/team-b-report/`
- `outputs/validation/team-evaluation/smoke-20261023T051823/comparison-report/`

이 기록은 `test_transport` 실행기 검증이다. 실제 모델 코칭 품질과 지연 시간은
GCP 모델에 연결한 뒤 같은 평가셋을 실행하고 사람 검토를 붙여 별도 기록한다.
