# LLM 호출 기록(`llm_exchanges`) 데이터 명세

vLLM 앞의 기록 프록시(`llm_proxy/`)가 LLM 호출 1회마다 1행을 **업무 DB(`axeval`)의 `llm_exchanges`** 테이블에 남깁니다.
직원이 업무 공간에서 나눈 대화와 평가 Agent의 분류·기준 생성·평가 호출이 같은 테이블에 쌓이며,
`work_id`로 한 업무의 LLM 사용 전체를 시간순으로 볼 수 있습니다.
업무 공간 대화의 원본이 이 테이블이고, 업무 DB의 `messages`는 이것을 펼쳐 보여주는 뷰입니다(3장).

- 정의: [`mcp_servers/db/postgres/001_schema.sql`](../mcp_servers/db/postgres/001_schema.sql) (팀 기준 문서 `db-schema.json`)
- 기록 코드: [`llm_proxy/proxy/app.py`](../llm_proxy/proxy/app.py)
- 모의 데이터: [`llm_proxy/mock/`](../llm_proxy/mock/) (7장)

## 1. 기록 단위와 흐름

```
[새 업무 생성] work_cases.work_id 발급 (업무 DB)
   │
   ├─ 업무 공간 대화    member=workspace-backend  stage=workspace_chat  user_id=직원
   │     답변 1개 = 1행 (POST /v1/chat/completions)
   │
   ├─ [중간 점검] Agent stage=live_evaluate  (요청 시점까지 성공한 최근 질문·응답 5쌍)
   │
   └─ [최종 완료] Agent member=eval-agent  user_id 없음
         classify → rubric → evaluate   (POST /v1/responses)
```

| 항목 | 규칙 |
| --- | --- |
| 1행 | 프록시를 거친 POST 요청 1회. 재시도·재실행은 각각 새 행 |
| 기록하지 않음 | GET 요청(모델 목록 등), 프록시 키가 틀린 요청(401), 저장할 수 없는 헤더라 vLLM 전에 거절한 요청(400 `invalid_trace_headers`) |
| 저장하지 않는 값 | `Authorization` 헤더(키). 기록용 헤더 `X-*`는 vLLM에 전달하지 않음 |
| 기록 시각 | 응답이 끝난 뒤(실패면 실패가 확정된 뒤). `created_at`은 DB 기본값 `now()` |
| 기록 실패 | DB 저장이 실패해도 LLM 응답은 그대로 반환하고 프록시 로그에 예외 종류만 남김 |

중간 피드백의 “5개”는 메시지 5행이 아니라 **완료된 호출에 대응하는 질문·응답 5쌍**이다.
`request`에는 이전 대화가 포함될 수 있으므로 전체 배열을 새 대화로 세지 않는다.
`conversation_pairs`로 질문·응답을 연결한 뒤, 해당 업무의 `workspace_chat`, HTTP 2xx,
오류 없음, 비어 있지 않은 질문·답변, 요청 시각 이전 완료 조건을 적용한다.
선택 결과는 `live_evaluations.conversation_snapshot`에 마스킹본으로 고정한다.

## 2. 컬럼 명세

JSON 타입은 모의 데이터 JSONL(`mcp_servers/data/llm_exchanges.jsonl`)에서의 타입입니다. NULL 허용은 스키마 기준이며, 기록 프록시는 `member`·`endpoint`·`stream`·`latency_ms`를 항상 채웁니다.

