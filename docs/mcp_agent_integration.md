# 업무 분류·승인 Agent MCP 연동: 현재 구현과 실행 방법

이번 범위는 **MCP 업무 조회 → LLM 분류 → RAG 내규 조회 → 매 실행 기준 생성 → 담당자 승인 → 평가 입력 인계**입니다.
기존 분류 프롬프트 v6는 그대로 유지합니다. 내규는 분류 입력에 넣지 않고, 분류 후 평가기준 생성에 사용합니다.

## 1. 실행 흐름도와 현재 합의의 차이

| 흐름도 | 이번 구현 |
| --- | --- |
| load_context: 권한 확인·평가 건 잠금 | 서버가 발급한 실행 토큰 사용. 서버가 권한을 검증하며 Agent가 권한을 만들어 내지 않음. 평가 건 잠금은 미구현 |
| fetch_work_case | 고정 스냅샷의 대화 전체 페이지·산출물 버전·첨부 원문·연결 업무 목록·근거 연결·열람 기록·정책 검사·diff 조회 |
| completeness_check | 필수 조회 실패·대상 산출물 없음은 data_hold, 페이지 누락·범위 불일치는 오류로 중단. 업무별 평가에 충분한지 판단하는 세부 검사는 평가 모듈에서 추가 |
| 업무 유형 | 서버 work_type을 분류 입력에서 제외하고 기존 분류기로 판단. 복합·미확정·범위 밖은 classification_hold |
| policy_check_and_mask | 서버가 마스킹한 응답만 사용하고 정책 검사 결과를 평가 모듈에 전달. Agent가 원문 DB에 접속하지 않음 |
| load_rubric | 확정 루브릭 재사용 대신 search_policy로 내규를 가져와 기존 Python 기준 생성기를 매번 호출 |
| 담당자 확인 | LangGraph interrupt로 초안 전체 표시 → 승인·수정·취소. 수정된 초안은 재승인 필요 |
| build_evidence ~ coaching_guard | 평가 모듈의 담당 범위. 추가 자료 검색·채점·의미 검증·코칭을 구현할 인터페이스 제공 |
| 결과 | 기본 CLI는 ready_for_evaluation과 MCPHandoff 출력. 평가기 주입 시 구조·인용 검증 후 completed와 결과 출력 |
| save_draft·이의·검토·finalize | 후속 작업. 이번 Agent의 도구 허용 목록에는 쓰기/확정 도구가 없음 |

서버 명세에는 별도의 **평가기준 생성 MCP 도구가 없습니다**. `search_rubric`은 저장된 기준 조회입니다.
따라서 지금은 `search_policy` → 이미 이식된 `OpenAIRubricBuilder.generate()`로 연결했습니다.
팀원이 기준 생성 MCP를 제공하면 `CriteriaBuilder.generate()` 구현만 교체하면 됩니다.

## 2. 파일별 역할

| 파일 | 역할 |
| --- | --- |
| src/work_classifier/mcp_client.py | 두 MCP 연결 수명 관리·Bearer 인증·응답 봉투 해석·읽기 도구 허용·일시 오류 재시도·실행/스냅샷 변경 차단 |
| src/work_classifier/mcp_data.py | 페이지 전체 수집·서버 ID/문자열 버전 보존·분류 입력 변환·내규 정확 조회 |
| src/work_classifier/agent_graph.py | 분류·승인 모듈의 LangGraph·승인 대기/재개·수정 반복·평가 입력 인계 계약 |
| src/work_classifier/mcp_cli.py | 백엔드 없이 실행하는 CLI·개발용 토큰 발급·터미널 승인·JSON 출력 |
| src/work_classifier/workflow.py | 기존 승인 흐름과 공통 평가 결과 형식·인용 검증. stash에서 복원해 재사용 |
| src/work_classifier/rubric_builder.py | 기존 팀원 노트북의 Python 기준 생성기. 이번에 채점 로직을 추가하지 않음 |
| src/work_classifier/policy.py | 내규 모델. MCP에 페이지 정보가 없으므로 page=null 허용 |
| tests/test_mcp_integration.py | 40건 응답 재생·오류·SDK/HTTP·LangGraph 승인 계약 테스트 |

