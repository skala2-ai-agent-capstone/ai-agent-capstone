# 개인 최종 평가 페이지용 결과

브랜치: `feat/final-evaluation-page-result`, 기반: `0a95d11`.
사용자가 제공한 `index.html`의 `screen-3` 개인 평가 리포트를 기준으로 데이터 구조를 구성했다.
HTML 원본·평가 기준 생성 로직·기존 MCP 도구·큐 이벤트는 변경하지 않았다.

## 화면 연결

| 화면 영역 | 페이지 결과 필드 | 처리 |
| --- | --- | --- |
| 업무 제목·유형 | `work.title`, `work.work_type_label` | 제목이 없으면 work ID 표시. 고객명으로 임의 제목 생성 안 함 |
| 평가 대상 산출물 | `work.artifact_id`, `work.artifact_version` | 평가 시점의 고정 버전 |
| 기준 버전 태그 | `rubric.doc_id`, `rubric.version` | 현재 최신 기준으로 교체하지 않음 |
| 점수 원형 표시 | `score.display`, `score.earned`, `score.possible` | 산정 가능 항목만 합산 |
| 종합 설명 | `summary`, `score.coverage`, `review_required` | 코드로 계산, 임의 개선율·ROI·등급 없음 |
| 항목 카드 | `items[]` | 문제 정의 → 지시 품질 → 결과 검증 → 보안 준수 → 결과물 품질 |
| 점수 표시 | `items[].score`, `status_label` | null과 0점 구분, 보안은 0 또는 2점 |
| 평가 기준 상세 | `criterion`, `score_levels`, `unobserved_condition`, `review_condition` | 저장된 기준의 원문 조건 |
| 판단·업무 근거 | `reason`, `work_evidence[]` | 실제 원본 ID, 출처 종류, 검증된 인용문 |
| 기준·참조 근거 | `rule_evidence[]` | 문서 ID·버전·구간·인용문 보존 |
| 개선 제안 | `coaching` | 기존 평가기의 항목별 코칭, 새로운 LLM 호출 없음 |
| 확인 대기/확정 배지 | **서버가 별도로 제공** | Agent 결과에 상태를 넣어 임의 확정하지 않음 |
| 확인/이의 제기 버튼 | **서버가 별도로 제공** | 개인 조회 권한과 버튼 허용 여부는 백엔드 판단 |

문자열은 일반 텍스트로 렌더링한다. 대화·모델 출력·기준 인용을 HTML로 삽입하지 않는다.
인용문의 `source_id`는 링크 식별자이며 URL이 아니다. 원문 열람은 서버 권한 검사를 거쳐야 한다.
업무/기준 근거는 상세 펼침 영역에 표시할 수 있고, 첫 카드에는 판단 이유를 우선 표시하면 된다.

## 점수와 상태 규칙

- `observed`: 0·1·2점 중 기준이 허용하는 점수. 합계에 포함한다.
- `unobserved`: 미관측. 점수 null, 분모에서도 제외한다.
- `insufficient_data`: 자료 부족. 점수 null, 검토 필요, 분모 제외.
- `needs_review`: 검토 필요. 점수 null, 분모 제외.
- 기존 저장 평가 검증기는 채점 상태와 검토 필요 플래그를 동시에 쓰는 결과를 거부한다.
- 4개 항목에서 7점이면 **7/8**, 5개 모두 채점되면 **합계/10**이다.
  관측 범위를 보존하며 임의로 10점 만점 환산하지 않는다.
- 모두 미채점이면 `earned=null`, `possible=0`, `display="산정 불가"`다.
  프론트에서 `earned / possible`을 계산하지 말고 산정 불가 UI를 표시한다.
- `coverage=partial`은 합산 대상이 일부라는 뜻이며 업무 상태가 임시/미확정이라는 뜻은 아니다.
- 입력 HTML의 고정 `7/10` 예시는 미관측 제외 규칙과 맞지 않아 그대로 사용하지 않았다.

## 실행과 저장 위치

최종 평가 실행은 기존과 같다. `PreparedFinalEvaluation.page_result`에 페이지 내용이 함께 저장된다.
평가 결과·근거를 재검증한 후 구성하므로 확인되지 않은 ID나 인용문이 화면으로 통과하지 않는다.
`page_result`가 없는 이전 체크포인트도 읽으며, 같은 실행으로 재개하면 모델/저장 재호출 없이
페이지 내용을 추가한다. MCP에서 받은 서버 리비전·범위 검사는 기존대로 수행한다.