| # | 컬럼 | PostgreSQL 타입 | JSON 타입 | NULL | 출처 | 설명 | 예시 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `id` | `BIGSERIAL` (PK) | integer | 불가 | DB | 기록 순번. 1부터 증가 | `42` |
| 2 | `created_at` | `TIMESTAMPTZ` | string (ISO 8601, 시간대 포함) | 불가 | DB `now()` | 기록 시각(응답 종료 시각) | `"2026-10-07T11:18:40.962+09:00"` |
| 3 | `member` | `TEXT` | string | 가능 | 프록시 키 | 호출한 쪽. `PROXY_KEYS`의 이름 | `"eval-agent"` |
| 4 | `endpoint` | `TEXT` | string | 가능 | 요청 경로 | 호출한 OpenAI 호환 API 경로(쿼리 제외) | `"/v1/responses"` |
| 5 | `stage` | `TEXT` CHECK | string \| null | 가능 | 헤더 `X-Stage` | 업무 단계(4장의 5개 값만) | `"classify"` |
| 6 | `work_id` | `TEXT` FK → `work_cases` | string \| null | 가능 | 헤더 `X-Work-Id` | 업무. 업무를 지우면 함께 지워짐 | `"W-E2E-QT-03"` |
| 7 | `user_id` | `TEXT` FK → `users` | string \| null | 가능 | 헤더 `X-User-Id` | 직원(JWT의 사용자). Agent 호출은 NULL | `"E-1047"` |
| 8 | `model` | `TEXT` | string \| null | 가능 | 요청 본문 `model` | 요청한 모델 이름(vLLM `--served-model-name`) | `"gemma4"` |
| 9 | `stream` | `BOOLEAN` | boolean | 가능 (기본 `false`) | 요청 본문 `stream` | 스트리밍 요청 여부 | `false` |
| 10 | `status_code` | `INTEGER` | integer \| null | 가능 | vLLM 응답 / 프록시 | HTTP 상태 코드. 연결 실패·시간 초과는 프록시가 돌려준 502·504 | `200` |
| 11 | `error` | `TEXT` | string \| null | 가능 | 프록시 | 오류 종류 코드(4장). 정상이면 NULL | `"upstream_timeout"` |
| 12 | `latency_ms` | `INTEGER` | integer | 가능 | 프록시 | 요청 전달부터 응답 종료까지 걸린 시간(ms). `created_at - latency_ms` = 요청 시각(질문 시각) | `14115` |
| 13 | `input_tokens` | `INTEGER` | integer \| null | 가능 | 응답 `usage` | 입력 토큰 수. 응답에 usage가 없으면 NULL | `2934` |
| 14 | `output_tokens` | `INTEGER` | integer \| null | 가능 | 응답 `usage` | 출력 토큰 수 | `556` |
| 15 | `request` | `JSONB` | object \| null | 가능 | 요청 본문 | 요청 본문 전체(5장). JSON이 아니면 NULL | `{"model": "gemma4", ...}` |
| 16 | `response` | `JSONB` | object \| null | 가능 | 응답 본문 | 응답 본문 전체(6장). 스트리밍·JSON 아님·응답 없음이면 NULL | `{"object": "response", ...}` |
| 17 | `response_text` | `TEXT` | string \| null | 가능 | 응답 본문 | 스트리밍 SSE 원문, 또는 JSON으로 읽지 못한 응답 | `"data: {...}\n\n"` |

### 인덱스

| 이름 | 컬럼 | 용도 |
| --- | --- | --- |
| `ix_llm_exchanges_work` | `(work_id, stage, id)` | 업무별 단계 흐름, `messages` 뷰 |
| `ix_llm_exchanges_user` | `user_id` | 직원별 사용량 |
| `ix_llm_exchanges_time` | `created_at` | 기간 조회 |

### 값 사이의 관계

| 조건 | 결과 |
| --- | --- |
| `stream = true` | `response` = NULL, `response_text` = SSE 원문 |
| `stream = false`, 응답이 JSON | `response` = 본문, `response_text` = NULL |
| `error` ∈ {`upstream_unreachable`, `upstream_timeout`} | `response`·`response_text`·토큰 = NULL |
| `error` = `upstream_error` | `status_code` ≥ 400, `response` = vLLM 오류 본문 |
| `input_tokens`·`output_tokens` | Chat Completions는 `usage.prompt_tokens`·`completion_tokens`, Responses API는 `usage.input_tokens`·`output_tokens`, 스트리밍은 마지막 `usage` 이벤트에서 읽음 |

## 3. 연결 키와 `messages` 뷰

`llm_exchanges`는 업무 DB 안에 있어 외래 키로 이어집니다.

| `llm_exchanges` | 업무 DB | 관계 |
| --- | --- | --- |
| `work_id` | `work_cases.work_id` | N:1. 업무 하나에 여러 호출 |
| `user_id` | `users.user_id` | N:1. Agent 호출은 NULL |
| `workspace_chat` 행 | `messages` 뷰 | 호출 1건 → 새 질문 + 답변 1개(실패 호출은 질문만) |

