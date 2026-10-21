# B 평가 Agent: 모델과 MCP 입력 연결

현재 구현 범위는 **승인된 평가 입력 → business/RAG MCP 조회 → GPT 평가 → 원문 검증 → 코칭·리포트**입니다.
현재 범위에서는 DB에 저장하지 않고 결과를 반환하거나 화면에 출력합니다.
평가기준 생성 코드는 기존 구현을 그대로 사용합니다.

## 파일 역할

| 파일 | 역할 |
| --- | --- |
| `src/work_classifier/workflow_models.py` | A/B 공통 입력, 기준, 승인, 결과 계약 |
| `src/work_classifier/evidence_models.py` | 등록 내용, 고객 원문, 산출물 구간, 정책 검사, 실행 범위 |
| `src/work_classifier/evaluation_models.py` | B 결과의 점수 범위, 보안 점수, 5개 항목 및 중복 제한 |
| `src/work_classifier/mcp_adapter.py` | SDK 결과 읽기, 응답 변환, 실행·스냅샷·버전 검사, 읽기 도구 연결 |
| `src/work_classifier/evaluator.py` | 승인된 입력과 MCP 근거를 평가용 딕셔너리로 연결 |
| `src/work_classifier/evaluation_materials.py` | 추가 대화·원문·열람·변경·승인 요청·기준 견적·RAG 출처 조회 |
| `src/work_classifier/evaluation_agent.py` | `gpt-4o-mini` 호출, 검증 재시도, 화면용 리포트 |
| `src/work_classifier/evaluation_validation.py` | 승인 항목·점수·인용문·버전·직원 행동 근거 대조 |
| `src/work_classifier/evaluate_cli.py` | 승인된 JSON 입력으로 실행하는 독립 명령 |

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

이 예시는 A의 비동기 함수 안에서 입력 준비만 실행하는 연결 코드입니다.
Agent 측에서 내부 API 키를 새로 다루거나 DB 서버를 실행하지 않습니다.
MCP SDK의 구조화 결과와 JSON 텍스트 결과를 모두 받을 수 있습니다.

호출하는 도구는 `get_work_case`, 업무가 반환한 허용 버전별 `get_artifact_version`,
평가 대상 버전의 `get_policy_check_result`입니다. 권한 인자와 저장 도구는 사용하지 않습니다.
실제 평가는 아래의 `OpenAIEvaluator`를 사용하세요. 위 입력 준비 함수는 기존 계약을 유지합니다.

## GPT 평가와 코칭 실행

```python
from openai import AsyncOpenAI
from work_classifier.evaluation_agent import OpenAIEvaluator, render_evaluation_report

async def call_rag_tool(name, arguments):
    return await rag_client.call_tool(name, arguments)

async with AsyncOpenAI(timeout=60.0, max_retries=1) as llm:
    evaluator = OpenAIEvaluator(
        llm,
        context=context,
        business_call=call_business_tool,
        rag_call=call_rag_tool,
    )
    result = await evaluator.evaluate(approved_input)
    print(render_evaluation_report(result))
    # 프론트엔드에는 result.model_dump(mode="json")을 전달합니다.
```

