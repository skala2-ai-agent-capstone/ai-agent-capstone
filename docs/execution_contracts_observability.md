# 실행 계약·추적·검증 기반

작업 기준: 폴더 개편을 반영한 `origin/main`의 `8f12065`.
개발 브랜치: `codex/evaluation-contracts-observability`.

이번 단계는 서비스 요청 계약과 관측·검증 기반을 추가한다. 기존 평가 기준 생성,
점수 계산, 프롬프트, 기준 승인 규칙은 변경하지 않는다.
중간 피드백 그래프, HTTP 요청 수신, MCP 저장 도구, DB 마이그레이션은 다음 단계다.

## 1. 공통 실행 계약 v1

구현: `src/work_classifier/shared/execution_contracts.py`.
기존 `EvaluationInput`, `EvaluationReceipt`, `AgentEvaluationOutput`을 대체하지 않는
추가 계약이다. 새 요청을 기존 평가기에 연결하는 어댑터는 아직 구현하지 않았다.

| 모델 | 역할 |
| --- | --- |
| `EvaluationRequest` | `request_id`, `purpose`, `work_type`, `snapshot`, `idempotency_key` |
| `ExecutionSnapshot` | 업무 ID, 대화 순번 범위, 산출물 버전, 문서 버전들, 승인된 기준 참조, 전체 내용 해시 |
| `ApprovedRubricRef` | 기준 ID·버전·업무 유형·내용 해시·승인 ID·승인 시각 |
| `FeedbackResult` | 요청·스냅샷·실행 ID, 상태, 최대 3개 피드백, 부족한 자료 |
| `FeedbackItem` | 기준 항목 ID, 대상·근거 ID, 진단, 다음 행동, 선택적 추천 프롬프트 |

- `purpose=feedback`: 요청 시점까지의 대화와 현재 초안. 초안 없이도 요청 가능.
- `purpose=final`: 완료 시점까지의 대화와 최종 산출물 버전. 산출물 참조 필수.
- 두 모드 모두 범위를 고정한다. 이후 추가된 대화를 실행 도중 섞지 않는다.
- 빈 대화 범위는 `0..0`, 비어 있지 않으면 `1 <= from_seq <= to_seq`다.
- 문서 ID와 버전을 함께 전달한다. 같은 문서/버전 중복을 거부한다.
- 기준은 스냅샷 시각 이전에 승인되어야 하고 요청 업무 유형과 같아야 한다.
- 현재 제안한 경계 표준 유형은 `rfp_analysis`, `proposal`, `quote`, `inquiry_reply`다.
  기존 내부 명칭 `quotation`, `customer_response`를 쓰는 연동부에서는
  `canonical_work_type()`을 명시적으로 호출한다. 기존 내부 Enum은 바꾸지 않는다.
- 확정적인 강점·개선 지적에는 대상과 근거 ID가 필요하다.
  `FeedbackResult.validate_scope()`에 MCP에서 확인한 근거 ID와 기준 항목 ID를
  전달해 존재하지 않는 근거·다른 스냅샷의 결과를 거부한다.
- 모델은 불변이며 알 수 없는 필드는 거부한다. 토큰은 계약 본문에 넣지 않는다.

**서버 책임:** 클라이언트가 선언한 승인 ID나 해시만 믿어서는 안 된다.
인증된 서버가 승인 원장과 버전을 확인하고 내용을 고정해야 한다.
이 DTO 검증은 인증·권한 확인·승인 확인·원문 해시 재계산을 대신하지 않는다.
`idempotency_key`는 필드만 정의했다. 동일 키 중복 실행/저장 방지와 다른 내용 재사용
거부는 서버 저장 트랜잭션에서 구현해야 한다.

최종 평가 결과의 상세 구조는 기존 `AgentEvaluationOutput`과 `EvaluationReceipt`를
유지한다. 서비스 공통 응답 및 저장 envelope는 백엔드/MCP 연결 시 추가한다.

계약 확인:

```bash
.venv/bin/python -m work_classifier.validation.cli schemas
```