`messages` 뷰가 대화를 만드는 규칙은 스키마([001_schema.sql](../mcp_servers/db/postgres/001_schema.sql))의 「대화 뷰」에 있습니다.
요약하면 요청 이력의 마지막 assistant 뒤 user 메시지가 새 질문이고, 앞 호출이 실패한 같은 질문은 다시 세지 않으며,
질문 시각은 `created_at - latency_ms`, 답변 시각은 `created_at`입니다.

`work_id`는 분류 힌트가 되지 않도록 `classify`·`rubric` 요청 본문에는 넣지 않고 헤더로만 보냅니다
(분류 입력의 메시지·산출물 ID도 `message-001`, `artifact-001` 같은 중립 키).
`evaluate` 요청에는 실행 ID·근거 ID 안에 `work_id`가 들어갈 수 있습니다.

## 4. 코드값

### `member`

| 값 | 의미 |
| --- | --- |
| `workspace-backend` | 업무 공간 백엔드(직원 대화) |
| `eval-agent` | 평가 Agent(`src/ai_agents`) |
| 그 밖의 이름 | 팀원 개인 키(개발·테스트). 모의 데이터는 `dev-local` |

### `stage`

| 값 | 보내는 곳 | endpoint | 의미 |
| --- | --- | --- | --- |
| `workspace_chat` | 업무 공간 백엔드 | `/v1/chat/completions` | 직원과 AI의 업무 대화 |
| `classify` | `provider.py` | `/v1/responses` | 업무 유형 분류 |
| `rubric` | `rubric_builder.py` | `/v1/responses` | 평가기준 초안 생성(검증 실패 시 재요청 포함) |
| `evaluate` | `evaluation_agent.py` | `/v1/responses` | 현행 기준으로 최종 평가 |
| `live_evaluate` | 중간 점검 Agent | `/v1/responses` | 최근 대화 5개만 평가(`live_evaluations`) |
| NULL | 헤더를 안 붙인 호출 | 모두 | 개발 테스트 등 |

위 5개 외의 값은 스키마 CHECK에 걸리므로 기록 프록시가 vLLM에 넘기기 전에 `400 invalid_trace_headers`로 거절합니다. `workspace_chat`은 `X-Work-Id`·`X-User-Id`가 필수입니다.

### `error`

| 값 | `status_code` | 의미 |
| --- | --- | --- |
| NULL | 2xx | 정상 |
| `upstream_error` | vLLM이 준 4xx·5xx | vLLM이 오류로 응답(입력 길이 초과, 잘못된 스키마 등) |
| `upstream_unreachable` | 502 | vLLM에 연결할 수 없음(기동 중·중단) |
| `upstream_timeout` | 504 | `UPSTREAM_TIMEOUT`(기본 600초) 안에 응답이 끝나지 않음 |
| `upstream_stream_broken` | vLLM이 준 코드 | 스트리밍 도중 vLLM 연결이 끊김 |
| `client_disconnected` | vLLM이 준 코드 | 스트리밍 도중 호출한 쪽이 연결을 끊음 |

## 5. `request` (JSONB) 구조

프록시는 본문을 바꾸지 않으므로 OpenAI 호환 API 요청 형식 그대로입니다. 주요 필드만 적습니다.

### Chat Completions (`/v1/chat/completions`)

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | --- | --- |
| `model` | string | ✓ | 모델 이름 |
| `messages` | array\<object\> | ✓ | 대화 전체(시스템 → 이전 대화 → 이번 질문) |
| `messages[].role` | string | ✓ | `system` \| `user` \| `assistant` \| `tool` |
| `messages[].content` | string | ✓ | 본문 |
| `temperature` | number | | 샘플링 온도 |
| `max_tokens` | integer | | 출력 상한 |
| `stream` | boolean | | 스트리밍 여부 |
| `stream_options.include_usage` | boolean | | 스트리밍 마지막에 usage 이벤트 포함 |
| `tools`, `tool_choice` | array, string | | 도구 호출을 쓸 때 |