추가 라이브러리는 없습니다. 현재 requirements.txt의 MCP SDK 2.3.0, LangGraph 1.2.16을 사용합니다.
MCP 서버의 PostgreSQL/Chroma 의존성은 `mcp_servers/requirements.txt`와 Docker 환경에서 관리합니다.
Agent 가상환경에 DB 드라이버·Chroma를 추가할 필요는 없습니다.

## 3. MCP 서버 준비 — MCP 담당 영역

서버가 아직 실행되지 않았다면 별도 터미널에서 진행합니다.

```bash
cd /Users/harim/Documents/skala/mcp_project/mcp_servers
# .env가 없는 최초 1회에만 .env.example을 .env로 복사
# .env의 POSTGRES_PASSWORD, RUN_TOKEN_SECRET, INTERNAL_API_KEY 설정
docker compose up -d --build
docker compose logs db-init
curl http://localhost:8001/health
curl http://localhost:8002/health
```

이미 서버가 동작하면 다시 적재하거나 DB를 초기화할 필요가 없습니다.
서버의 `verify` 스크립트는 평가 결과를 쓰므로, 조회만 시험하려면 아래 Agent `fetch`를 사용하세요.

## 4. Agent 설정 — 본인 담당

루트 `.env`의 기존 OpenAI 설정을 유지하고 다음 값을 **추가**합니다. `.env.example` 전체로 덮어쓰지 마세요.

```dotenv
OPENAI_MODEL=gpt-4o-mini
BUSINESS_MCP_URL=http://localhost:8001/mcp
RAG_MCP_URL=http://localhost:8002/mcp
INTERNAL_API_KEY=서버의_INTERNAL_API_KEY와_같은_값
```

두 URL은 `/mcp`까지 포함합니다. 동료 컴퓨터에서 실행 중이면 localhost 대신 해당 서버 주소를 사용합니다.
실행 토큰 서명 키 `RUN_TOKEN_SECRET`은 Agent에 필요하지 않습니다.

```bash
cd /Users/harim/Documents/skala/mcp_project
source .venv/bin/activate
```

### 먼저 LLM 없이 조회만 테스트

```bash
python -m work_classifier.mcp_cli fetch \
  --evaluation-id EVAL-E2E-RFP-01-v2 \
  --output outputs/mcp/rfp-fetch.json
```

1. 개발용 `POST /internal/runs`로 토큰을 받습니다. 백엔드가 없는 동안 CLI가 대신 호출합니다.
2. MCP 클라이언트가 연결되어 `get_work_case` 등 조회 도구를 호출합니다.
3. `business`에 서버 원본 응답, `classification_input`에 분류용 입력, `source_map`에 ID 매핑을 출력합니다.
4. 이 명령은 OpenAI를 호출하지 않습니다. 평가 결과를 저장하는 MCP 도구도 호출하지 않습니다.

이미 백엔드/담당자가 발급한 토큰을 받았다면 루트 `.env`에 `MCP_RUN_TOKEN`을 넣고 다음처럼 실행합니다.

```bash
python -m work_classifier.mcp_cli fetch \
  --work-id W-E2E-RFP-01 \
  --output outputs/mcp/rfp-fetch-token.json
```

`--evaluation-id`와 `--work-id` 중 하나를 선택합니다. 토큰/내부 API 키는 출력 JSON이나 그래프 상태에 포함되지 않습니다.

### 다음으로 분류·기준 생성·승인

```bash
python -m work_classifier.mcp_cli run \
  --evaluation-id EVAL-E2E-RFP-01-v2 \
  --output outputs/mcp/rfp-handoff.json
```