요청 예시는 `samples/validation/contracts-v1.json`의 `examples[].inputs`에 있다.
가상 ID와 해시를 쓰는 계약 테스트이며 실제 승인 데이터가 아니다.

## 2. 추적: 로컬 기본, LangSmith 선택

구현: `src/work_classifier/observability/`.

추적 트리는 요청 → 워크플로 → 기존 그래프 노드 / 평가기 → MCP 호출·재시도 /
LLM 호출 → 평가 결과 검증이다. 수동 승인 대기를 포함한 루트 시간은 전체 경과시간이며,
모델 지연시간은 `llm.responses.parse` 자식 실행을 기준으로 본다.

| 기록 | 내용 |
| --- | --- |
| 식별 | trace/span/parent ID, 정적 단계 이름, 허용된 버전·요청 메타데이터 |
| 입출력 | SHA-256과 바이트 수. 일반 텍스트 원문 제외 |
| 수행 | 시작·종료 시각, 경과시간, 예외 클래스 |
| LLM | SDK가 반환한 입력·출력·전체 토큰 수 |

알 수 없는 Python 객체는 `repr()` 대신 타입 정보만 사용한다.
해시는 실행 비교용 지문이며 원문을 복원하거나 평가 근거를 읽는 수단이 아니다.
예외 메시지와 traceback은 원문을 포함할 수 있어 추적에 저장하지 않는다.
추적 ID·버전 메타데이터에는 개인정보나 인증값을 사용하지 않는다.

기존 전체 평가 CLI에 적용하는 방법:

```bash
# 실제 MCP와 LLM을 호출하는 명령: 서버와 인증 설정 후 실행
.venv/bin/python -m work_classifier.evaluation_workflow_cli \
  --evaluation-id EVAL-ID --trace-dir outputs/traces

# .env의 LANGSMITH_API_KEY, LANGSMITH_PROJECT를 설정한 후 명시적으로 활성화
.venv/bin/python -m work_classifier.evaluation_workflow_cli \
  --evaluation-id EVAL-ID --trace-dir outputs/traces --langsmith
```

`--trace-dir`는 로컬 JSONL을 생성한다. `--langsmith`는 정제된 수동 RunTree만 추가로
전송한다. `LANGSMITH_TRACING=true`가 설정되어 있어도 이 CLI와 `TraceSession`
범위에서는 LangGraph의 전체 state 자동 추적을 끈다. 기존 다른 CLI는 변경하지 않았다.
LangSmith 업로드 실패는 경고하고 로컬 실행을 계속한다. 로컬 기록 실패는 실행 실패로
노출한다. SDK 비동기 전송의 최종 서버 수신까지 보장하는 영수증은 제공하지 않는다.

직접 새 Agent 실행을 연결할 때:

```python
from pathlib import Path
from work_classifier.observability import TraceSession, trace_step

@trace_step("load_snapshot", run_type="tool")
async def load_snapshot(request):
    return await mcp_read_snapshot(request)  # 이후 구현할 MCP 연동부

with TraceSession(Path("outputs/traces"), metadata={"purpose": "feedback"}) as trace:
    with trace.span("feedback_request") as span:
        result = await load_snapshot(request)
        span.result(result)
```

하나의 세션에는 하나의 루트를 두고 하위 단계를 중첩한다. 기존 OpenAI
`responses.parse` 호출은 `TracedLLMClient`가 감싼다. AWS 자체 호스팅 모델의 별도
클라이언트/스트리밍 API는 아직 연결하지 않았으며 해당 호출 경계에 같은 추적을 붙이면 된다.

