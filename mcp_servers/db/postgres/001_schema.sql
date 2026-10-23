-- AX 평가 Agent 업무 DB (PostgreSQL 16)
-- business-mcp가 읽고 쓰는 원본 저장소. rag-mcp 검색 색인(ChromaDB)도 documents·document_chunks·rubric_items에서 만든다.
-- 텍스트 원문(content)과 마스킹본(content_masked)을 같이 둔다: 정책 검사는 원문, MCP 응답은 마스킹본 (명세 7장 기본안 2).
-- 업무 공간 대화는 기록 프록시가 llm_exchanges에 저장하고, messages는 그것을 펼쳐 보여주는 뷰다.
-- 대화의 마스킹본은 저장하지 않고 뷰를 읽을 때 policy_rules의 마스킹 규칙으로 만든다(mask_text).
-- 기준 문서: db-schema.json(2026-10-22). 백엔드 소유 테이블(outbox_events, evaluation_failures,
-- aggregation_approvals)은 aworko-server Flyway가 만들므로 여기서 만들지 않는다.

BEGIN;

-- 이전 스키마(users.test_token 등)로 만든 DB에는 덮어 올리지 않는다. 볼륨을 지우고 새로 만든다.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.columns
             WHERE table_schema = current_schema() AND table_name = 'users' AND column_name = 'test_token') THEN
    RAISE EXCEPTION '이전 스키마로 만든 DB입니다. docker compose down -v 로 볼륨을 지운 뒤 다시 올리세요';
  END IF;
END $$;

-- ─────────────────────────── 조직·고객 ───────────────────────────
CREATE TABLE IF NOT EXISTS companies (
  company_id            text PRIMARY KEY,
  name                  text NOT NULL,
  min_aggregation_size  int  NOT NULL DEFAULT 5
);

CREATE TABLE IF NOT EXISTS teams (
  team_id     text PRIMARY KEY,
  company_id  text NOT NULL REFERENCES companies,
  name        text NOT NULL
);

-- 로그인 계정 겸용. 백엔드가 사번 + 비밀번호로 로그인시키고 JWT를 발급한다(서버 세션 없음).
CREATE TABLE IF NOT EXISTS users (
  user_id      text PRIMARY KEY,                   -- 내부 식별자. 다른 테이블 FK가 참조
  employee_no  int  NOT NULL UNIQUE,               -- 사번 = 로그인 ID
  password     text NOT NULL,                      -- 평문(단순 프로젝트용). API 응답·로그에 내보내지 않는다
  company_id   text NOT NULL REFERENCES companies,
  team_id      text REFERENCES teams,
  name         text NOT NULL,
  role         text NOT NULL CHECK (role IN ('member','team_lead'))  -- team_lead는 검토자 겸임
);

CREATE TABLE IF NOT EXISTS customers (
  customer_id         text PRIMARY KEY,
  company_id          text NOT NULL REFERENCES companies,
  name                text NOT NULL,
  approved_reference  boolean NOT NULL DEFAULT false
);

-- ─────────────────────────── 가격·할인 규칙 ───────────────────────────
CREATE TABLE IF NOT EXISTS products (
  code  text PRIMARY KEY,
  name  text NOT NULL,
  unit  text NOT NULL
);

CREATE TABLE IF NOT EXISTS price_tables (
  version         text PRIMARY KEY,
  status          text NOT NULL CHECK (status IN ('현행','폐기','초안')),
  effective_from  date,
  effective_to    date
);

CREATE TABLE IF NOT EXISTS price_table_items (
  version     text   NOT NULL REFERENCES price_tables ON DELETE CASCADE,
  code        text   NOT NULL REFERENCES products,
  unit_price  bigint NOT NULL,
  PRIMARY KEY (version, code)
);

CREATE TABLE IF NOT EXISTS discount_components (
  seq        int PRIMARY KEY,
  type       text NOT NULL,
  condition  text NOT NULL,
  pct        numeric(5,2) NOT NULL
);

CREATE TABLE IF NOT EXISTS approval_tiers (
  max_pct            numeric(5,2) PRIMARY KEY,
  approver           text NOT NULL,
  approval_required  boolean NOT NULL
);