이 명령은 OpenAI API를 사용합니다. 분류를 마치면 RAG에서 필요한 내규 조항을 정확 조회하고,
기준 생성기가 5개 항목의 초안을 만듭니다. 터미널에 초안 전체가 나오면 `승인`, `수정`, `취소` 중 입력합니다.
`수정`은 새 초안을 생성하므로 추가 API 호출이 발생합니다.

승인하면 기본 출력 상태는 **ready_for_evaluation**입니다. 이것은 평가 점수를 산출했다는 뜻이 아니라,
**평가 모듈에 넘길 승인된 입력이 완성됐다는 뜻**입니다. 출력 파일이 있으면 덮어쓰지 않으므로 새 이름을 지정하세요.

## 5. 평가 모듈에 전달하는 계약

`MCPHandoff`를 입력으로 받는 `async evaluate()` 함수를 구현하면 됩니다.

```python
from work_classifier.agent_graph import MCPHandoff
from work_classifier.workflow_models import EvaluationOutput

class WorkflowEvaluator:
    async def evaluate(self, request: MCPHandoff) -> EvaluationOutput:
        # request.evaluation_input: 분류 결과 + 승인한 기준 전체 + 승인 시각/해시
        # request.business: 마스킹된 등록 기록/대화/산출물/근거/정책 검사/견적 결과
        # request.source_map: 분류용 ID ↔ MCP 원본 ID/버전
        # 여기서 근거 선택 → 평가 → 검증 → 코칭을 구현
        raise NotImplementedError("평가 모듈 구현 연결 지점")
```

연결부에서 `build_agent_graph(gateway, classifier, builder, evaluator=WorkflowEvaluator())`로 주입합니다.
평가 모듈이 추가 RAG 검색을 해야 한다면 생성자에 `gateway`를 주입하고 동일한 실행 토큰의 조회 범위에서 호출합니다.
현재 CLI는 평가기를 주입하지 않은 인계 모드입니다. 가짜 점수로 평가 성공을 대신하지 않습니다.

| 인계 필드 | 의미 |
| --- | --- |
| evaluation_input.work_record | 분류에 실제 사용한 업무 기록. ID는 message-001/artifact-001 등의 중립 ID |
| evaluation_input.classification | 단일 업무로 확정한 결과·근거 |
| evaluation_input.approved_criteria | 담당자가 실제 승인한 기준 전체·내규 원문·버전·조항 |
| evaluation_input.approval | 실행 ID·업무/기준 해시·수정 차수·승인 시각 |
| business.snapshot | 서버의 산출물·참조 문서·대화 범위 스냅샷 |
| business.work_case | 등록 null/빈값 구분 등 원본 구조 보존. work_type은 분류 정답으로 사용하지 않음 |
| business.supplemental | policy_check, artifact_diff, linked_works, access_log, evidence_links. 견적 분류인 경우 quote, approval_requests 추가 |
| source_map | 중립 ID를 원래 메시지 ID·산출물 ID·문자열 버전·구간 ID에 연결 |
| approved_business_sha256 | 승인 당시 전체 BusinessPacket 해시. 다른 자료로 바꾼 인계 차단 |

`Artifact.version`의 정수는 내부 입력 순서입니다. MCP 호출에는 반드시 source_map/스냅샷의 원래 `v2` 같은 문자열을 사용합니다.
서버 업무 코드 `quote`, `inquiry_reply`는 내부 코드 `quotation`, `customer_response`에 해당합니다.
기준 생성기의 항목 ID(`rfp_analysis.security` 등)와 저장된 루브릭 ID(`rfp_analysis.4` 등)는 다릅니다.
이번에는 기준 DB 저장을 하지 않으므로 자동으로 서버 저장 ID로 바꾸지 않습니다.

