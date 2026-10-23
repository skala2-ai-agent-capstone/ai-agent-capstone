# MCP 담당자에게 요청: 개인 최종 평가 리포트 저장·조회

## 현재 상태

Agent는 `get_evaluation_context`, `get_work_case`, 고정 버전 `search_rubric`, 전체 대화·산출물
조회로 평가하고 `save_evaluation_draft`와 `save_coaching`으로 항목/코칭을 저장한다.
이번에 **개인 평가 페이지용 summary·score·항목·근거·코칭 JSON**을 구성했다.
현재 리포트 전체는 로컬 체크포인트에만 저장한다. 아래 도구는 **추가 제안이며 아직 구현/호출하지 않는다.**
도구명은 담당자와 확정할 수 있다. 기준 생성·기준 저장 로직 변경 요청이 아니다.

## 1. 업무 제목 제공

기존 `get_work_case(work_id)` 응답에 가능하면 `title: string | null`을 추가해 주세요.
현재 서버 반환에는 title이 없어 페이지에 work ID가 표시됩니다. 업무 등록 제목을 사용하고,
없으면 null을 반환하면 됩니다. Agent는 고객명이나 모델 추론으로 제목을 만들지 않습니다.
마스킹·조회 권한은 기존 도구 정책을 따릅니다.

## 2. 리포트 저장 도구 제안: save_evaluation_report

Agent가 기존 초안·코칭 저장을 확인한 뒤 한 번 더 호출하는 도구입니다.
정확한 구조는 `python -m ai_agents.cli.final_report schemas`의 `FinalReportSaveRequest`를
확인해 주세요. 가상 예시: `samples/final/page-save-request-v1.json`.

```text
evaluation_id
operation_id
draft_revision        기존 save_evaluation_draft 응답의 리비전
coaching_revision     기존 save_coaching 응답의 리비전
report                FinalEvaluationPage JSON (schema_version="1")
report_sha256         내용 대조용 해시
idempotency_key       final:<평가/작업 ID 해시>:report:v1
```

`report`는 업무·산출물 ID/버전, 기준 ID/버전/해시, 원본 실행 ID·스냅샷 해시,
합계와 산정 범위, 종합 요약, 원본 rubric item ID 5개, 항목별 점수/상태/이유/코칭,
업무 인용(source_id/source_kind/quote), 기준 인용(document_id/version/section_id/quote)을 담습니다.

요청 처리 조건:

1. evaluation 토큰의 회사·평가 건·업무 범위에 속해야 합니다. 본문을 권한 근거로 삼지 않습니다.
2. 두 리비전이 해당 평가 건의 현재 저장 결과와 일치하는지 확인합니다. 다르면 VERSION_CONFLICT.
3. report의 평가/업무/기준/산출물 버전·스냅샷이 해당 실행과 맞는지 확인합니다.
4. report의 항목 ID·점수·이유·코칭은 연결한 초안/코칭 리비전과 대조합니다.
   초안 저장은 자료 부족/검토 필요를 모두 '검토 필요'로 합치지만 report는 원래 구분을 보존합니다.
5. 같은 키+같은 본문은 기존 응답을 반환합니다. 같은 키+다른 본문은 충돌로 거부합니다.
6. report는 개인정보가 포함될 수 있는 개인 평가 내용입니다. 기존 마스킹·보관·조회 정책을 적용합니다.
7. 새 보고서 리비전과 저장 응답은 원자적으로 기록합니다. 연결된 초안/코칭이 변경되면
   기존 보고서를 최신 결과처럼 반환하지 말고 재생성 필요/버전 충돌로 처리합니다.
8. 이 저장만으로 '확정'으로 전환하거나 팀 집계에 반영하지 않습니다.

해시는 `sha256(json.dumps(report, ensure_ascii=False, sort_keys=True).encode("utf-8"))`입니다.
현재 Python 기본 separators(쉼표 뒤/콜론 뒤 공백 포함)를 사용합니다.
다른 언어 구현은 동일 JSON 직렬화 규칙을 맞추거나 서버에서 정규화해야 합니다.
내용 해시는 인증 서명이나 권한 증명이 아닙니다.

응답 제안 (기존 MCP ok/data/meta envelope 안의 data):

```json
{
  "evaluation_id": "EVAL-EXAMPLE",
  "operation_id": "final-v1",
  "report_revision": 1,
  "draft_revision": 1,
  "coaching_revision": 1,
  "report_sha256": "<저장된 report의 SHA256>",
  "saved_at": "2026-10-23T12:00:00+09:00",
  "duplicate": false
}
```

스냅샷 해시는 JSON을 ensure_ascii=False, sort_keys=True, separators=(",", ":")로 직렬화한
SHA256입니다. `source_run_id`는 최초 평가 결과의 run ID이므로 재개 때 재발급된 토큰의 run ID와
다를 수 있습니다. **run ID가 다르다는 이유만으로 거부하지 말고**, 동일 평가 건·고정 스냅샷인지
권한 있는 서버 기록으로 확인해야 합니다. 최신 대화 범위로 임의 변경하면 안 됩니다.

## 3. 개인 리포트 조회 제안: get_evaluation_report

프론트는 로그인된 사용자용 백엔드 API를 호출하고 백엔드/MCP가 개인 조회 권한을 검사합니다.
MCP 도구로 노출할지 기존 내부 조회를 확장할지는 담당자 구현에 맡깁니다.

입력: `evaluation_id`, 선택 `report_revision`. 응답에는 위 저장 영수증 정보와 `report`를 반환합니다.
보고서가 없으면 NOT_FOUND, 결과 리비전이 바뀌었으면 VERSION_CONFLICT 등 명확한 상태로 구분해 주세요.
Agent는 화면의 상태를 추측하지 않으며 현재 get_evaluation_context를 대체해 쓰지 않습니다.

백엔드가 화면 응답에 별도로 넣을 항목:

- 서버 정본 상태(`확인 대기`, `검토 대기`, `확정` 등), 저장·확정 시각.
- 로그인 사용자 기준 확인/이의 제기 허용 여부. 서버 API에서도 같은 권한 검사 필요.
- 이의 제기 시 대상 원본 `item_id` 목록과 `report_revision`을 받아 이전 리포트와 혼동하지 않기.
- 본인의 평가만 조회 가능. company/team ID를 알거나 관리자라는 이유로 개인 내용 접근 허용 금지.

기존 `request_review`는 팀장을 검토자로 배정합니다. 관리자에게 개인 내용을 보여주지 않는다는
현재 요구사항과 충돌할 수 있으므로 검토자 역할/접근 범위를 합의하기 전 연결하지 않습니다.
단순 버튼 숨김이나 Agent의 JSON 필드는 접근 제어를 대신하지 않습니다.

## 도구가 준비되면 Agent 쪽에서 할 작업

1. 최종 평가 전용 MCP 허용 목록에 합의한 저장 도구만 추가.
2. 기존 두 저장 뒤 → report 저장 → report 영수증 체크포인트 기록 순서로 연결.
3. 중복/응답 유실/버전 충돌 테스트 추가, worker 완료 알림에 필요한 report_revision 연결.
4. 개인 페이지에서는 서버가 반환한 정본 리포트와 상태를 사용.

현재 완료 이벤트 `FINAL_EVALUATION_SAVED`는 **초안·코칭 저장 완료** 의미입니다.
리포트 저장 도구가 연결되기 전에는 페이지 전체를 서버에서 조회할 수 있다는 의미로 쓰지 마세요.
이후 완료 이벤트의 의미/버전은 백엔드와 함께 확정해야 합니다.