기존 `LocalCriteriaPartner(..., evaluator=evaluator)`의 `evaluate(request)`에도 연결됩니다.
모델은 기본 `gpt-4o-mini`, temperature=0입니다. 평가·코칭을 한 번에 구조화해서 받고,
검증 실패에 한해 기본 한 번 더 요청합니다. API에는 `store=False`를 설정합니다.
SDK의 구조화 출력 방식은 [OpenAI 공식 문서](https://developers.openai.com/api/docs/guides/structured-outputs)를 참고했습니다.

매 실행마다 다음을 조회합니다.

- 스냅샷 범위의 전체 대화: 페이지 누락·중복·범위 이탈 거부
- 자료 열람·수정 근거 링크·승인 요청: 스냅샷 이후 기록 제외
- 허용된 산출물 버전 간 변경 비교
- 고객 첨부 원문: 전체 페이지와 줄 ID 확인
- 견적 업무에 한해 서버 기준 견적: LLM의 임의 검산으로 대체하지 않음
- 승인된 기준의 출처: RAG `get_reference`로 문서·버전·구간·원문 정확 대조
- 스냅샷의 참조·연결 문서: 정확 조회하며 폐기·초안 자료는 사실 근거에서 제외

검색 유사도에 의존하지 않고 허용 자료를 정확 조회합니다. 평가할 때 기준을 새로 만들거나
기존 승인 내용을 변경하지 않습니다. 기준 생성과 사람의 확인·승인은 기존 A 흐름에서 매 실행 수행합니다.
승인된 `CriteriaDraft.references`의 본문은 동일한 RAG 구간의 원문과 일치해야 합니다.
별도의 PDF 전처리로 문장·줄바꿈이 바뀌었다면 생성 단계에서 RAG 원문과 정합성을 먼저 맞춰야 합니다.
추가 업무의 원본 산출물을 따라가는 조회나 코칭 사례 검색은 현재 실행의 필수 경로에 포함하지 않았습니다.

직원 행동 점수에 고객 원문·AI 답변·산출물만 인용하는 결과, 다른 항목의 기준,
없는 근거 ID·인용문·문서 버전, 보안 검사 없는 보안 점수, 기준 견적 없는 견적 만점은 거부합니다.
코칭은 다섯 항목마다 하나씩 필요합니다. 요약의 관측 점수·분모는 코드로 계산하며,
미관측을 0점으로 합산하거나 시간 절감·ROI·전후 개선율을 만들지 않습니다.
근거가 판단을 의미적으로 충분히 뒷받침하는지는 실제 업무 사례와 담당자 검토가 추가로 필요합니다.

## 터미널에서 실행

환경에 `OPENAI_API_KEY`, 백엔드가 발급한 `RUN_TOKEN`을 설정합니다.
키·토큰은 저장소에 커밋하지 않습니다. `--run-context`는 토큰 발급 응답 전체 또는
그 `data` 객체 또는 `run_id/work_id/snapshot`만 있는 JSON을 받을 수 있습니다.
실행 토큰, 승인 입력, 실행 정보는 같은 평가 실행의 값이어야 합니다.

```bash
python -m work_classifier.evaluate_cli \
  --input /path/to/approved_input.json \
  --run-context /path/to/run_context.json

# 화면 대신 프론트엔드용 JSON을 표준 출력으로 확인
python -m work_classifier.evaluate_cli \
  --input /path/to/approved_input.json \
  --run-context /path/to/run_context.json --json
```

기본 MCP 주소는 `http://localhost:8001/mcp`, `http://localhost:8002/mcp`이며
`--business-url`, `--rag-url`로 바꿀 수 있습니다. CLI는 승인을 자동 생성하지 않습니다.

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
저수준 변환 함수는 실패를 숨기지 않습니다. `OpenAIEvaluator`는 읽기 도구의
`TEMPORARY_FAILURE`이면서 `retryable=True`인 경우 한 번 재시도합니다.
권한·버전·형식 오류는 재시도하지 않습니다. 선택적 참조 문서의 `NOT_FOUND`는 자료 부족으로 표시합니다.

## 확인 명령

```bash
python -m pytest -q tests/test_mcp_adapter.py
python -m pytest -q tests/test_evaluation_agent.py
python -m pytest -q
```

테스트는 저장소의 raw 40건을 서버 응답 형식으로 투영한 자료와 도구 호출 대역을 사용합니다.
raw 자료 테스트는 변환 호환성을 확인하며 마스킹 서버 동작을 재검증하지 않습니다.
실제 DB/MCP 서버와 OpenAI API를 호출하지 않고, 승인도 테스트 안에서만 대체합니다.
영업 4종 수집·평가 경계·코칭 출력, 실제 OpenAI SDK와 가짜 HTTP 전송,
잘못된 인용·항목·해시·정책 원문, 응답 거절·미완료·API 오류를 확인합니다.
