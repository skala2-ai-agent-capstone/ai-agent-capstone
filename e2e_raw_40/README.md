# E2E raw 테스트 데이터 40건 (업무 유형별 10건)

서비스 전체를 돌려볼 때 **입력으로 넣는 raw 데이터만** 담았다. 정답지(expected)는 어디에도 없다.
모든 고객사·문서·수치·연락처는 가상이다.

## 구성

| 경로 | 내용 |
|---|---|
| `cases/RFP`, `cases/PR`, `cases/QT`, `cases/IQ` | 케이스 JSON 40개 (`E2E-유형-01` ~ `10`) |
| `all_cases.jsonl` | 같은 40건을 한 줄에 한 케이스로 합친 파일 |
| `scenario_index.xlsx` | 케이스별 상황 요약, RFP 유무, 입력 특징, RFP 유무 분포 |
| `seed/` | 먼저 적재할 기준 데이터 (조직·사용자, 고객 마스터, 가격표·할인 규칙, 정책 규칙, RAG 문서, 과거 업무). 고객 9곳과 직원 2명을 이전 버전에 추가했다 |

## RFP 유무 분포

| 업무 유형 | RFP 있음 | RFP 없음 | 합계 |
|---|---|---|---|
| RFP 요구사항 분석 | 7 | 3 | 10 |
| 고객 맞춤 제안서 | 5 | 5 | 10 |
| 견적서 | 4 | 6 | 10 |
| 고객 질의 답변 | 4 | 6 | 10 |
| 합계 | 20 | 20 | 40 |

`work_case.rfp_status`로 구분한다.

- `첨부`: RFP 원문이 `source_documents`에 들어 있다. 정정 공고가 함께 오면 `첨부 + 정정 공고`.
- `본문 인용(고객 이메일)`: 정식 RFP 문서 없이 이메일 본문에 요구사항이 적혀 있다 (`source_message`).
- `없음`: RFP가 없다. 제안서·견적서·문의 답변은 RFP 없이 진행하는 일반 업무이고, RFP 분석 유형의 `없음`은 고객 이메일 문의, 현장 미팅 메모, 전화 통화 메모처럼 비정형 요구사항을 정리하는 경우다 (ID는 `ASK-`, `MTG-`).
- RFP가 있는 제안서·견적서·문의 답변은 해당 RFP 발췌본이 `source_documents`에 있고, 앞선 RFP 분석 건은 `linked_work_ids`로 연결했다.

## 케이스 JSON 구조

```
case_id, work_type, rfp_status, summary          ← 케이스 식별 정보 (summary는 입력 상황 요약이며 결과·점수는 없음)
raw
 ├ work_case            업무 건 정보, 업무 등록 내용(registration), 고객 메일 원문(source_message)
 ├ source_documents     고객 제출 문서 (RFP, 정정 공고, 미팅·통화 메모). 마스킹 전 원문
 ├ attached_references  업무에 지정한 사내 자료 (seed/documents.json 기준)
 ├ messages             직원(user)과 업무 AI(assistant)의 대화
 ├ artifact_versions    산출물 버전별 본문과 구간(span) ID
 ├ quote_data / approval_requests   (견적서만) 견적 구조화 데이터, 직원이 올린 승인 요청
 │                       quote_data.v*.exceptions: 예외 승인 대상(SLA 상향·결제 조건 변경 등)
 ├ reference_access_log 자료 열람 기록
 ├ evidence_links       직원이 산출물 구간에 연결한 근거
 ├ events               메시지 큐로 들어올 이벤트
 ├ human_actions        사람의 행동 (확인, 이의 제기, 검토 결정)
 ├ fault_injection      일부러 일으킬 장애
 └ access_attempts      조회 권한 시험용 접근 시도
```

## 쓰는 법

1. `seed/`를 적재한다 (사용자, 고객, 가격표, 정책 규칙, RAG 색인).
2. 케이스의 `raw`를 업무 API와 메시지 큐에 넣는다. `fault_injection`은 해당 시점에 장애를 주입하라는 지시다.
3. 서비스가 만든 결과(마스킹, 정책 위반, 평가, 상태 전이, 큐 처리, 접근 결과)는 별도로 수집한다. 비교용 정답이 필요하면 `e2e_testdata/`의 4건(`E2E-*-01`)에만 있다.

## 참고

- 각 유형의 01번은 이전에 만든 E2E 케이스를 expected 없이 그대로 재사용했다 (`rfp_status`와 `summary` 필드만 추가).
- 케이스마다 직원 작업 방식이 다르다 (원문과 설명서를 대조해 교정하는 경우, AI 초안을 그대로 저장하는 경우, 내부 자료를 대화에 넣는 경우, 지시가 짧거나 등록 정보가 비어 있는 경우 등). 구체적인 입력 특징은 `scenario_index.xlsx`의 마지막 열에 있다.
- 견적서 케이스는 최종본의 소계·합계를 단가×수량×기간과 할인율로 다시 계산해 일치함을 확인했다.
