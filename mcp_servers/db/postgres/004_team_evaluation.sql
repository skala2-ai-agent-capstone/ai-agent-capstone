-- 이전 DB용 비파괴 변경(팀 평가). 새 DB는 001_schema.sql에 이미 포함한다. 여러 번 실행해도 결과가 같다.
-- 적용한 뒤 business-mcp를 재시작한다. 기존 평가·기준·대화는 바꾸지 않는다. 계약: docs/team_evaluation.md
CREATE TABLE IF NOT EXISTS team_evaluations (
  request_id             uuid PRIMARY KEY,
  snapshot_id            uuid NOT NULL UNIQUE,
  company_id             text NOT NULL REFERENCES companies,
  team_id                text NOT NULL REFERENCES teams,
  period_start           date NOT NULL,
  period_end             date NOT NULL,
  requested_by           text REFERENCES users,
  requested_at           timestamptz NOT NULL DEFAULT now(),
  release_approved       boolean NOT NULL,
  min_members            int  NOT NULL CHECK (min_members BETWEEN 5 AND 100),
  snapshot               jsonb NOT NULL,
  snapshot_sha256        text NOT NULL,
  source_evaluation_ids  text[] NOT NULL,
  status                 text NOT NULL DEFAULT '대기' CHECK (status IN ('대기','완료')),
  report                 jsonb,
  report_sha256          text,
  revision               int,
  idempotency_key        text,
  saved_at               timestamptz,
  run_id                 text,
  CHECK (period_start < period_end),
  CHECK ((status = '완료') = (report IS NOT NULL))
);
