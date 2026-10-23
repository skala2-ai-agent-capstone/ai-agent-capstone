-- 이전 DB용 비파괴 변경(프론트 연동). 새 DB는 001_schema.sql에 이미 포함한다. 여러 번 실행해도 결과가 같다.
-- · 고객 없이 업무를 만들 수 있다(work_cases.customer_id NULL = 고객 미지정)
-- · 앱에서 올린 고객 자료의 원본 파일을 남긴다. content는 원본에서 꺼낸 텍스트(못 꺼내면 빈 문자열)
ALTER TABLE work_cases ALTER COLUMN customer_id DROP NOT NULL;
ALTER TABLE source_documents
  ADD COLUMN IF NOT EXISTS file_name    text,
  ADD COLUMN IF NOT EXISTS content_type text,
  ADD COLUMN IF NOT EXISTS size_bytes   bigint,
  ADD COLUMN IF NOT EXISTS file_data    bytea;
