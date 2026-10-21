-- AX 평가 Agent 업무 DB (PostgreSQL 16)
-- business-mcp가 읽고 쓰는 원본 저장소. rag-mcp 검색 색인(ChromaDB)도 documents·document_chunks·rubric_items에서 만든다.
-- 텍스트 원문(content)과 마스킹본(content_masked)을 같이 둔다: 정책 검사는 원문, MCP 응답은 마스킹본 (명세 7장 기본안 2).

BEGIN;

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

CREATE TABLE IF NOT EXISTS users (
  user_id     text PRIMARY KEY,
  company_id  text NOT NULL REFERENCES companies,
  team_id     text REFERENCES teams,
  name        text NOT NULL,
  role        text NOT NULL CHECK (role IN ('employee','team_lead','reviewer','ax_manager','admin')),
  test_token  text
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
  work_type            text NOT NULL CHECK (work_type IN ('rfp_analysis','proposal','quote','inquiry_reply')),
  customer_id          text NOT NULL REFERENCES customers,
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

CREATE TABLE IF NOT EXISTS messages (
  message_id      text PRIMARY KEY,
  work_id         text NOT NULL REFERENCES work_cases ON DELETE CASCADE,
  seq             int  NOT NULL,
  role            text NOT NULL CHECK (role IN ('user','assistant')),
  content         text NOT NULL,
  content_masked  text NOT NULL,
  created_at      timestamptz NOT NULL,
  UNIQUE (work_id, seq)
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

-- ─────────────────────────── 인덱스 ───────────────────────────
CREATE INDEX IF NOT EXISTS ix_messages_work        ON messages (work_id, seq);
CREATE INDEX IF NOT EXISTS ix_artifact_versions_w  ON artifact_versions (work_id, saved_at);
CREATE INDEX IF NOT EXISTS ix_spans_artifact       ON artifact_spans (artifact_id, version, seq);
CREATE INDEX IF NOT EXISTS ix_source_docs_work     ON source_documents (work_id, uploaded_at);
CREATE INDEX IF NOT EXISTS ix_events_work          ON work_events (work_id, event_type);
CREATE INDEX IF NOT EXISTS ix_human_actions_work   ON human_actions (work_id, action, at);
CREATE INDEX IF NOT EXISTS ix_evaluations_work     ON evaluations (work_id);
CREATE INDEX IF NOT EXISTS ix_audit_eval           ON audit_log (evaluation_id, ts);
CREATE INDEX IF NOT EXISTS ix_chunks_clause        ON document_chunks (doc_id, version, clause_id);

COMMIT;