CREATE TABLE IF NOT EXISTS exception_rules (
  type      text PRIMARY KEY,
  approver  text NOT NULL
);

CREATE TABLE IF NOT EXISTS pricing_policy (
  key    text PRIMARY KEY,   -- rounding, source
  value  text NOT NULL
);

-- ─────────────────────────── 정책 규칙 ───────────────────────────
CREATE TABLE IF NOT EXISTS policy_rules (
  rule_id       text PRIMARY KEY,
  seq           int  NOT NULL,
  kind          text NOT NULL CHECK (kind IN ('masking','violation')),
  pattern       text,
  replace_with  text,
  targets       text[],
  clause        text,
  description   text,
  detect        text
);

CREATE TABLE IF NOT EXISTS policy_meta (
  key    text PRIMARY KEY,   -- scope_note
  value  text NOT NULL
);

-- ─────────────────────────── 사내 문서 (색인 원본) ───────────────────────────
CREATE TABLE IF NOT EXISTS documents (
  doc_id          text NOT NULL,
  version         text NOT NULL,
  company_id      text NOT NULL REFERENCES companies,
  title           text NOT NULL,
  grade           text NOT NULL,               -- 사내·대외비·기밀
  collection      text CHECK (collection IN ('references','policies','rubrics','coaching_examples')),
  indexed         boolean NOT NULL,             -- false = 기밀, 색인 금지 (정책 검사에만 사용)
  status          text NOT NULL,                -- 현행·폐기·초안
  effective_from  date,
  content         text,
  work_type       text,                         -- rubrics·coaching_examples
  item            text,                         -- coaching_examples
  score_level     int,                          -- coaching_examples
  source_file     text,
  note            text,
  PRIMARY KEY (doc_id, version),
  CHECK (indexed OR collection IS NULL)
);

