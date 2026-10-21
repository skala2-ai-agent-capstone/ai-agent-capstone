# B 평가 Agent: 모델과 MCP 입력 연결

현재 완료한 범위는 **승인된 평가 입력 → business MCP 읽기 → 응답 검증 → 평가용 자료 준비**입니다.
GPT 평가, 점수의 근거 대조, 코칭 생성은 아직 연결하지 않았습니다. 현재 범위에서는 DB에 저장하지 않습니다.
평가기준 생성 코드는 기존 구현을 그대로 사용합니다.

## 파일 역할

| 파일 | 역할 |
| --- | --- |
| `src/work_classifier/workflow_models.py` | A/B 공통 입력, 기준, 승인, 결과 계약 |
| `src/work_classifier/evidence_models.py` | 등록 내용, 고객 원문, 산출물 구간, 정책 검사, 실행 범위 |
| `src/work_classifier/evaluation_models.py` | B 결과의 점수 범위, 보안 점수, 5개 항목 및 중복 제한 |
| `src/work_classifier/mcp_adapter.py` | SDK 결과 읽기, 응답 변환, 실행·스냅샷·버전 검사, 읽기 도구 연결 |
| `src/work_classifier/evaluator.py` | 승인된 입력과 MCP 근거를 평가용 딕셔너리로 연결 |

## A에서 연결하기

담당자의 승인 이후 생성한 `EvaluationInput`을 전달합니다. 실행 정보는
백엔드의 토큰 발급 응답에서 가져옵니다. LLM에게 토큰이나 실행 범위를 만들게 하지 않습니다.
토큰 자체는 인증된 MCP 클라이언트에만 설정하며 평가 payload에는 넣지 않습니다.
A의 `WorkContext`도 같은 실행의 스냅샷 범위에서 수집해야 합니다. 현재 공통 모델에는
메시지 시각·순번이 없으므로 B는 A의 대화가 그 범위인지 다시 증명할 수 없습니다.

```python
from work_classifier.evaluator import prepare_mcp_evaluation_payload
from work_classifier.evidence_models import EvaluationRun

# approved_input: 담당자 승인 이후 만들어진 EvaluationInput
# run_data: 백엔드 POST /internal/runs 성공 응답의 data
# business_client: 같은 run_token으로 인증된 MCP SDK 클라이언트
context = EvaluationRun(
    run_id=run_data["run_id"],
    work_id=approved_input.work_record.work_id,
    snapshot=run_data["snapshot"],
)

async def call_business_tool(name, arguments):
    return await business_client.call_tool(name, arguments)

payload = await prepare_mcp_evaluation_payload(
    approved_input,
    context=context,
    call_tool=call_business_tool,
)
```

이 예시는 A의 비동기 함수 안에서 실행하는 연결 코드입니다.
Agent 측에서 내부 API 키를 새로 다루거나 DB 서버를 실행하지 않습니다.
MCP SDK의 구조화 결과와 JSON 텍스트 결과를 모두 받을 수 있습니다.

호출하는 도구는 `get_work_case`, 업무가 반환한 허용 버전별 `get_artifact_version`,
평가 대상 버전의 `get_policy_check_result`입니다. 권한 인자와 저장 도구는 사용하지 않습니다.
고객 첨부 원문 페이지, 자료 접근 이력, 변경 근거 링크, 승인 요청, 기준 견적 및 RAG의
추가 검색은 별도 연결 대상입니다. 현재 수집 범위를 전체 평가 자료 수집 완료로 취급하지 않습니다.

## 출력과 검사

기존 `build_evaluation_payload(request)` 호출은 유지됩니다. 이 호출만으로는
MCP 근거가 수집되지 않습니다. MCP를 사용하는 실행에서는 위의 비동기 진입점을 사용하세요.

MCP 근거가 연결된 payload에는 기존 업무·기준 외에 다음이 추가됩니다.

- `snapshot`: 조회 범위와 버전
- `evidence`: 등록 내용·고객 원문·허용 버전별 산출물·정책 검사 결과
- `work_evidence_sources`: 업무 기록과 추가 자료의 근거 ID → 원문 매핑
- `evidence_sha256`: 이번에 수집한 근거 내용의 해시
- `missing_sources`: A의 자료 부족 표시와 미수집 정책 검사 표시

기존 승인은 `WorkContext`와 `CriteriaDraft`의 해시를 검사합니다.
추가 MCP 근거는 같은 실행·스냅샷인지 검사하고 별도 해시를 남깁니다.
이 추가 해시는 담당자가 그 근거를 별도로 검토·승인했다는 뜻이 아닙니다.

다른 실행·스냅샷·업무·산출물 버전, 스냅샷 이후 자료, 중복·누락된 산출물,
등록 내용·고객 원문·산출물의 잘못된 근거 ID를 거부합니다.
원문의 공백, 빈 등록 내용, 원래 버전 문자열과 구간 ID는 보존합니다.
업무의 `work_type` 정답 필드는 B 근거에 복사하지 않습니다.

`None`인 등록 내용은 조회 결과에 등록 기록이 없음을 뜻합니다. `""`나 `{}`는
등록 기록이 비어 있는 상태입니다. 필드 자체가 누락되면 잘못된 응답으로 처리합니다.
정책 결과를 받지 않은 수동 조합은 `missing_sources=["policy_check"]`로 표시합니다.
위반 목록이 빈 것과 검사를 받지 못한 것은 다릅니다. 위반 없음만으로 2점을 확정하지 않습니다.

MCP 오류는 `MCPToolError.code`, `.retryable`, `.meta`로 전달합니다.
이 단위에서는 자동 재시도하거나 실패를 정상 빈 자료로 바꾸지 않습니다.

## 확인 명령

```bash
python -m pytest -q tests/test_mcp_adapter.py
python -m pytest -q
```

테스트는 저장소의 raw 40건을 서버 응답 형식으로 투영한 자료와 도구 호출 대역을 사용합니다.
raw 자료 테스트는 변환 호환성을 확인하며 마스킹 서버 동작을 재검증하지 않습니다.
실제 DB/MCP 서버와 OpenAI API를 호출하지 않고, 승인도 테스트 안에서만 대체합니다.