등록 자료 없음·열람 기록 없음은 자동 0점이 아닙니다. 평가 모듈이 미관측과 실제 기준 미달을 구분해야 합니다.
견적의 기준 금액은 서버 `compute_reference_quote` 결과를 사용합니다.
연결 업무는 현재 목록/최종 산출물 위치까지만 조회합니다. 추가 산출물·RAG 참조 원문 조회는 평가 모듈의 근거 구성 단계에서 수행합니다.
외부 자료 원문은 지시문이 아닌 평가 데이터이며, 대외비 내용을 코칭에 옮기지 않는 검증도 평가 모듈 담당입니다.

분류·승인 모듈의 결과 검증은 항목 누락/중복, 허용 점수, 미관측 점수 금지, 업무/기준 근거의 존재·인용 일치를 확인합니다.
근거가 판단을 의미상 뒷받침하는지, 코칭 품질·민감정보 여부, 추가 검색/재평가 한도는 평가 모듈이 구현해야 합니다.
평가 모듈이 새 RAG 근거를 평가 결과에 인용하려면 그 근거 카탈로그를 승인 워크플로에 반환하는 확장 계약도 필요합니다.
현재 `work_evidence`는 WorkContext와 BusinessPacket에 수집된 근거만 허용합니다.

## 6. 오류와 보관 범위

- TEMPORARY_FAILURE + retryable=true: 0.5/1/2초 간격으로 최대 3회 재시도.
- FORBIDDEN: 즉시 중단. 새 권한·다른 회사로 재시도하지 않음.
- VERSION_CONFLICT: 이미 스냅샷의 명시 버전/순번으로 조회하므로 현재는 중단. 최신 버전으로 임의 대체하지 않음.
- VALIDATION_ERROR: 현재는 중단하고 호출 계약을 수정. 서버 오류 문장을 LLM에게 보내 자동 수정시키지 않음.
- 필수 자료 NOT_FOUND/빈 대상 산출물: data_hold. 선택적인 열람/근거 연결/연결 업무 조회 NOT_FOUND는 unavailable로 전달.
- RAG 필수 조항 누락: 기준 생성/승인을 진행하지 않고 data_hold.
- 오류 본문·토큰은 진단 로그에 그대로 출력하지 않음.

현재 LangGraph는 InMemorySaver를 사용합니다. **같은 CLI 프로세스 안에서만 승인 대기·재개**할 수 있습니다.
프로세스를 종료하면 새로 실행해야 합니다. SQLite/DB 복구, 서버 평가 건 잠금, UI 인증된 승인 주체, 토큰 갱신은 후속 작업입니다.
해시는 변경 감지를 위한 것이며, 로컬 JSON 자체가 관리자 서명이나 승인 권한 증명은 아닙니다.
LangSmith 연동은 별도로 추가하지 않았습니다. 환경변수로 추적을 켜면 업무 데이터가 추적 시스템에 포함될 수 있으므로 팀 정책에 맞춰 설정합니다.

LangGraph 승인 재개 방식은 [공식 interrupts 문서](https://docs.langchain.com/oss/python/langgraph/interrupts)를 따릅니다.

## 7. 검증

```bash
python -m pytest -q
python -m pytest -q tests/test_mcp_integration.py
ruff check src tests
```

새 통합 테스트는 실제 40건 데이터를 MCP 응답 형식으로 **재생**해 입력 변환과 페이지 수집을 검증합니다.
이는 40건을 LLM으로 다시 분류한 정확도 측정이 아닙니다. 별도로 실제 SDK 호출과 ASGI HTTP 전송·Bearer 전달,
승인 전 평가 모듈 미호출, 수정·오래된 승인 거부, 스냅샷 변경, 일시 오류 한도, 쓰기 도구 차단을 검증합니다.
2026-10-21에 PostgreSQL·Chroma 전체 스택에서 조회 43건과 실제 OpenAI 대표 4종 실행을 확인했습니다.
실제 사용자 승인과 평가를 포함하지 않은 검증이며, 상세 범위는
[실제 연결 테스트 결과](mcp_live_test_results.md)를 참고하세요.