CREATE TABLE IF NOT EXISTS document_chunks (
  doc_id     text NOT NULL,
  version    text NOT NULL,
  chunk_id   text NOT NULL,
  seq        int  NOT NULL,
  clause_id  text,
  label      text,
  text       text NOT NULL,
  PRIMARY KEY (doc_id, version, chunk_id),
  FOREIGN KEY (doc_id, version) REFERENCES documents ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS rubric_items (
  doc_id            text NOT NULL,
  version           text NOT NULL,
  item_id           text NOT NULL,
  seq               int  NOT NULL,
  item              text NOT NULL,
  criterion         text NOT NULL,
  score_2           text NOT NULL,
  score_1           text,
  score_0           text NOT NULL,
  unobserved        text,
  needs_review      text,
  evidence_sources  text[] NOT NULL,
  related_clauses   text[] NOT NULL,
  PRIMARY KEY (doc_id, version, item_id),
  FOREIGN KEY (doc_id, version) REFERENCES documents ON DELETE CASCADE
);

-- ─────────────────────────── 업무 데이터 (케이스 raw) ───────────────────────────
CREATE TABLE IF NOT EXISTS historical_works (
  work_id            text PRIMARY KEY,
  company_id         text NOT NULL REFERENCES companies,
  team_id            text REFERENCES teams,
  employee_id        text REFERENCES users,
  work_type          text NOT NULL,
  customer_id        text REFERENCES customers,
  status             text NOT NULL,
  completed_at       timestamptz,
  final_doc_id       text,
  final_doc_version  text,
  evaluated          boolean NOT NULL DEFAULT false,
  note               text
);

CREATE TABLE IF NOT EXISTS work_cases (
  work_id              text PRIMARY KEY,
  case_id              text UNIQUE,              -- E2E 시나리오 ID (테스트 데이터일 때)
  company_id           text NOT NULL REFERENCES companies,
  team_id              text REFERENCES teams,
  employee_id          text NOT NULL REFERENCES users,
  work_type            text CHECK (work_type IN ('rfp_analysis','proposal','quote','inquiry_reply')),  -- NULL = 아직 분류 전
  customer_id          text NOT NULL REFERENCES customers,
  title                text,                     -- 「새 업무 작성」의 업무명
  status               text NOT NULL,
  created_at           timestamptz NOT NULL,
  completed_at         timestamptz,
  rfp_status           text,
  registration         jsonb,                    -- NULL = 등록 기록 없음, 빈 문자열 값 = 등록했지만 비어 있음
  registration_masked  jsonb,
  scenario_summary     text
);

CREATE TABLE IF NOT EXISTS source_messages (       -- 고객 요청·질문 원문 (메일 등)
  work_id         text PRIMARY KEY REFERENCES work_cases ON DELETE CASCADE,
  channel         text,
  received_at     timestamptz,
  content         text NOT NULL,
  content_masked  text NOT NULL
);

CREATE TABLE IF NOT EXISTS work_links (
  work_id         text NOT NULL REFERENCES work_cases ON DELETE CASCADE,
  linked_work_id  text NOT NULL,                 -- work_cases 또는 historical_works
  seq             int  NOT NULL,
  PRIMARY KEY (work_id, linked_work_id)
);

CREATE TABLE IF NOT EXISTS reused_sources (
  work_id      text NOT NULL REFERENCES work_cases ON DELETE CASCADE,
  doc_id       text NOT NULL,
  version      text NOT NULL,
  recorded_at  timestamptz,
  PRIMARY KEY (work_id, doc_id, version)
);

CREATE TABLE IF NOT EXISTS source_documents (      -- 고객 제출 원문 첨부 (RFP·정정 공고·메모)
  attachment_id   text PRIMARY KEY,
  work_id         text NOT NULL REFERENCES work_cases ON DELETE CASCADE,
  doc_id          text NOT NULL,
  doc_type        text NOT NULL CHECK (doc_type IN ('rfp','rfp_correction','meeting_note','call_note')),
  version         text NOT NULL,
  grade           text,
  uploaded_by     text REFERENCES users,
  uploaded_at     timestamptz NOT NULL,
  content         text NOT NULL,
  content_masked  text NOT NULL
);

CREATE TABLE IF NOT EXISTS attached_references (   -- 업무에 지정한 사내 자료 → 스냅샷 reference_refs
  work_id  text NOT NULL REFERENCES work_cases ON DELETE CASCADE,
  doc_id   text NOT NULL,
  version  text NOT NULL,
  seq      int  NOT NULL,
  PRIMARY KEY (work_id, doc_id, version)
);

-- LLM 호출 원본. 기록 프록시(llm_proxy)가 호출 1회마다 1행을 쓴다. 업무 공간 대화의 원본이므로 지우지 않는다.
-- 명세: docs/llm_exchanges_data_spec.md
CREATE TABLE IF NOT EXISTS llm_exchanges (
  id             bigserial PRIMARY KEY,
  created_at     timestamptz NOT NULL DEFAULT now(),  -- 응답이 끝나 기록한 시각 (= 답변 시각)
  member         text,                           -- 호출한 쪽(프록시 키 이름)
  endpoint       text,
  stage          text CHECK (stage IN ('workspace_chat','classify','rubric','evaluate','live_evaluate')),  -- X-Stage
  work_id        text REFERENCES work_cases ON DELETE CASCADE,  -- X-Work-Id
  user_id        text REFERENCES users ON DELETE SET NULL,      -- X-User-Id
  model          text,
  stream         boolean DEFAULT false,
  status_code    int,
  error          text,                           -- upstream_error·upstream_unreachable·upstream_timeout·upstream_stream_broken·client_disconnected
  latency_ms     int,                            -- created_at - latency_ms = 요청 시각 (= 사용자 메시지 시각)
  input_tokens   int,
  output_tokens  int,
  request        jsonb,                          -- 대화 이력 포함
  response       jsonb,
  response_text  text                            -- 스트리밍 SSE 원문 또는 JSON이 아닌 응답
);

CREATE TABLE IF NOT EXISTS artifact_versions (
  artifact_id     text NOT NULL,
  version         text NOT NULL,
  work_id         text NOT NULL REFERENCES work_cases ON DELETE CASCADE,
  author          text NOT NULL CHECK (author IN ('ai_draft','employee')),
  saved_at        timestamptz NOT NULL,
  content         text NOT NULL,
  content_masked  text NOT NULL,
  PRIMARY KEY (artifact_id, version)
);

CREATE TABLE IF NOT EXISTS artifact_spans (
  span_id      text PRIMARY KEY,                 -- {artifact_id}:{version}:{key}
  artifact_id  text NOT NULL,
  version      text NOT NULL,
  seq          int  NOT NULL,
  span_key     text NOT NULL,                    -- REQ-06, L07 …
  text         text NOT NULL,
  text_masked  text NOT NULL,
  FOREIGN KEY (artifact_id, version) REFERENCES artifact_versions ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS reference_access_logs (
  work_id      text NOT NULL REFERENCES work_cases ON DELETE CASCADE,
  seq          int  NOT NULL,
  user_id      text NOT NULL REFERENCES users,
  doc_id       text NOT NULL,
  version      text NOT NULL,
  accessed_at  timestamptz NOT NULL,
  PRIMARY KEY (work_id, seq)
);

CREATE TABLE IF NOT EXISTS evidence_links (
  link_id     text PRIMARY KEY,
  work_id     text NOT NULL REFERENCES work_cases ON DELETE CASCADE,
  span_id     text NOT NULL REFERENCES artifact_spans,
  doc_id      text NOT NULL,
  version     text NOT NULL,
  location    text,
  created_by  text REFERENCES users,
  created_at  timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS work_events (           -- 큐 이벤트 (백엔드가 소비)
  event_id          text PRIMARY KEY,
  work_id           text NOT NULL REFERENCES work_cases ON DELETE CASCADE,
  event_type        text NOT NULL,
  artifact_version  text,
  rubric_version    text,
  idempotency_key   text,
  occurred_at       timestamptz NOT NULL,
  payload           jsonb NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS human_actions (         -- 본인 확인·이의 제기·검토 결정 등
  action_id         text PRIMARY KEY,
  work_id           text NOT NULL REFERENCES work_cases ON DELETE CASCADE,
  user_id           text NOT NULL REFERENCES users,
  action            text NOT NULL,
  at                timestamptz NOT NULL,
  artifact_version  text,
  payload           jsonb NOT NULL DEFAULT '{}',   -- target_items, cited_evidence, comment, decision, reason
  payload_masked    jsonb NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS quote_data (            -- 견적 구조화 데이터 (견적서 업무)
  work_id              text NOT NULL REFERENCES work_cases ON DELETE CASCADE,
  artifact_version     text NOT NULL,
  quote_no             text,
  price_table          text,
  lines                jsonb NOT NULL,             -- [{code, quantity, months}]
  discount_components  jsonb NOT NULL DEFAULT '[]',
  free_items           jsonb NOT NULL DEFAULT '[]',
  exceptions           jsonb NOT NULL DEFAULT '[]',
  PRIMARY KEY (work_id, artifact_version)
);

CREATE TABLE IF NOT EXISTS approval_requests (
  approval_id       text PRIMARY KEY,
  work_id           text NOT NULL REFERENCES work_cases ON DELETE CASCADE,
  artifact_version  text NOT NULL,
  submitted_at      timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS approval_request_items (
  approval_id  text NOT NULL REFERENCES approval_requests ON DELETE CASCADE,
  seq          int  NOT NULL,
  type         text NOT NULL,
  value        text,
  approver     text,
  reason       text,
  PRIMARY KEY (approval_id, seq)
);

CREATE TABLE IF NOT EXISTS fault_injections (      -- 테스트 장애 주입 계획 (명세 6.2)
  work_id  text NOT NULL REFERENCES work_cases ON DELETE CASCADE,
  seq      int  NOT NULL,
  target   text NOT NULL,                          -- business-mcp.get_conversation, llm_api …
  node     text,
  error    text NOT NULL DEFAULT 'TEMPORARY_FAILURE',
  times    int  NOT NULL DEFAULT 1,
  on_call  int  NOT NULL DEFAULT 1,
  note     text,
  PRIMARY KEY (work_id, seq)
);

CREATE TABLE IF NOT EXISTS access_attempts (       -- 리포트 조회 권한 테스트 (백엔드 API 몫)
  attempt_id  text PRIMARY KEY,
  work_id     text NOT NULL REFERENCES work_cases ON DELETE CASCADE,
  user_id     text NOT NULL REFERENCES users,
  action      text NOT NULL,
  target      text NOT NULL
);

-- ─────────────────────────── 평가 (MCP·백엔드가 씀) ───────────────────────────
CREATE TABLE IF NOT EXISTS evaluations (
  evaluation_id     text PRIMARY KEY,
  work_id           text NOT NULL REFERENCES work_cases,
  company_id        text NOT NULL REFERENCES companies,
  artifact_id       text NOT NULL,
  artifact_version  text NOT NULL,
  rubric_version    text NOT NULL,
  status            text NOT NULL DEFAULT '대기' CHECK (status IN
                      ('대기','수집 중','평가 중','근거 확인 중','코칭 생성 중','확인 대기','검토 대기','확정','처리 실패','자료 보류')),
  source_key        text UNIQUE,                   -- work.completed idempotency_key
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now()
  -- artifact_versions FK는 두지 않는다: 업무 데이터를 다시 적재해도 평가 결과가 남아야 한다
);

CREATE TABLE IF NOT EXISTS evaluation_drafts (
  evaluation_id  text NOT NULL REFERENCES evaluations ON DELETE CASCADE,
  revision       int  NOT NULL,
  items          jsonb NOT NULL,
  saved_at       timestamptz NOT NULL DEFAULT now(),
  run_id         text,
  PRIMARY KEY (evaluation_id, revision)
);

CREATE TABLE IF NOT EXISTS evaluation_coaching (
  evaluation_id  text NOT NULL REFERENCES evaluations ON DELETE CASCADE,
  revision       int  NOT NULL,
  items          jsonb NOT NULL,
  saved_at       timestamptz NOT NULL DEFAULT now(),
  run_id         text,
  PRIMARY KEY (evaluation_id, revision)
);

CREATE TABLE IF NOT EXISTS review_requests (
  review_request_id  text PRIMARY KEY,
  evaluation_id      text NOT NULL REFERENCES evaluations ON DELETE CASCADE,
  target_items       jsonb NOT NULL,
  reason             text NOT NULL,
  review_memo        jsonb NOT NULL,
  reviewer_id        text REFERENCES users,
  created_at         timestamptz NOT NULL DEFAULT now(),
  run_id             text
);

-- 중간 점검·실시간 피드백. 백엔드가 요청 행(대기)을 만들어 큐에 발행하고, Agent가 MCP로 결과를 저장한다.
-- 최종 평가·이의 제기·팀 집계와는 별개다.
CREATE TABLE IF NOT EXISTS live_evaluations (
  live_eval_id    text PRIMARY KEY,              -- 큐 메시지 중복 방지 키
  work_id         text NOT NULL REFERENCES work_cases ON DELETE CASCADE,
  requested_by    text NOT NULL REFERENCES users,
  requested_at    timestamptz NOT NULL DEFAULT now(),  -- 사용자별 5분 1회 제한 판단에 사용
  from_seq        int  NOT NULL,                 -- messages.seq 범위 시작(최근 5개)
  to_seq          int  NOT NULL,                 -- messages.seq 범위 끝
  rubric_doc_id   text,
  rubric_version  text,
  status          text NOT NULL DEFAULT '대기' CHECK (status IN ('대기','생성 중','완료','실패')),  -- 폴링용
  items           jsonb,
  coaching        jsonb,
  model           text,
  latency_ms      int,
  error           text
);

CREATE TABLE IF NOT EXISTS progress_log (
  id             bigserial PRIMARY KEY,
  evaluation_id  text NOT NULL REFERENCES evaluations ON DELETE CASCADE,
  stage          text NOT NULL,
  at             timestamptz NOT NULL DEFAULT now(),
  run_id         text
);

CREATE TABLE IF NOT EXISTS idempotency_keys (
  tool           text NOT NULL,
  key            text NOT NULL,
  evaluation_id  text,
  result         jsonb NOT NULL,
  created_at     timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tool, key)
);

CREATE TABLE IF NOT EXISTS audit_log (             -- 민감정보 원문 없이 요약만 (명세 2장 감사)
  id             bigserial PRIMARY KEY,
  ts             timestamptz NOT NULL DEFAULT now(),
  server         text NOT NULL,
  run_id         text,
  evaluation_id  text,
  tool           text NOT NULL,
  input          jsonb NOT NULL,
  result_code    text NOT NULL,
  elapsed_ms     int
);

-- ─────────────────────────── 대화 뷰 ───────────────────────────
-- policy_rules의 마스킹 규칙을 Masker(common/runtime.py)와 같은 순서로 적용한다: 이메일 → 연락처 → 담당자 → 나머지
CREATE OR REPLACE FUNCTION mask_text(t text) RETURNS text LANGUAGE plpgsql STABLE AS $$
DECLARE
  r record;
BEGIN
  IF t IS NULL THEN
    RETURN NULL;
  END IF;
  FOR r IN SELECT pattern, replace_with FROM policy_rules
           WHERE kind = 'masking' AND pattern IS NOT NULL
           ORDER BY CASE rule_id WHEN 'MASK-EMAIL' THEN 0 WHEN 'MASK-PHONE' THEN 1 WHEN 'MASK-PERSON' THEN 2 ELSE 9 END, seq
  LOOP
    t := regexp_replace(t, r.pattern, r.replace_with, 'g');
  END LOOP;
  RETURN t;
END $$;

-- 스트리밍 응답(SSE 원문)에서 답변 본문만 이어 붙인다. 잘린 마지막 줄 등 JSON이 아닌 줄은 건너뛴다.
CREATE OR REPLACE FUNCTION sse_text(raw text) RETURNS text LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE
  line text;
  ev jsonb;
  ch jsonb;
  out text := '';
BEGIN
  IF raw IS NULL THEN
    RETURN NULL;
  END IF;
  FOREACH line IN ARRAY regexp_split_to_array(raw, E'\n') LOOP
    CONTINUE WHEN line NOT LIKE 'data: {%';
    BEGIN
      ev := substr(line, 7)::jsonb;
    EXCEPTION WHEN others THEN
      CONTINUE;
    END;
    CONTINUE WHEN jsonb_typeof(ev->'choices') IS DISTINCT FROM 'array';
    FOR ch IN SELECT value FROM jsonb_array_elements(ev->'choices') LOOP
      out := out || COALESCE(ch->'delta'->>'content', '');
    END LOOP;
  END LOOP;
  RETURN out;
END $$;

-- 업무 공간 대화 = workspace_chat 호출마다 "이번에 새로 보낸 질문 + 답변 1개".
--  · 새 질문: 요청 이력에서 마지막 assistant 뒤의 user 메시지 (백엔드가 이력을 잘라 보내도 동작)
--  · 앞 호출이 실패(끊김·오류)했으면 그 질문은 이미 보였으므로 재전송분은 세지 않는다
--  · 실패한 호출은 질문만 남고 답변은 없다
--  · 사용자 메시지 시각은 요청 시각(created_at - latency_ms), 답변 시각은 created_at
CREATE OR REPLACE VIEW messages AS
WITH calls AS (
  SELECT e.id, e.work_id,
         e.created_at - make_interval(secs => COALESCE(e.latency_ms, 0) / 1000.0) AS requested_at,
         e.created_at,
         CASE WHEN jsonb_typeof(e.request->'messages') = 'array' THEN e.request->'messages' ELSE '[]'::jsonb END AS msgs,
         CASE WHEN e.error IS NULL
              THEN NULLIF(COALESCE(e.response->'choices'->0->'message'->>'content', sse_text(e.response_text)), '')
         END AS reply
  FROM llm_exchanges e
  WHERE e.stage = 'workspace_chat' AND e.work_id IS NOT NULL
),
asked AS (
  SELECT c.*,
         COALESCE((SELECT jsonb_agg(m->>'content' ORDER BY i)
                   FROM jsonb_array_elements(c.msgs) WITH ORDINALITY AS t(m, i)
                   WHERE m->>'role' = 'user' AND jsonb_typeof(m->'content') = 'string'
                     AND i > COALESCE((SELECT max(j) FROM jsonb_array_elements(c.msgs) WITH ORDINALITY AS a(x, j)
                                       WHERE x->>'role' = 'assistant'), 0)), '[]'::jsonb) AS questions
  FROM calls c
),
with_prev AS (
  SELECT a.*, lag(a.questions) OVER w AS prev_questions, lag(a.reply IS NULL) OVER w AS prev_failed
  FROM asked a
  WINDOW w AS (PARTITION BY a.work_id ORDER BY a.id)
),
fresh AS (
  SELECT p.*,
         CASE WHEN p.prev_failed
                   AND jsonb_array_length(p.prev_questions) <= jsonb_array_length(p.questions)
                   AND (SELECT COALESCE(jsonb_agg(x ORDER BY k), '[]'::jsonb)
                        FROM jsonb_array_elements(p.questions) WITH ORDINALITY AS q(x, k)
                        WHERE k <= jsonb_array_length(p.prev_questions)) = p.prev_questions
              THEN jsonb_array_length(p.prev_questions) ELSE 0 END AS already_shown
  FROM with_prev p
),
rows AS (
  SELECT f.work_id, f.id AS exchange_id, q.k AS pos, 'user' AS role, q.x #>> '{}' AS content, f.requested_at AS created_at
  FROM fresh f, LATERAL jsonb_array_elements(f.questions) WITH ORDINALITY AS q(x, k)
  WHERE q.k > f.already_shown
  UNION ALL
  SELECT f.work_id, f.id, 2147483647, 'assistant', f.reply, f.created_at
  FROM fresh f
  WHERE f.reply IS NOT NULL
),
ordered AS (
  SELECT r.*, row_number() OVER (PARTITION BY r.work_id ORDER BY r.exchange_id, r.pos)::int AS seq
  FROM rows r
)
SELECT work_id || '-M' || lpad(seq::text, greatest(2, length(seq::text)), '0') AS message_id,
       work_id, seq, role, content, mask_text(content) AS content_masked, created_at, exchange_id
FROM ordered;

-- ─────────────────────────── 인덱스 ───────────────────────────
CREATE INDEX IF NOT EXISTS ix_llm_exchanges_work   ON llm_exchanges (work_id, stage, id);
CREATE INDEX IF NOT EXISTS ix_llm_exchanges_user   ON llm_exchanges (user_id);
CREATE INDEX IF NOT EXISTS ix_llm_exchanges_time   ON llm_exchanges (created_at);
CREATE INDEX IF NOT EXISTS ix_live_evals_work      ON live_evaluations (work_id, requested_at);
CREATE INDEX IF NOT EXISTS ix_live_evals_user      ON live_evaluations (requested_by, requested_at);
CREATE INDEX IF NOT EXISTS ix_users_team_role      ON users (team_id, role);
CREATE INDEX IF NOT EXISTS ix_artifact_versions_w  ON artifact_versions (work_id, saved_at);
CREATE INDEX IF NOT EXISTS ix_spans_artifact       ON artifact_spans (artifact_id, version, seq);
CREATE INDEX IF NOT EXISTS ix_source_docs_work     ON source_documents (work_id, uploaded_at);
CREATE INDEX IF NOT EXISTS ix_events_work          ON work_events (work_id, event_type);
CREATE INDEX IF NOT EXISTS ix_human_actions_work   ON human_actions (work_id, action, at);
CREATE INDEX IF NOT EXISTS ix_evaluations_work     ON evaluations (work_id);
CREATE INDEX IF NOT EXISTS ix_audit_eval           ON audit_log (evaluation_id, ts);
CREATE INDEX IF NOT EXISTS ix_chunks_clause        ON document_chunks (doc_id, version, clause_id);

COMMIT;
