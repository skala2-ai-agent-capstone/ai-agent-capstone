-- 이전 DB용 비파괴 변경(팀 평가 공개 기준 하한 5→2). 새 DB는 001_schema.sql·004에 이미 포함한다. 여러 번 실행해도 결과가 같다.
-- 시연 데이터 규모에 맞춘 하한이다. 실제 공개 기준은 companies.min_aggregation_size로 정한다. 계약: docs/team_evaluation.md
ALTER TABLE team_evaluations DROP CONSTRAINT IF EXISTS team_evaluations_min_members_check;
ALTER TABLE team_evaluations ADD CONSTRAINT team_evaluations_min_members_check CHECK (min_members BETWEEN 2 AND 100);