### Responses API (`/v1/responses`, Agent)

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | --- | --- |
| `model` | string | ✓ | 모델 이름 |
| `input` | array\<object\> | ✓ | `[{role, content}]`. 시스템 프롬프트 + 입력 JSON 문자열(+ 재요청 시 이전 답·수정 지시) |
| `text.format.type` | string | ✓ | `json_schema` |
| `text.format.name` | string | ✓ | 출력 스키마 이름(`DecisionEnvelope`, `GenerationRubric`, `EvaluationSelection` 등) |
| `text.format.strict` | boolean | ✓ | `true` |
| `text.format.schema` | object | ✓ | Pydantic 모델의 JSON Schema |
| `max_output_tokens` | integer | ✓ | 분류 2000, 기준 생성·평가 6000 |
| `store` | boolean | ✓ | `false` |
| `temperature` | number | | 평가는 `0` |

## 6. `response` (JSONB) / `response_text` 구조

### Chat Completions 응답 (`object = "chat.completion"`)

| 필드 | 타입 | 설명 |
| --- | --- | --- |
| `id` | string | 응답 ID |
| `created` | integer | Unix 시각(초) |
| `model` | string | 모델 이름 |
| `choices[].message.role` | string | `assistant` |
| `choices[].message.content` | string \| null | 답변 본문 |
| `choices[].message.tool_calls` | array | 도구 호출(없으면 빈 배열) |
| `choices[].finish_reason` | string | `stop` \| `length` \| `tool_calls` |
| `usage.prompt_tokens` / `completion_tokens` / `total_tokens` | integer | 토큰 수 |

### Responses API 응답 (`object = "response"`)

| 필드 | 타입 | 설명 |
| --- | --- | --- |
| `id` | string | 응답 ID |
| `created_at` | integer | Unix 시각(초) |
| `status` | string | `completed` \| `incomplete` 등 |
| `output[].type` | string | `message` (thinking을 켜면 `reasoning`도 올 수 있음) |
| `output[].content[].type` | string | `output_text` \| `refusal` |
| `output[].content[].text` | string | 구조화 출력 JSON 문자열 |
| `usage.input_tokens` / `output_tokens` / `total_tokens` | integer | 토큰 수 |

### vLLM 오류 응답 (`error = upstream_error`)

| 필드 | 타입 | 설명 |
| --- | --- | --- |
| `object` | string | `error` |
| `type` | string | 예: `BadRequestError` |
| `code` | integer | HTTP 코드 |
| `message` | string | 오류 문장 |

### 스트리밍 원문 (`response_text`)

`data: {chat.completion.chunk JSON}\n\n` 이벤트의 연속이며 정상 종료면 `data: [DONE]`으로 끝납니다.
`choices[].delta.content`를 이어 붙이면 답변 본문이 되고, `include_usage`를 켰으면 마지막 이벤트에 `usage`가 있습니다.

## 7. 모의 데이터

업무 DB 원천(`mcp_servers/data/all_cases.jsonl`, 가상 40건)의 대화를 바탕으로 만든 **가상 데이터**입니다.
업무 DB 적재기(`db-init`)가 그대로 `llm_exchanges`에 넣으므로 따로 넣을 필요가 없고, `messages` 뷰가 원천 대화 182개를 순서·내용·시각까지 그대로 돌려줍니다.

| 파일 | 내용 |
| --- | --- |
| `llm_proxy/mock/build_mock.py` | 생성기. 같은 입력이면 항상 같은 결과(시드 고정) |
| `mcp_servers/db/loader/chat_log.py` | 직원 대화 → 호출 변환 규칙. 적재기와 생성기가 같이 씀 |
| `mcp_servers/data/llm_exchanges.jsonl` | 생성 결과. 1줄 = 1행, 2장의 컬럼 순서 |

```bash
.venv/bin/python llm_proxy/mock/build_mock.py          # 다시 만들기 (프로젝트 루트)
docker compose run --rm db-init                        # 업무 DB에 다시 적재 (mcp_servers/에서)
```

### 구성 (237행, 2026-10-04 ~ 2026-10-20)

| member | stage | 행 수 | 내용 |
| --- | --- | --- | --- |
| `workspace-backend` | `workspace_chat` | 111 | 질문 1개 = 호출 1건(모두 스트리밍). 답변 72건 + 답 없이 끊긴 질문 38건 + 다시 보내기 전 끊긴 호출 1건. 직원 9명 |
| `eval-agent` | `classify` | 41 | 40건 + 입력 길이 초과(400) 1건 |
| `eval-agent` | `rubric` | 41 | 40건 + 검증 실패 재요청 1건 |
| `eval-agent` | `evaluate` | 41 | 40건 + 시간 초과(504) 1건 |
| `dev-local` | NULL | 3 | 서버 기동 직후 개발자 테스트(업무·사용자 NULL) |

