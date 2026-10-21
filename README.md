# Sprint 1: 더미 JSON 기반 업무 분류

MCP·DB 없이 업무 기록을 읽고 **RFP 분석 / 제안서 / 견적서 / 고객 질의 답변**을
분류하는 Python 모듈입니다. 업무 유형은 입력에 없으며 LLM이 판단합니다.
현재 목표는 **내규 없이 업무 기록으로 유형을 분류**하는 것입니다.
내규 PDF는 이후 유형별 평가 기준을 구성하고 평가할 때 사용하는 자료입니다.
현재 분류 프롬프트는 `work-classification-v6`입니다.
아래 설명은 업무 분류 CLI를 기준으로 합니다. B 담당의 승인 후 MCP 조회·GPT 평가·
근거 검증·코칭 기능과 별도 실행 명령은 [B 평가 Agent 안내](docs/evaluation_b.md)를 참고하세요.
평가 결과는 화면 또는 호출자에게 반환하며 DB에 저장하지 않습니다.

```text
샘플 JSON → WorkContext 입력 검증 → LLM 분류 → 출력·근거 검증 → JSON 리포트
```

## 1. 설치

Python 3.11 이상이 필요합니다. 현재 프로젝트의 `.venv`는 Python 3.11로 구성되어 있습니다.
`requirements.txt`에 실행·테스트·코드 검사 의존성을 고정했습니다.

```bash
cd /Users/harim/Documents/skala/mcp_project

# 현재 컴퓨터에서는 이미 있으므로 재생성할 필요가 없습니다.
# 새 컴퓨터에서 처음 설정할 때만:
python3.11 -m venv .venv

source .venv/bin/activate

# uv로 생성한 환경 등 pip가 없는 경우에도 설치할 수 있도록 준비:
python -m ensurepip --upgrade
python -m pip install -r requirements.txt
```

`requirements.txt`의 `-e .`는 현재 프로젝트를 편집 가능한 패키지로 설치합니다.
따라서 `src/work_classifier/` 코드를 수정하면 재설치 없이 반영되고
`work-classifier` 명령을 사용할 수 있습니다.
Windows PowerShell의 활성화 명령은 `.venv\\Scripts\\Activate.ps1`입니다.

매일 새 터미널을 열면 아래 두 단계만 반복합니다. 패키지 설치는 처음 또는 의존성 변경 시 수행합니다.

```bash
cd /Users/harim/Documents/skala/mcp_project
source .venv/bin/activate

# 실제 선택된 Python 확인
python -c "import sys; print(sys.executable)"

# 작업 종료 시
deactivate
```

가상환경을 활성화한 경우 아래 예시의 `uv run`은 생략해도 됩니다.
예: `work-classifier validate samples/*.json`, `python -m work_classifier classify ...`.
VS Code에서도 이 프로젝트의 `.venv/bin/python`을 인터프리터로 선택하세요.