SDK 구현 근거: [수동 추적](https://docs.langchain.com/langsmith/annotate-code),
[환경변수 없이 추적 제어](https://docs.langchain.com/langsmith/trace-without-env-vars),
[입출력 마스킹](https://docs.langchain.com/langsmith/mask-inputs-outputs).

## 3. 검증 실행과 전후 비교

구현: `src/work_classifier/validation/`.
다음 명령은 서버·모델·LangSmith 없이 가상 계약 사례 5개를 검증한다.
같은 디렉터리 덮어쓰기는 거부하므로 재실행할 때 새 경로를 쓴다.

```bash
.venv/bin/python -m work_classifier.validation.cli run \
  --dataset samples/validation/contracts-v1.json \
  --target work_classifier.validation.targets:validate_request \
  --output-dir outputs/validation/contract-a --label contract-a \
  --model none --prompt-version none --rubric-version fixture-v2 --data-version fixture-v1

.venv/bin/python -m work_classifier.validation.cli run \
  --dataset samples/validation/contracts-v1.json \
  --target work_classifier.validation.targets:validate_request \
  --output-dir outputs/validation/contract-b --label contract-b \
  --model none --prompt-version none --rubric-version fixture-v2 --data-version fixture-v1

.venv/bin/python -m work_classifier.validation.cli compare \
  outputs/validation/contract-a/experiment.json \
  outputs/validation/contract-b/experiment.json \
  --output outputs/validation/contract-comparison.json
```

두 번 실행하는 예시는 실행기 동작 확인이다. 코드 개선 효과나 AI 품질 향상을 의미하지 않는다.

| 출력 | 검증결과서에 사용할 내용 |
| --- | --- |
| `manifest.json` | 실행 시작 시 커밋·작업 트리 해시·lock 해시·Python/주요 패키지 버전, 설정·평가셋 해시 |
| `dataset.json` | 실행에 사용한 가상 입력·기대값·체크 조건의 고정 사본 |
| `results.jsonl` | 사례별 결과를 즉시 기록; 중간 실패 시 확인 가능 |
| `experiment.json` | 종료 시각을 포함한 전체 실험 결과와 provenance |
| `report.csv` | 검증 ID, 유형, 입력, 기대 결과, 근거, 실제 결과, 판정, 조치란, trace ID |
| `traces/` | 사례별 로컬 추적 |

현재 데이터셋은 `data_policy=synthetic`만 허용한다. 실제 내용이 가상 데이터인지는
작성자가 확인해야 한다. 로컬 검증 보고서에는 입력·실제 출력이 포함된다.
이는 원문을 제외하는 추적 JSONL과 다르다. 실제 고객 대화·비밀키를 평가셋에 넣지 않는다.

새 평가 함수를 연결할 때 `--target module:function`은 신뢰하는 로컬 함수만 지정한다.
함수는 JSON 입력 하나를 받고 JSON 결과(또는 Pydantic 모델)를 반환하며 async도 지원한다.
기대값은 함수에 전달하지 않는다. 입력 사본을 전달해 함수가 평가셋을 수정하지 못하게 한다.
체크 경로는 성공 시 `status=ok`, `output.<반환값>`, 실패 시
`status=error`, `error_type=<예외 클래스>`를 기준으로 정의한다.

`--model`, `--model-settings`, 프롬프트·기준·데이터 버전은 **실제 target의 설정을 기록하는
실험 선언값**이다. 실행기가 target의 모델 설정을 강제로 변경하지 않는다.
후속 실모델 어댑터가 실제 설정/해시와 이 선언값을 일치시켜야 한다.
검증 CLI의 `--langsmith` 사용 시 환경변수로 키를 전달한다. 이 CLI는 `.env`를 자동 로드하지 않는다.

- 기대 체크가 모두 통과하면 `passed`, 하나라도 실패하면 `failed`다.
- 체크가 없으면 `unreviewed`다. 미검토를 성공으로 세지 않는다.
- CLI 종료 코드는 체크 실패 1, 실행기 오류 2, 그 외 0이다. 따라서 종료 코드 0만으로
  검증 완료를 판단하지 말고 `unreviewed=0`인지도 확인한다.
- 수동 품질 평가의 점수 입력·확정 워크플로는 아직 없다. CSV의 조치란을 작성해도
  `experiment.json`의 판정이 자동으로 변경되지는 않는다.

비교는 동일 목적·평가셋 ID/버전/해시·데이터 버전·사례 ID를 요구한다.
기준 수정 효과를 비교할 때만 `--allow-change rubric_version`을 추가한다.
모델·생성 설정·프롬프트도 변경하려면 해당 `--allow-change`를 명시해야 한다.
서로 다른 평가셋은 허용하지 않는다. 비교 결과에는 개선/퇴행/검토 상태 변경 사례,
전후 커밋, 작업 트리 해시, 환경 변경, 미커밋 변경 경고가 포함된다.

**브랜치 머지 후에도 비교 가능:** 브랜치 이름은 이동하거나 삭제될 수 있으므로 실행별
커밋 SHA를 보관한다. 변경 전 실험과 변경 후 실험을 각각 해당 커밋에서 실행하고 결과
폴더를 보관한다. 커밋 밖의 코드가 있으면 해시는 기록되지만 코드를 복원할 수는 없다.
정식 비교는 커밋된 코드, 고정 데이터, 기준 본문/버전, 모델·생성 설정을 보관한 환경에서 한다.
외부 모델의 변경이나 비결정성 때문에 동일 결과의 완전한 재현까지 보장하지는 않는다.

## 4. 팀원과 다음 구현 순서

1. 기준 담당자는 기존 `src/work_classifier/rubric/`의 생성·수정·검증을 진행한다.
   승인된 기준 ID, 버전, 항목 ID, 업무 유형, 내용 해시, 승인 정보를 연결부에 제공한다.
   이 브랜치는 그 기준의 내용과 채점 로직을 수정하지 않는다.
2. 서버 담당자와 위 계약을 합의한다. 서버가 요청 시 스냅샷과 기준 버전을 확정하고,
   실행 권한 및 중복 방지를 담당한다. 기존 run token/승인 계약은 유지한다.
3. 사용자의 다음 구현: 신규 요청을 받는 실행 진입점과 `purpose` 분기,
   MCP 조회 결과 → 스냅샷 계약 어댑터, 중간 피드백 그래프와 근거 검증.
4. 피드백 결과 저장 MCP 도구와 프론트 오른쪽 패널 연결. 저장 완료 후 상태를 갱신한다.
   현재 MCP 클라이언트는 조회 도구만 허용하며 저장은 아직 지원하지 않는다.
5. 팀원이 확인한 기대 결과·근거로 실제 업무 평가셋을 만들고, 실모델 target을 연결해
   동일 사례의 전후 품질·지연시간·오류를 검증결과서에 기록한다.

현재 5개 사례는 계약 정상/오류 검증용이다. 서비스 STEP04 검증 ID와 매핑한 기능 사례,
검증결과서의 AI 품질 평가셋(최소 10개·권장 20개)은 별도로 마련해야 한다.
기존 40개 원시 입력을 기대 정답 확인 없이 품질 통과 사례로 계산하면 안 된다.

개발 검증 명령:

```bash
.venv/bin/python -m pytest -q
.venv/bin/ruff check src/work_classifier/observability src/work_classifier/validation \
  src/work_classifier/shared/execution_contracts.py \
  tests/test_execution_contracts.py tests/test_observability.py tests/test_validation_runner.py
```

## 이번 변경의 확인 결과

- 최신 main을 받은 직후: 기존 테스트 544개 통과.
- 변경 후: 전체 테스트 569개 통과, `ruff check src tests` 통과.
- 신규 검증에는 요청 범위·승인 시각·근거 ID 오류, 추적 원문 제외·동시 실행 부모 연결,
  업로드 실패 시 계속 실행, 기존 승인 대기→승인→평가의 추적, 데이터셋 변경 비교 거부가 포함된다.
- 로컬 `contract-a`, `contract-b`: 각 5/5 통과. 같은 코드의 반복 실행이므로 개선/퇴행 없음.
  파일은 `outputs/validation/`에 있으며 Git에서 제외된다. 커밋 전 실행으로 `dirty=true`다.
- 실제 MCP 네트워크, AWS 모델, LangSmith 서버 수신은 이번 확인 범위에 포함하지 않았다.
