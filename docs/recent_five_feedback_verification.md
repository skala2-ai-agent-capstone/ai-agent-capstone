# 중간 피드백 구현 검증 기록 — 2026-10-22

브랜치: `feat/recent-five-feedback`.
기준 HEAD: `2a893e1d700b19873978563da386d9b3141c031b`.
이번 구현은 미커밋 작업 트리에서 검증했다. 커밋·push는 수행하지 않았다.
HEAD만 체크아웃하면 이번 신규 코드가 없으므로, 최종 증빙은 사용자가 커밋한 후 재실행해야 한다.

## 관측 결과

| 검증 | 결과 | 범위/한계 |
|---|---:|---|
| `python -m pytest -q` | 608 passed, 5.58초 | 기존 테스트 포함, 실제 서비스 호출 없음 |
| 신규 중간 피드백 테스트 | 39 passed | 위 608개에 포함 |
| `ruff check src tests` | 통과 | Python 정적 검사 |
| 기능 평가셋 실행 A | 12/12 통과 | MCP·모델 고정 응답으로 제어 흐름 확인 |
| 기능 평가셋 실행 B | 12/12 통과 | 같은 코드/같은 데이터 재실행 |
| A/B 비교 | 개선 0, 회귀 0 | 비교 도구 동작 확인; 품질 개선 실험 아님 |
| 품질 평가셋 20건 | 스키마 검사 통과 | 실제 모델 호출·사람 판정 미실행 |
| 기준 생성/기존 최종 평가 디렉터리 diff | 없음 | 팀원 로직 미수정 |
| 기존 의존성 버전 변경 | 없음 | Worker extra용 6개 패키지만 추가 |

신규 테스트는 질문+응답 5쌍, 1쌍 허용, 5개 메시지 오해 방지,
부분·중복·범위 밖·마스킹 누락 거부, 잘못된 인용/추천 대상 거부,
기준 버전 고정, terminal 중복 skip, 저장 재시도 시 LLM 재호출 방지,
retry/DLQ 발행 확인 전 ack 금지, 추적에 원문 미포함을 검사했다.

실제 MCP SDK의 Streamable HTTP를 ASGI 가상 서버에 연결해 인증 헤더와
start→get→save 직렬화/왕복을 확인했다. 운영 MCP의 권한/SQL을 검증한 것은 아니다.
RabbitMQ는 대체 전송 객체로 publisher Ack/Nack/미확인과 수동 ack/requeue 순서를 확인했다.
실제 브로커의 TTL·DLX·재연결·장애 동작을 확인한 것은 아니다.

## 실행 증빙

- A: `outputs/validation/feedback-functional-smoke-a/experiment.json`
  - experiment ID: `fedb5b25-cd3d-406a-a8f9-5f5d526b263e`
- B: `outputs/validation/feedback-functional-smoke-b/experiment.json`
  - experiment ID: `40833b49-b31f-4072-98b0-3ddb9e1373c9`
- 비교: `outputs/validation/feedback-functional-smoke-comparison.json`
- 각 폴더의 `manifest.json`, `dataset.json`, `results.jsonl`, `report.csv`, `traces/`에
  설정·가상 입력·결과·케이스별 추적이 있다. CSV는 UTF-8 BOM으로 저장한다.

두 실행의 공통 식별값:

```text
dirty: true
working_tree_sha256: d40a7d3db0273c53320225b570a2972c1fad6014f345d8f53874db9421cd208a
dataset_sha256: 6b6f4b8018caae9790cd448191b54e872aaad86fcd10a9df8bb5ce59790630cb
target: ai_agents.validation.feedback_targets:replay_feedback
model: fixture-model
prompt_version: recent-five-feedback-v1
rubric_version: none
data_version: synthetic-v1
```

실행 후 이 기록 문서와 설치 안내를 추가했으므로 현재 전체 작업 트리 hash와는 다를 수 있다.
`outputs/`는 Git 무시 대상이다. 위 경로는 현재 로컬 증빙이며 GitHub 업로드된 증빙이 아니다.

## 검증결과서에 적을 수 있는 문장

> 최근 완료된 질문·AI 응답 최대 5쌍을 입력받는 중간 피드백 Agent/Worker를 구현했다.
> 정상·예외·오류 처리 고정 사례 12건을 모의 MCP/LLM 응답으로 실행한 결과 모두 통과했다.
> 실제 MCP SDK의 HTTP 직렬화와 피드백 조회·저장 호출 흐름을 가상 서버에서 확인했다.
> 동일 코드와 평가셋의 반복 실행 및 비교 파일 생성을 확인했으며, 이 결과는 LLM 의미 품질이나
> 운영 시스템 전체의 성공률을 뜻하지 않는다.

## 후속 검증 — 아직 미실행

| 항목 | 필요한 준비 | 남길 기록 |
|---|---|---|
| 실제 MCP·DB 저장 | 새 도구 3개, 테스트 계정·회사, 마스킹 | 요청 ID, 대상 5쌍 ID, 저장 결과, 타 회사/타인 거부 |
| 실제 RabbitMQ 재전달 | 백엔드 큐·outbox·DLQ 소비자 | 장애/재시도/중복 시나리오, 최종 상태, 중복 저장 여부 |
| AWS 모델 호환성 | Chat Completions JSON mode 주소·모델 | 모델 ID, 형식 실패율, 토큰 수, 지연 |
| LangSmith 원격 추적 | API 키·프로젝트, `--langsmith` | 원격 run/trace 링크, 로컬 trace ID와 매칭 |
| 품질 20건 | 실제 모델, 검토자·판정 기준 | 진단 타당성, 추천 유용성, 실제 결과, 검토 의견 |
| 팀원 기준 수정 비교 | 고정 대화 + 기준 v1/v2 export | 기준 hash, 변경 허용 조건, 개선·회귀 케이스, 사람 판정 |

운영 대화나 실제 LLM을 이번 검증에서 호출하지 않았고, LangSmith에 원격 전송하지 않았다.
명령과 팀별 작업 순서는 [구현·연동 가이드](recent_five_feedback.md)를 따른다.