기존 [uv](https://docs.astral.sh/uv/) 흐름을 선호하면 다음을 사용해도 됩니다.
uv는 `uv run` 시 프로젝트 가상환경을 사용하므로 수동 활성화가 필수는 아닙니다.

```bash
uv sync --locked
uv run work-classifier validate samples/*.json
```

의존성의 원본 정의는 `pyproject.toml`, 해석된 버전은 `uv.lock`입니다.
`requirements.txt`는 팀의 pip 설치를 위해 그 잠금 결과를 내보낸 파일입니다.
새 라이브러리를 추가할 때는 원본과 잠금을 갱신한 후 다시 내보내세요.

```bash
# 예: uv add <패키지명> 또는 uv add --dev <개발용 패키지명>
uv export --locked --format requirements-txt --no-hashes --no-annotate \
  --output-file requirements.txt
```

`.venv`는 설치된 Python과 패키지 폴더이고, `.env`는 API 키·모델을 담은 설정 파일입니다.
서로 다른 역할이며 두 항목 모두 Git에서 제외되어 있습니다.

### 현재 및 다음 단계에 사용하는 라이브러리

| 패키지 | 역할 | 사용하는 시점 |
| --- | --- | --- |
| `openai` | OpenAI LLM 호출과 구조화된 출력 | 현재 분류 함수 |
| `pydantic` | 입력·출력 및 MCP 응답 데이터 검증 | 현재부터 계속 |
| `python-dotenv` | `.env` 설정 읽기 | 현재부터 계속 |
| `langgraph` | 조회 → 분류 → 루브릭 조회 → 평가 → 검증 → 저장의 노드·분기·상태 관리 | 다음 워크플로 구현 |
| `mcp` | Business/RAG MCP 서버 연결, 도구 목록 조회 및 호출 | MCP 연결 구현 |
| `langgraph-checkpoint-sqlite` | 그래프 실행 상태를 로컬 SQLite 파일에 저장·재개 | 중단·재개 및 사람 검토 실험 |
| `pytest` / `pytest-asyncio` | 일반 테스트 및 비동기 MCP·그래프 테스트 | 개발·검증 |
| `httpx` | 기존 OpenAI 테스트의 가짜 HTTP 응답 구성 | 개발·검증 |
| `ruff` | 코드 오류·스타일 검사 | 개발·검증 |

현재 고정된 주요 버전은 LangGraph `1.2.16`, MCP SDK `2.3.0`,
SQLite 체크포인트 `3.1.2`, pytest-asyncio `1.4.0`입니다.
전체 버전은 `requirements.txt`와 `uv.lock`을 기준으로 합니다.
`httpx`는 기존 간접 의존성이었으며 테스트에서 직접 사용하므로 개발 의존성에 명시했습니다.

LangGraph 노드 안에서 기존 `classify_work()`와 OpenAI SDK를 호출할 수 있으므로
`langchain`·`langchain-openai`를 별도로 추가할 필요는 없습니다.
`langchain-core` 등 하위 패키지는 LangGraph의 의존성으로 자동 설치됩니다.
MCP 도구를 LangChain 도구 객체로 변환하는 방식이 필요해지면
`langchain-mcp-adapters`의 SDK 호환성을 확인한 후 추가할 수 있습니다.
참고: [LangGraph 설치 안내](https://docs.langchain.com/oss/python/langgraph/install).

MCP 연결은 설치한 SDK **v2**의 API를 기준으로 구현합니다.
서버 담당자에게 서버 주소·전송 방식·인증 방식·도구 입력/출력 스키마를 받아야
실제 연결을 구현할 수 있습니다. 패키지 설치만으로 서버에 연결되지는 않습니다.
참고: [MCP Python SDK](https://py.sdk.modelcontextprotocol.io/).

SQLite 체크포인트는 Agent의 실행 상태를 저장하는 로컬 개발용 저장소입니다.
팀의 업무 DB나 RAG Vector DB와는 역할이 다르며, 현재 SQLite 파일을 생성하거나
업무 테이블을 구현하지는 않습니다.
참고: [LangGraph 상태 저장](https://docs.langchain.com/oss/python/langgraph/persistence).
RAG용 Chroma·pgvector, 업무 DB용 SQLAlchemy·psycopg, 별도 API용 FastAPI는
각 담당 영역의 구현 방식이 정해질 때 추가합니다. 이 Agent는 우선 MCP 도구로 데이터를 받습니다.

## 2. API 키 없이 먼저 확인하기

```bash
# 12개 샘플 입력의 Schema 검사. 분류를 수행하는 명령은 아닙니다.
uv run work-classifier validate samples/*.json

# 입력 / LLM 출력 / 저장 리포트의 JSON Schema
uv run work-classifier schemas

# 외부 API를 호출하지 않는 테스트
uv run pytest -q
```

테스트는 모델 대신 가짜 응답 또는 가짜 HTTP 전송을 사용합니다.
테스트 통과는 데이터 계약·제어 흐름의 검증이며 **실제 LLM 분류 정확도 검증이 아닙니다.**

## 3. 실제 LLM으로 한 건 분류하기

```bash
# .env가 이미 있으면 기존 설정을 유지합니다.
cp -n .env.example .env
```

`.env`를 편집합니다. 실제 API 키를 메시지·Git에 공유하지 마세요.

```dotenv
OPENAI_API_KEY=본인의_API_키
OPENAI_MODEL=gpt-4o-mini
```

모델 ID는 `gpt-4o-mini`입니다. `gpt-4.o-mini`가 아닙니다.
사용자가 선택한 모델을 기본값으로 사용합니다. 계정에서 사용할 수 있는 모델이어야 하며,
Responses API와 Structured Outputs를 지원해야 합니다.
모델 접근 권한·호출 비용은 사용 중인 API 계정을 따릅니다.
기존 환경변수가 있으면 `.env`보다 우선하고, `--model`은 `OPENAI_MODEL`보다 우선합니다.
현재 작업 폴더의 `.env`만 읽습니다.

```bash
uv run work-classifier classify samples/03_quotation.json

# 결과를 파일에도 저장: 기존 파일은 덮어쓰지 않습니다.
uv run work-classifier classify samples/03_quotation.json \
  --output outputs/quotation-run-01.json

# 프로젝트 패키지를 직접 실행해도 같습니다.
uv run python -m work_classifier classify samples/08_needs_review.json
```

이 명령은 업무 자료를 OpenAI API로 보내며 유료 호출이 발생할 수 있습니다.
제공된 샘플은 모두 가상 자료입니다. `store=False`를 설정하지만 이 설정이
모든 제공자 보관 정책을 대체하거나 무보관을 보장하지는 않습니다.

## 4. 출력과 상태

다음은 형식 설명용 예시이며 실제 LLM 실행 결과가 아닙니다.

```json
{
  "work_id": "sample-003",
  "input_sha256": "입력의 SHA-256",
  "model": "gpt-4o-mini",
  "prompt_version": "work-classification-v1",
  "response_id": "API 응답 ID",
  "classification": {
    "status": "classified",
    "work_type": "quotation",
    "reason": "상품 수량과 기간에 따른 금액 산출이 목적이다.",
    "evidence": [
      {"source_id": "m1", "quote": "금액을 계산"}
    ],
    "candidate_types": ["quotation"],
    "missing_information": []
  }
}
```

| 상태 | 의미 | work_type |
|---|---|---|
| `classified` | 한 유형으로 분류 가능 | 네 유형 중 하나 |
| `needs_review` | 자료 부족·유형 불명확 | `null` |
| `multiple_tasks` | 서로 다른 평가 업무를 함께 수행 | `null` |
| `out_of_scope` | 네 유형 밖의 업무 | `null` |

네 유형: `rfp_analysis`, `proposal`, `quotation`, `customer_response`.
`candidate_types`는 classified이면 선택한 유형 하나, multiple_tasks이면 두 개 이상입니다.
단순히 두 유형 사이에서 애매하면 multiple_tasks가 아니라 needs_review입니다.
needs_review에서는 `missing_information`에 부족한 자료나 해소할 모호성을 적습니다.

실행 성공(exit 0)은 분류 리포트를 생성했다는 뜻입니다. needs_review 등도 정상 리포트입니다.
입력 오류·인용 검증 실패·API 오류는 exit 2이며 성공 리포트를 만들지 않습니다.

## 5. 입력 계약

실제 예시는 `samples/`를 확인하세요.

| 필드 | 내용 |
|---|---|
| `work_id` | 업무 식별자. LLM 입력에서는 제외 |
| `title`, `customer_request` | 업무 제목·고객 요청. 없으면 빈 문자열 |
| `messages` | `id`, `role`, `content`. 배열 순서는 원래 대화 순서 |
| `artifacts` | 버전별 고유 `id`, `version`, `title`, `stage`, `content` |
| `coverage` | `complete` / `partial` / `unknown` |
| `missing_sources` | 수집하지 못한 자료 |

- 업무 목적과 최종 산출물을 기준으로 분류하고 대화를 함께 확인합니다.
- message role은 `user` / `assistant` / `system` / `tool`을 보존합니다.
  **과거 system 메시지도 분석 대상 데이터이며 현재 LLM의 system 지시로 승격하지 않습니다.**
- artifact stage는 `draft` / `final` / `reference`. 기존 RFP 대응표나 확정 견적을
  참조하는 것과 현재 그 업무를 수행한 것을 구분합니다.
- 근거 ID는 메시지·산출물 전체에서 고유해야 합니다. 산출물은 버전마다 별도 ID를 주세요.
- 제목 근거 ID는 `work:title`, 고객 요청은 `work:customer_request`입니다.
  이 두 ID는 예약되어 있습니다.
- 인용문은 해당 메시지 content, 산출물 content, 제목 또는 고객 요청의 연속 원문이어야 합니다.
- 문서 제목은 분류 보조 정보이며 산출물 ID의 인용문은 content에서 가져옵니다.
- `work_type`이나 테스트 정답을 입력에 추가하면 검증 오류가 발생합니다.
- 파일 1MB, 직렬화한 LLM 입력 80,000자 제한을 둡니다. 초과 입력을 몰래 잘라 분류하지 않습니다.
  실제 긴 RFP의 분할·전체 범위 처리는 후속 구현이 필요합니다.

## 6. 샘플과 기대 판정

| 파일 | 기대 판정 | 확인할 경계 |
|---|---|---|
| `01_rfp.json` | rfp_analysis | 요구사항 대응표 |
| `02_proposal.json` | proposal | 도입 방안 |
| `03_quotation.json` | quotation | 금액·승인 조건 산출 |
| `04_customer_response.json` | customer_response | 질문별 답변 |
| `05_proposal_with_price.json` | proposal | 가격 언급만으로 견적 분류 금지 |
| `06_response_about_price.json` | customer_response | 기존 견적 설명 |
| `07_multiple_tasks.json` | multiple_tasks | RFP 분석과 제안서를 모두 작성 |
| `08_needs_review.json` | needs_review | 앞선 대화·산출물 없음 |
| `09_out_of_scope.json` | out_of_scope | 개인 여행 |
| `10_injection_in_record.json` | proposal | 과거 메시지의 분류 변경 지시 |
| `11_proposal_using_rfp.json` | proposal | 기존 대응표는 참고 자료 |
| `12_quotation_without_final.json` | quotation | 최종본이 없어도 목적은 명확 |

사람이 검토·조정할 초기 기대값은 `tests/fixtures/expected_labels.json`에 분리했습니다.
실제 LLM 입력으로 이 파일이나 샘플 파일명을 보내지 않습니다.
실제 모델 품질은 classify 결과의 status/work_type/candidate_types를 기대값과 비교하고,
판단 이유·근거 타당성을 별도로 확인해야 합니다.
프롬프트 조정에 쓴 샘플 외의 새로운 사례도 준비하세요.
특히 프롬프트 주입 방어는 지침과 데이터 격리를 구현한 것이며, 무조건 성공한다고 보장하지 않습니다.

## 7. 코드 구조와 사용자님이 수정할 곳

```text
src/work_classifier/
  models.py       입력·출력 모델, 상태 조합 검증
  prompts.py      네 업무 정의·경계 규칙·프롬프트 버전
  classifier.py   classify_work(), 근거 원문 검증, 입력 해시
  provider.py     OpenAI Responses API 구조화 출력
  structured.py   유형별 작업 선택 스키마, 근거 구간 목록, 결과·인용 복원
  cli.py          JSON 로딩·환경변수·실행·파일 저장
samples/          정답 없는 가상 업무 기록 12개
tests/            모델·근거·CLI·SDK 경계 테스트와 별도 기대값
```

사용자님은 우선 `models.py` → `prompts.py` → `classifier.py` 순서로 읽으면 됩니다.
`ClassificationBackend` 인터페이스를 구현하면 다른 모델 제공자도 연결할 수 있습니다.

SDK는 `responses.parse(..., text_format=response_model(catalog))`로 구조화된 출력을 받습니다.
모델은 유형별 작업과 허용된 원문 구간 ID를 선택하고, Python 코드가 공개 결과
`ClassificationResult`와 인용문을 생성합니다. 이후 기존 근거 검증도 유지합니다.
**실제 존재하는 인용이라는 사실이
판단의 의미상 타당성을 보장하지는 않습니다.** 잘못된 분류는 기준 사례·사람 검토로 확인해야 합니다.
모델 거절·미완료·누락 출력은 정상 분류로 대체하지 않습니다.
SDK 요청 timeout은 60초, SDK 재시도는 최대 1회입니다. 전체 실행 시간은 재시도로 더 길 수 있습니다.

## 8. 나중에 MCP 연결

지금 입력 경로:

```text
load_context(JSON 파일) → WorkContext → classify_work(context, backend)
```

나중 입력 경로:

```text
get_work_case / get_conversation / get_artifact_version
→ MCP 응답 어댑터 → WorkContext → 같은 classify_work(context, backend)
```

MCP 연결은 `provider.py`가 아니라 **입력 어댑터**로 추가합니다.
`provider.py`는 업무 데이터의 출처가 아니라 LLM 제공자 연결을 담당합니다.
확정되지 않은 MCP 인자·응답을 현재 코드에 가정해서 넣지 않았습니다.

## 9. 명령 한 번의 코드 실행 순서

```bash
python -m work_classifier classify samples/03_quotation.json \
  --output outputs/quotation-run-02.json
```

1. `__main__.py`가 `cli.main()`을 호출합니다. `work-classifier` 명령도
   `pyproject.toml`의 `[project.scripts]` 설정을 통해 같은 함수로 진입합니다.
2. `cli.parser()`가 `classify`, 입력 파일, 출력 경로를 읽습니다.
3. `cli.load_context()`가 파일을 읽어 `WorkContext.model_validate_json()`으로 검증합니다.
   이때 아직 API를 호출하지 않습니다.
4. `cli.run_classification()`이 `.env`에서 API 키·모델을 읽고 `AsyncOpenAI` 객체를 만듭니다.
   API 키는 프롬프트에 넣지 않고 SDK 인증에만 사용합니다.
5. `classifier.classify_work()`가 업무 입력을 다시 검증하고 LLM용 JSON을 만듭니다.
   업무 ID와 파일명은 분류 단서에서 제외합니다.
6. `prompts.SYSTEM_PROMPT`의 업무 정의·경계 규칙과 JSON을
   `provider.OpenAIClassifier.classify()`에 전달합니다.
7. `provider.py`의 `await client.responses.parse(...)`가 실제 외부 API 호출입니다.
   system 메시지는 분류 지침, user 메시지는 분석할 업무 JSON입니다.
   JSON 내부의 과거 system 메시지는 현재 system 메시지가 아닙니다.
   `structured.py`가 원문 구간을 나눠 `evidence_catalog`를 만들고, 해당 입력의 허용된
   근거 ID만 enum으로 열거한 `response_model(catalog)`를 `text_format`으로 전달합니다.
8. 응답 거절·미완료 여부를 검사한 뒤 `response.output_parsed`를 꺼냅니다.
9. 모델 전용 `DecisionEnvelope`를 검증한 뒤 유형별 선택 수로 단일/복합 상태를 계산합니다.
   선택한 구간의 원문을 그대로 복원하고 공개 `ClassificationResult`를 생성합니다.
   `ClassificationResult`가 상태·업무 유형 등의 조합을 검사하고
   `validate_evidence()`가 근거 ID 존재·인용문 일치·중복을 확인합니다.
   이는 의미상 분류 정확성을 보장하는 검사는 아닙니다.
10. `ClassificationReport`에 입력 해시·모델·프롬프트 버전·응답 ID를 붙입니다.
11. CLI가 리포트를 출력하고 `--output`이 있으면 파일에도 저장한 뒤 종료합니다.
    기존 파일은 덮어쓰지 않으며, 서버처럼 계속 실행되는 프로그램이 아닙니다.

`async` 함수는 `await`로 외부 응답을 기다릴 수 있는 함수입니다.
현재 CLI는 `asyncio.run()`으로 한 건을 실행하며 자동 배치·병렬 호출을 하지는 않습니다.
정상 경로에서는 업무 한 건당 모델 요청 한 번이고, SDK의 일시적 오류 재시도는 별도입니다.

| 파일 | 처음 읽을 핵심 |
|---|---|
| `models.py` | WorkContext: 입력 계약 / ClassificationResult: 공개 분류 결과 / ClassificationReport: 실행 결과 |
| `structured.py` | 모델 전용 출력 계약·입력별 허용 근거 ID·단일/복합 상태 및 인용 복원 |
| `prompts.py` | 네 업무 정의, 제안과 가격 질의의 구분, 복합 업무·자료 부족 처리 |
| `classifier.py` | 입력 → 모델 호출 → 근거 검사 → 리포트 조립 |
| `provider.py` | 실제 API 요청·응답과 제공자 오류 처리 |
| `cli.py` | 파일·환경변수·인자·출력 등 터미널 실행 처리 |
| `tests/` | API 없이 데이터 계약·검증·SDK 응답 처리를 검사 |

`ClassificationBackend`는 모델 제공자가 구현할 함수 형태를 정의한 인터페이스입니다.
현재 `OpenAIClassifier`가 그 인터페이스를 구현합니다.
테스트의 가짜 제공자도 같은 형태라서 실제 모델 호출 없이 분류 파이프라인을 검증할 수 있습니다.

## 10. 제공된 raw 40건으로 분류 테스트

`src/work_classifier/e2e.py`는 외부 raw 데이터를 변환하고 실제 모델로 일괄 테스트하는
별도 실행 모듈입니다. 기존 단건 분류 함수·프롬프트는 변경하지 않았습니다.
이름이 E2E인 데이터셋을 사용하지만, 현재 실행 범위는 **업무 유형 분류**입니다.
평가 점수·MCP 권한·이의 처리·중복 이벤트·장애 주입은 실행하지 않습니다.

원본 위치: `/Users/harim/Downloads/e2e_raw_40/`

- `cases/`: 네 유형별 10건, 총 40건. `all_cases.jsonl`은 같은 데이터이며 중복 실행하지 않습니다.
- 데이터에 있는 `work_type`은 비교용 라벨입니다. 독립적으로 검수된 점수 정답지는 없습니다.
- 최상위/업무 내부의 `work_type`, `summary`, 파일명, 유형이 드러나는 테스트 ID는 모델 입력에서 제외합니다.
- 메시지·산출물 ID를 `m001`, `a001` 등으로 바꾸고, 원래 ID·문서 버전은 `audit/`에 보관합니다.
- 대화 전체, 산출물의 모든 저장 버전, 등록 조건, 고객 제출 원문을 사용합니다.
  과거 버전은 `draft`, 완료 업무의 최신 직원 저장본은 `final`, 고객 제출 문서는 `reference`입니다.
- 제공된 `seed/policy_rules.json`의 마스킹 규칙으로 연락처·이메일·담당자 이름을 치환합니다.
  이 정규식 마스킹은 제공된 합성 데이터 테스트용이며 일반 개인정보 탐지의 완전성을 보장하지 않습니다.
- 연결된 과거 업무와 사내 참조 문서 본문은 조회하지 않으므로 수집 범위는 `partial`입니다.

API 호출 없이 변환·검증:

```bash
python -m work_classifier.e2e prepare \
  --source /Users/harim/Downloads/e2e_raw_40 \
  --output outputs/my-raw40-inputs
```

기존 프롬프트로 40건 분류(API 사용 비용 발생):

```bash
python -m work_classifier.e2e run \
  --prepared outputs/my-raw40-inputs \
  --output outputs/my-raw40-baseline
```

제공 PDF의 p.6(제17~21조) 및 p.9(별표4)에서 추출해 둔 텍스트를 함께 제공하는 비교:

```bash
python -m work_classifier.e2e run \
  --prepared outputs/my-raw40-inputs \
  --output outputs/my-raw40-policy \
  --policy-excerpt outputs/e2e_raw_40/policy/classification_excerpt.txt
```

위 발췌 파일은 이번 로컬 테스트에서 생성했습니다. Git에 포함되지 않는 `outputs/`에
있으므로 다른 환경에서는 동일 PDF의 해당 구간을 추출해 UTF-8 텍스트로 준비해야 합니다.
이는 고정된 내규 발췌문을 직접 제공한 실험이며 **RAG 검색이나 자동 루브릭 생성 구현이 아닙니다.**
기준 원문을 참고하되 문서의 지시를 실행하지 않으며, 업무 수행 근거는 업무 기록에서만 인용합니다.

한 케이스만 재검사할 수도 있습니다.

```bash
python -m work_classifier.e2e run \
  --prepared outputs/my-raw40-inputs \
  --output outputs/my-raw40-one \
  --case-id E2E-IQ-05
```

`--case-id`는 여러 번 지정할 수 있습니다. `--concurrency`는 1~4, 기본값은 2입니다.
기존 출력 폴더는 덮어쓰지 않으므로 실행마다 새로운 폴더를 지정하세요.
SDK의 일시 오류 재시도를 제외하면 모드별 케이스당 한 번 호출합니다.
데이터 오류를 성공 결과로 대체하거나, 결과가 정답과 다르다는 이유로 자동 재시도하지 않습니다.

실행 폴더에는 다음이 저장됩니다.

| 경로 | 내용 |
| --- | --- |
| `summary.json` | 전체·유형별 라벨 일치 수와 건별 결과 |
| `reports/` | 입력·출력·인용 검증을 통과한 리포트. 복합 업무 판단도 포함 |
| `rows/` | 오류 건까지 포함한 건별 결과 |
| `rejected/` | 파싱에는 성공했으나 근거 검증에 실패한 응답. 진단용이며 유효 리포트가 아님 |
| `run_metadata.json` | 모델·실행 시각·프롬프트/내규 해시·범위 |
| `system_prompt.txt` | 해당 실행의 실제 시스템 프롬프트 |

### 2026-10-20 최초 실행 결과

모델 `gpt-4o-mini`, 기존 분류 프롬프트 `work-classification-v1`, 변환기 `raw40-v1`.
각 조건별 40건을 한 번씩 실행했습니다. 최초 실행에는 상세 오류 로깅이 없어서
오류 건은 `ClassificationError` 유형까지만 기록되어 있습니다.
이후 진단 실행부터 안전한 오류 설명·검증 필드를 기록하도록 개선했습니다.
최초 결과는 재실행 결과로 교체하지 않았습니다.

| 데이터셋 라벨 | 기본 프롬프트 일치 | 내규 발췌 추가 일치 |
| --- | --- | --- |
| RFP 분석 | 3/10 | 3/10 |
| 제안서 | 5/10 | 2/10 |
| 견적서 | 6/10 | 3/10 |
| 고객 질의 답변 | 5/10 | 4/10 |
| 합계 | **19/40 (47.5%)** | **12/40 (30%)** |

기본 실행은 일치 19건·복합 업무 판단 14건·오류 7건,
내규 추가 실행은 일치 12건·복합 업무 판단 8건·오류 20건입니다.
오류는 분모에서 제외하지 않았습니다. 이는 제공 라벨과의 일치율이며
운영 정확도나 평가 점수 정확도를 의미하지 않습니다.
원본 결과는 `outputs/e2e_raw_40/baseline/summary.json`과
`outputs/e2e_raw_40/with_policy/summary.json`입니다.

관찰한 문제:

1. 요구사항 추출·대응표 작성·수정처럼 같은 업무의 단계를 서로 다른 유형으로 판단했습니다.
   예를 들어 RFP-02를 기본 실행에서 RFP 분석+제안서로 판단했습니다.
2. 기존 견적에 대한 설명을 신규 견적 작성으로 해석했습니다.
   IQ-01의 회신 업무를 고객 질의 답변+견적으로 판단했습니다.
3. 별도 진단에서는 `multiple_tasks`인데 후보 유형이 두 개 미만인 출력(IQ-08)이
   모델 검증에서 거절됐고, 원문을 `...`로 생략한 인용(QT-01)은 근거 검증에서 거절됐습니다.
   최초 실행의 모든 오류 원인이 동일하다고 단정할 수 없습니다.
4. 재실행 시 일부 상태가 달라졌습니다. 한 번의 비교로 내규 제공 자체가 성능 저하의
   원인이라고 결론 낼 수 없고, 반복 실행 및 별도 검증 세트가 필요합니다.

내규와 데이터셋에서 합의할 사항:

- PDF `SALES-AI-GL-001 v1.0` 시행일은 2026-11-01이고 케이스 완료일은 2026-10-05~20입니다.
  이번 내규 추가 실행은 가상 비교 실험으로 명시했습니다. 운영 평가는 업무 시점에 유효한 버전이 필요합니다.
- 데이터셋의 RFP 분석에는 정식 RFP가 없는 이메일·미팅 메모의 요구사항 정리도 포함됩니다.
  내규 제17조는 RFP 원문을 중심으로 서술하므로 분류 정의를 명확히 합의해야 합니다.
- 내규에는 제17~21조/별표4의 유형별 지침뿐 아니라 별표5의 평가 항목·점수 기준도 이미 있습니다.
  평가 구현 시 원문 기준을 구조화하고 문서·조항·버전을 연결하는 것이 우선입니다.

이 최초 실행은 수정 전 기준점으로 보존합니다. 이후 개선 내용과 재시험은 아래에 기록합니다.

## 11. 단일/복합 업무와 출력·근거 오류 개선

이 절의 개선 당시 버전은 `work-classification-v4` / `evidence-selection-v3`입니다.
중간 개선판 v2와 채택하지 않은 v3 실험 결과도 별도 폴더에 보존했습니다.

분류 기준:

- 같은 목적의 초안·수정·검산·승인 확인·재저장은 한 업무 과정입니다.
- 기존 RFP나 대응표를 참고했다는 이유만으로 현재 RFP 분석 업무를 추가하지 않습니다.
- 제안서 안의 가격 교정이나 고객 회신 안의 기존 견적 설명은 별도 견적 업무의 충분한 근거가 아닙니다.
- 서로 다른 유형마다 독립적인 목적과 실제 수행 근거가 있어야 복합 업무입니다.
  예를 들어 대응표와 제안서를 실제로 각각 작성하거나, 고객 회신과 변경 견적 산출을 함께 수행한 경우입니다.
- 유형이 애매해 두 후보가 있는 상황은 `needs_review`이며 복합 업무가 아닙니다.
- 산출물 두 개가 모두 견적서라면 유형 분류에서는 `quotation` 하나입니다.
- 문서 품질/규정 위반 여부와 업무 유형을 구분합니다.

모델 전용 출력 예:

```json
{
  "decision": {
    "status": "classified",
    "task": {
      "work_type": "quotation",
      "goal": "신규 견적서 작성",
      "evidence_ids": ["e0003"]
    }
  },
  "reason": "고객 조건에 따른 신규 견적 산출이다."
}
```

단일 업무는 `task` 하나, 복합 업무는 `tasks` 2~4개를 갖는 서로 다른 모델 출력 형태입니다.
코드가 공개 결과의 `work_type`, `candidate_types`, `evidence`를 생성하므로
같은 판단을 여러 필드에 중복 생성하면서 생기던 모순을 줄입니다.
복합 업무 응답에 같은 유형의 하위 작업만 들어오면 유형을 하나로 통합합니다.
이때 모든 근거를 유지하고 `decision_trace.normalizations`에 `same_type_tasks_merged`를
기록하며, 공개 판단 이유에도 통합 사실을 명시합니다. 서로 다른 유형은 임의로 삭제하지 않습니다.
`needs_review`와 `out_of_scope`는 별도 출력 형태로 계속 지원합니다.
공개 `classification`의 필드는 그대로이고, 리포트에 `decision_trace`를 추가해
모델의 원래 작업별 목적과 선택 구간 ID를 보존합니다.

인용 처리:

1. 원문을 줄 단위, 긴 줄은 최대 240자 구간으로 나누고 원문 위치를 보존합니다.
2. 해당 입력에서 실제 존재하는 구간 ID를 JSON Schema의 enum으로 제한합니다.
   `reference` 자료는 판단 배경으로 제공하지만 현재 업무 수행의 인용 후보에서는 제외합니다.
3. LLM은 ID를 선택하고 코드는 해당 원문을 복원합니다. LLM이 인용문을 재작성하지 않습니다.
4. 미등록 ID, 빈 작업 선택, 근거가 없는 주장, 원문 불일치에 대한 검증은 유지합니다.
   원문이 없는 데이터를 생성하거나 인용 검사를 끄는 방식으로 오류를 숨기지 않습니다.

`python -m work_classifier schemas`는 이제 `llm_output`(모델 판정 계약),
`classification`(공개 결과), `input`, `report`를 출력합니다.
실제 API의 입력별 ID enum 스키마는 배치 결과의 `request_schemas/`에서 확인할 수 있습니다.
근거 ID는 원문 위치와 연결되지만, 해당 근거가 분류를 의미상 입증하는지는 별도 검토가 필요합니다.
정답과 다르다는 이유로 재시도하거나 검증 실패를 성공으로 바꾸는 처리는 없습니다.

재시험 명령(API 사용 비용 발생, 매번 새 출력 폴더 지정):

```bash
python -m work_classifier.e2e run \
  --prepared outputs/e2e_raw_40/prepared \
  --output outputs/my-v4-baseline

python -m work_classifier.e2e run \
  --prepared outputs/e2e_raw_40/prepared \
  --output outputs/my-v4-policy \
  --policy-excerpt outputs/e2e_raw_40/policy/classification_excerpt.txt

python -m work_classifier.e2e run \
  --prepared outputs/e2e_raw_40/regression_prepared \
  --output outputs/my-v4-regression
```

회귀 세트는 기존 12개 샘플과 `tests/fixtures/boundary_inputs/`의 새 경계 사례 3개로
구성했습니다. 실제 복합 업무, 모호한 후보, 같은 유형의 견적서 두 개를 포함합니다.
별도 입력과 라벨을 분리하며, 이는 회귀 점검용 세트로 독립적인 운영 정확도 평가 세트는 아닙니다.

### 동일 40건 재시험 결과

모델 `gpt-4o-mini`, 동일한 `prepared/inputs`와 동일한 내규 발췌를 사용했습니다.
각 조건은 한 번씩 실행했으며 라벨 일치 여부에 따른 재시도는 없습니다.
원래 제공 라벨·마스킹·ID 변환 방식은 바꾸지 않았습니다.
저장된 이전 정상 리포트의 입력 해시도 동일함을 확인했습니다.

| 유형 | v1 기본 | v4 기본 | v1 내규 포함 | v4 내규 포함 |
| --- | --- | --- | --- | --- |
| RFP 분석 | 3/10 | 8/10 | 3/10 | 9/10 |
| 제안서 | 5/10 | 9/10 | 2/10 | 8/10 |
| 견적서 | 6/10 | 10/10 | 3/10 | 9/10 |
| 고객 질의 답변 | 5/10 | 10/10 | 4/10 | 10/10 |
| 합계 | **19/40 (47.5%)** | **37/40 (92.5%)** | **12/40 (30%)** | **36/40 (90%)** |
| 출력·근거 처리 오류 | 7 | **0** | 20 | **0** |
| 복합 업무 판단 | 14 | 3 | 8 | 4 |

기본 조건에서는 기존 불일치 18건을 개선했고 기존 일치 건의 회귀는 없었습니다.
내규 조건에서는 25건을 개선했으나 `E2E-PR-06` 한 건이 일치에서 불일치로 바뀌었습니다.
같은 유형 하위 작업을 하나의 유형으로 정규화한 건은 각 조건에서 5건입니다.
해당 결과는 `decision_trace.normalizations`로 구분할 수 있습니다.
최종 80개 리포트의 모든 근거가 입력 원문과 정확히 일치하는지도 다시 검사했습니다.

남은 불일치:

- 기본: `E2E-PR-06`, `E2E-RFP-01`, `E2E-RFP-05`.
- 내규 포함: `E2E-PR-02`, `E2E-PR-06`, `E2E-QT-02`, `E2E-RFP-05`.
- 모두 단일 업무 라벨을 서로 다른 유형의 복합 업무로 판단한 경우입니다.
  RFP를 참조한 제안/견적이나 대응표의 설명을 독립 업무로 추가하는 의미 판단 문제가 남았습니다.
  이 경우 서로 다른 유형을 임의로 제거해 정답에 맞추지 않습니다.

별도 15개 회귀 사례는 **15/15 일치**했습니다.
기존 복합 사례와 새 고객 회신+변경 견적 사례는 여전히 복합 업무로,
정보 부족 사례는 검토 필요로, 범위 밖 사례는 범위 밖으로 분류됐습니다.
이는 40건을 모두 단일로 강제해서 얻은 결과가 아닙니다.
오프라인 자동 테스트 **72개**, Ruff 코드 검사도 통과했습니다.

이번에는 동일 40건을 관찰하며 개선했으므로 이 일치율은 **개발 세트의 재시험 결과**입니다.
실제 운영 정확도나 독립적인 일반화 성능을 뜻하지 않습니다. 다음 평가는 별도의 미사용
케이스와 반복 실행으로 수행해야 합니다. 내규 포함/미포함의 1건 차이를 통계적 결론으로 해석하지 않습니다.

보존한 실험:

| 버전 | 기본 일치 | 내규 포함 일치 | 비고 |
| --- | --- | --- | --- |
| v1 | 19/40 | 12/40 | 수정 전 기준점 |
| v2 | 33/40 | 33/40 | 단일/복합 선택과 원문 구간 선택 도입 |
| v3 | 15/40 | 18/40 | 네 유형 슬롯을 채우는 구조에서 불필요한 선택 증가, 채택하지 않음 |
| v4 | 37/40 | 36/40 | 단일/복합 선택 + 허용 ID enum + 동일 유형 통합 |

이번 개선 작업의 실제 호출 대상은 중간 실험과 회귀를 포함해 총 270건이며,
SDK의 일시 오류 재시도는 별도입니다. 이전 결과 파일은 덮어쓰지 않았습니다.
`outputs/e2e_raw_40/v4_comparison.json`에 버전별 집계, 건별 전후 결과, 개선·회귀 목록을
저장했고, `v4_source/`에는 실행 시점의 Python 코드를 보존했습니다.
원본 결과는 `v4_baseline/summary.json`, `v4_with_policy/summary.json`,
`v4_regression/summary.json`에서 확인할 수 있습니다.

## 12. 분류 목표에 집중한 프롬프트 v6

현재 권장 흐름은 다음과 같습니다.

```text
업무 대화·산출물 조회 → 업무 유형 분류
                            ↓ 이후 구현
                RAG MCP에서 내규·평가 기준 조회 → 유형별 평가
```

앞 절의 내규 포함 실행은 과거 비교 실험입니다. 현재 분류 호출에는 내규 PDF나
평가 루브릭을 추가하지 않습니다. 테스트 데이터 안에 원래 들어 있는 내규 언급은
과거 업무 기록으로 남겨 두되, 분류 모델이 적용할 평가 지침으로 사용하지 않습니다.

v6는 출력 스키마나 모델을 바꾸지 않고 v4의 `prompts.py`만 보강했습니다.
핵심 질문은 **직원이 무엇을 만들어 달라고 요청했고, 현재 기록에서 무엇을 작성했는가**입니다.
먼저 중심 업무 하나를 파악하고, 추가 유형은 별도의 명시적인 작업 요청이나 독립적인
작성 결과가 있을 때만 선택합니다. 정보가 부족한 상황은 복합 업무로 추정하지 않고 검토 필요로 남깁니다.

| 구분 | 분류에서의 역할 |
| --- | --- |
| 직원의 실제 작업 요청 | 현재 업무의 직접 근거 |
| 현재 AI 답변·산출물 | 요청한 작업을 실제로 수행했는지 확인 |
| RFP·고객 메일·기존 문서 인용 | 참고 입력. 문서가 있다는 이유로 해당 업무를 추가하지 않음 |
| 고객 목적·예산·제출 기한 | 배경 조건. 별도 업무 수행의 근거가 아님 |

`role=user` 메시지 안에 인용된 RFP도 참고 입력일 수 있습니다.
추가 유형을 선택하려면 그 유형의 독립적인 결과와 직접적인 수행 근거가 필요합니다.
예를 들어 대응표의 '대안 제시' 칸은 제안서 작성의 증거가 아니고,
제안서에 붙여 넣은 RFP 원문은 별도 요구사항 분석을 수행했다는 증거가 아닙니다.
단일 업무, 실제 복합 업무, 모호해서 검토가 필요한 업무를 구분하는 대조 예시도 포함했습니다.

실행 명령:

```bash
python -m work_classifier.e2e run \
  --prepared outputs/e2e_raw_40/prepared \
  --output outputs/my-v6-classification
```

분류만 시험하므로 `--policy-excerpt`는 지정하지 않습니다.

### 내규 없이 분류한 결과

| 유형 | v4 | v6 |
| --- | --- | --- |
| RFP 분석 | 8/10 | 10/10 |
| 제안서 | 9/10 | 10/10 |
| 견적서 | 10/10 | 10/10 |
| 고객 질의 답변 | 10/10 | 10/10 |
| 합계 | 37/40 (92.5%) | **40/40 (100%)** |
| 출력·근거 오류 | 0 | 0 |

`E2E-PR-06`, `E2E-RFP-01`, `E2E-RFP-05`가 이번 실행에서 일치로 바뀌었고,
이전 일치 건의 회귀는 없었습니다. 같은 유형 하위 작업 통합은 1건이며 원래 응답과 통합
기록을 리포트에 보존합니다.

별도 경계 사례는 **15/15 일치**했습니다. 실제 복합 업무 2건을 포함하며,
정보 부족·범위 밖 처리도 유지했습니다. 최종 55개 리포트의 원문 인용 검증도 통과했습니다.
v4와 v6의 모델·입력 해시·출력 스키마 해시가 동일하고, Python 소스는 `prompts.py`만
달라졌음을 확인했습니다.

앞서 규칙을 길게 다시 작성한 v5는 28/40, 경계 사례 12/15로 후퇴해 채택하지 않았습니다.
그 결과도 `v5_baseline/`, `v5_regression/`에 보존했습니다.
현재 채택한 v6 결과는 `v6_baseline/summary.json`, `v6_regression/summary.json`,
건별 비교는 `outputs/e2e_raw_40/v6_comparison.json`, 소스는 `v6_source/`에 있습니다.
이번 분류 프롬프트 개선에서는 중간 실험까지 110개 케이스를 실행했습니다.

**40/40은 제공된 개발 데이터에서 한 번 관측한 라벨 일치 결과입니다.**
운영에서 항상 정확하다는 뜻은 아니며, 새 데이터와 반복 실행으로 추가 확인해야 합니다.

## 공식 참고

- [OpenAI Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=responses)
- [사용 모델 GPT-4o mini](https://developers.openai.com/api/docs/models/gpt-4o-mini)
