-- 기존 계약 v1 DB용 비파괴 변경. 새 DB는 001_schema.sql에 이미 포함한다.
-- 실행 중인 DB에 적용한 뒤 business-mcp를 재시작한다. 기존 평가·기준·대화는 변경하지 않는다.
ALTER TABLE live_evaluations ADD COLUMN IF NOT EXISTS conversation_snapshot jsonb;