워크플로 반환값에는 본문 대신 다음 메타데이터만 추가된다.

```json
{"page_result": {"schema_version": "1", "sha256": "<내용 해시>", "persistence": "checkpoint_only"}}
```

`checkpoint_only`는 서버의 리포트 저장이 아직 연결되지 않았다는 뜻이다.
기존 초안·코칭 저장 완료 알림은 유지된다. worker가 개인 리포트 본문을 큐에 발행하지 않는다.

```bash
# 프론트/MCP 담당자에게 전달할 계약
python -m ai_agents.cli.final_report schemas

# 최종 평가 CLI가 반환한 checkpoint 경로를 넣는다. 출력 부모 폴더는 먼저 준비한다.
python -m ai_agents.cli.final_report export \
  --checkpoint outputs/final-checkpoints/<실행해시>.json \
  --output outputs/final-page.json

# 추후 MCP 저장 도구에 전달할 제안 인자까지 생성. 서버 호출은 하지 않는다.
python -m ai_agents.cli.final_report export \
  --checkpoint outputs/final-checkpoints/<실행해시>.json \
  --output outputs/final-page-save-request.json --save-request
```

export는 초안·코칭의 저장 응답이 모두 있는 체크포인트만 허용한다. 저장 응답의 평가 ID·항목 수·
리비전을 확인하고, 캐시된 page_result를 신뢰하지 않고 근거와 평가 결과로 재구성한다.
출력 파일은 0600 권한으로 완성 후 공개하고 기존 파일·symlink는 덮어쓰지 않는다.
표준 출력에는 성공 여부와 해시만 표시한다. 원문·예외 내용·토큰을 출력하지 않는다.

이는 로컬 변환 명령이다. 현재 서버 리비전 변경이나 사용자 조회 권한까지 확인하는 API가 아니다.
운영 리포트 저장/조회에서는 MCP와 백엔드가 동일 리비전·소유자 권한을 다시 확인해야 한다.
파일에 업무 인용이 들어 있으므로 운영 파일은 기존 `outputs/`에 보관하고 Git에 올리지 않는다.
`samples/final/page-*.json`은 가상 E2E 테스트 자료에서 생성한 화면용 예시만 포함한다.

## 파일과 검증

- `shared/final_page_contracts.py`: 화면 데이터·제안 저장 인자의 JSON 계약 및 일관성 검사.
- `evaluation_agent/final/page.py`: 원본 루브릭 ID 매핑, 근거 검증, 점수·요약 계산.
- `orchestration/final_evaluation.py`: 체크포인트에 페이지 결과 추가. MCP 저장 호출 수 유지.
- `cli/final_report.py`: JSON Schema 출력, 명시적 로컬 내보내기.
- `tests/test_final_page.py`: 4개 유형, null 점수, 자료 부족·검토, 잘못된 근거 거부,
  이전 체크포인트 재개, 저장 리비전 연결, 0600 출력, 큐 본문 비공개 검증.

```bash
python -m pytest tests/test_final_page.py -q
python -m pytest tests/test_stored_final_evaluation.py tests/test_final_worker.py -q
```

MCP 담당자에게 전달할 요청은 [MCP 최종 리포트 요청](mcp_final_report_request.md)을 참조한다.
커밋 메시지: `feat: 개인 최종 평가 페이지용 결과 구성과 MCP 연동 계약 추가`

### 구현 검증 기록 — 2026-10-23

- 기준 커밋 `0a95d11`, 위 브랜치의 미커밋 코드로 검증.
- Agent·프록시 전체 검사: **853 passed, 16 skipped**, 7.81초.
- 새 페이지 결과 테스트: 22개 (전체 결과에 포함). 양수 점수 7/8 및 항목 순서 복원 포함.
- Ruff 및 `git diff --check` 통과.
- JUnit: `outputs/validation/final-page/pytest.xml` (Git 무시 대상).
- 16개는 테스트 DB 연결 설정이 필요한 검사로 건너뜀. 실제 모델·운영 MCP·백엔드 화면 연결,
  리포트 DB 저장·개인 조회 권한 및 LangSmith 원격 전송을 확인한 결과는 아님.
- 커밋별 비교 증빙은 커밋 후 같은 명령으로 다시 실행해 남긴다.