### 일부러 넣은 예외 상황

| 업무 | 행 | 상황 |
| --- | --- | --- |
| 36개 업무 | `workspace_chat` | "저장할게"처럼 답이 없는 마지막 질문 → 답을 받기 전에 끊긴 호출(`client_disconnected`). 뷰에는 질문만 보임 |
| `W-E2E-PR-10`, `W-E2E-QT-01` | `workspace_chat` | 답을 받기 전에 질문을 하나 더 보냄 → 첫 질문은 끊긴 호출, 다음 호출 이력에 두 질문이 함께 들어감 |
| `W-E2E-IQ-02` | `workspace_chat` | 첫 답변 도중 창을 닫음 → 잘린 SSE. 같은 질문을 다시 보낸 호출은 정상(뷰는 질문을 한 번만 셈) |
| `W-E2E-RFP-05` | `classify` | `400 upstream_error`(최대 컨텍스트 초과) → 12분 뒤 재실행 성공 |
| `W-E2E-PR-04` | `rubric` | 첫 초안이 항목 4개뿐이라 검증 실패 → 이전 답과 수정 지시를 붙여 재요청 |
| `W-E2E-QT-03` | `evaluate` | `504 upstream_timeout`(600초) → 8분 뒤 새 `run_id`로 재실행 성공 |

### 만든 방법과 한계

- **직원 대화**
  - 원천의 사용자 메시지 1개를 호출 1건으로 만들고, `request.messages`에는 시스템 프롬프트와 그때까지의 대화를 넣었습니다.
  - 메시지 시각을 원천과 맞추려고, 직원 대화의 `latency_ms`는 질문부터 답변(또는 끊김)까지의 간격으로 두었습니다. 그래서 실제 처리 시간이 아니라 사람이 다음 메시지를 보낸 간격이고, 수 분 단위입니다.
- **Agent 호출**: 시스템 프롬프트는 현재 코드의 실제 프롬프트를 썼습니다. 다만 `text.format.schema`와 내규 본문은 `"(축약)"`으로 줄였고, 분류·기준·평가 결과는 형식만 맞춘 모의 값입니다.
- **토큰 수**: 글자 1.4개당 1토큰으로 추정했습니다(실측 아님).
- **Agent·개발 호출의 소요 시간**: L4 GPU와 Gemma 4 26B-A4B 4bit를 가정해 계산했습니다(입력 처리 약 6,600 tok/s, 생성 약 45 tok/s, ±10~15% 변동). 실제 서버에서 측정한 값으로 바꿔야 합니다.

## 8. 조회 예시

```sql
-- 한 업무의 LLM 사용 전체
SELECT id, created_at, stage, user_id, status_code, error, latency_ms, input_tokens, output_tokens
FROM llm_exchanges WHERE work_id = 'W-E2E-QT-03' ORDER BY id;

-- 단계별 평균 소요 시간과 토큰
SELECT stage, count(*), round(avg(latency_ms)) AS avg_ms,
       round(avg(input_tokens)) AS avg_in, round(avg(output_tokens)) AS avg_out
FROM llm_exchanges WHERE error IS NULL GROUP BY stage ORDER BY stage;

-- 직원별 대화 호출 수
SELECT u.employee_no, u.name, count(*) AS calls, count(DISTINCT e.work_id) AS works
FROM llm_exchanges e JOIN users u USING (user_id)
WHERE e.stage = 'workspace_chat' GROUP BY 1, 2 ORDER BY calls DESC;

-- 한 업무의 대화 (messages 뷰)
SELECT seq, role, left(content, 60) AS content, created_at, exchange_id
FROM messages WHERE work_id = 'W-E2E-IQ-02' ORDER BY seq;

-- Agent 분류 결과(구조화 출력 JSON)
SELECT work_id, (response->'output'->0->'content'->0->>'text')::jsonb->'decision'->'task'->>'work_type'
FROM llm_exchanges WHERE stage = 'classify' AND error IS NULL;
```
