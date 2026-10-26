# 세일즈 AI 코치 — 평가 Agent와 MCP 서버

직원이 AI 업무 공간에서 작성한 대화와 산출물을 회사 평가 기준에 따라 평가하고,
개선 피드백을 제공하는 프로젝트입니다. 이 저장소에는 평가 기준 생성 Agent,
중간 피드백·개인 최종 평가·팀 평가 Agent, MCP 서버와 LLM 기록 프록시가 있습니다.

업무 유형은 **사용자가 업무 시작 시 선택**합니다.
RFP 분석 / 제안서 / 견적서 / 고객 질의 답변을 지원하며,
업무 흐름에서 LLM으로 유형을 자동 분류하지 않습니다.

## 구성과 담당 범위

| 구성 | 역할 | 위치 |
| --- | --- | --- |
| 평가 기준 생성 | 내규의 고정 버전에서 유형별 기준 초안 생성·검증·저장. 승인 후 적용 | `src/ai_agents/criteria_agent/` |
| 중간 피드백 | 사용자가 요청한 시점까지 완료된 최근 최대 5쌍 평가, 코칭·프롬프트 추천 | `src/ai_agents/evaluation_agent/feedback/` |
| 개인 최종 평가 | 고정한 업무 자료와 저장된 승인 기준으로 평가·코칭 생성, MCP 저장 | `src/ai_agents/evaluation_agent/final/` |
| 팀 평가 | 공개 가능한 팀 집계 스냅샷으로 강점·보완점·추천 활동 생성 | `src/ai_agents/evaluation_agent/team/` |
| Worker | 큐 수신, 재시도, 체크포인트, 완료·실패 처리 | `src/ai_agents/orchestration/`, `src/ai_agents/integrations/` |
| Business / RAG MCP | 업무 조회·결과 저장 / 내규·기준·참고 자료 검색 | `mcp_servers/` |
| LLM 기록 프록시 | 모델 요청 중계와 `llm_exchanges` 기록 | `llm_proxy/` |
| 프론트엔드 | 업무 공간·피드백·평가 리포트 화면 | [front-end](https://github.com/skala2-ai-agent-capstone/front-end) |
| 백엔드 | 사용자 API·업무 상태·큐 발행·실행 토큰 공급 | [aworko-server](https://github.com/skala2-ai-agent-capstone/aworko-server) |

질문 1개와 AI 응답 1개를 합쳐 **1쌍**으로 셉니다. 중간 피드백은 버튼 요청형이며
매 대화마다 자동으로 실행하지 않습니다. 개인 최종 평가와 팀 평가는 별도 입력과 결과 계약을 사용합니다.

```text
내규 업로드 → 백엔드 → 기준 생성 Worker → 내규 검색·기준 초안 저장 → 관리자 승인

사용자 요청 → 백엔드 → RabbitMQ → 평가 Worker → MCP 조회 → LLM 평가·검증 → MCP 저장
                  └─ 조회 API로 진행 상태와 결과 제공

업무 공간 대화 → 백엔드 → llm-proxy → 모델
                              └─ llm_exchanges → messages 뷰 → Business MCP 조회
```

Agent는 업무 DB를 직접 읽고 쓰지 않습니다. 이 저장소의 MCP 서버가 DB에 접근하고,
백엔드는 요청·상태 전이·토큰 공급을 담당합니다. 본인 확인·이의 제기·검토 결정은 백엔드가 처리합니다.

모델 제공자는 실행 환경 설정으로 정합니다. 프록시는 `OPENAI_STAGES`에 따라
업무 대화와 평가 요청을 서로 다른 모델로 보낼 수 있습니다. 실제 사용 모델은 실행 기록으로 확인합니다.

## 설치

Agent는 Python 3.11 이상을 사용합니다. MCP·프록시는 각각 별도의 의존성과 Docker 설정을 사용합니다.

```bash
git clone https://github.com/skala2-ai-agent-capstone/ai-agent-capstone.git
cd ai-agent-capstone
python3.11 -m venv .venv
source .venv/bin/activate
python -m ensurepip --upgrade
python -m pip install -r requirements.txt

# RabbitMQ Worker를 실행할 때 추가 설치
python -m pip install -r requirements-worker.txt

# 기존 설정이 있으면 덮어쓰지 않음
test -f .env || cp .env.example .env
```

Windows PowerShell의 가상환경 활성화 명령은 `.venv\Scripts\Activate.ps1`입니다.
uv를 사용하는 경우 설치 단계 대신 `uv sync --locked --extra worker`를 사용합니다.
설치 후 아래 Python 명령은 저장소 루트와 활성화된 가상환경을 기준으로 실행합니다.

설정 파일은 용도별로 구분합니다.

| 파일 | 용도 |
| --- | --- |
| [`.env.example`](.env.example) → 루트 `.env` | 로컬 Agent·Worker·품질 실험 |
| [`mcp_servers/.env.example`](mcp_servers/.env.example) → `mcp_servers/.env` | Docker Compose의 DB·MCP·프록시·백엔드·Worker |
| `pyproject.toml`, `uv.lock` | Agent 의존성 정의와 잠금 |
| `requirements.txt`, `requirements-worker.txt` | pip 설치용 기본 / Worker 포함 잠금 결과 |

`OPENAI_API_KEY`만 설정하면 모든 Worker가 실행되는 구조가 아닙니다.
각 기능의 `FEEDBACK_*`, `FINAL_*`, `TEAM_*`, `RUBRIC_*` 설정과 해당 MCP 토큰이 필요합니다.
키와 토큰은 `.env`에 설정하며 Git에 올리지 않습니다.

## API 키 없이 확인하기

```bash
# 평가셋 형식·근거 연결 검사: 모델·MCP 호출 없음
python -m ai_agents.cli.quality check --dataset samples/feedback/quality-v1.json
python -m ai_agents.cli.quality check --dataset samples/final/quality-v1.json
python -m ai_agents.cli.quality check --dataset samples/team/quality-v1.json

# Worker 메시지 계약 확인: 연결·소비·저장 없음
python -m ai_agents.cli.feedback schemas
python -m ai_agents.cli.final_worker schemas
python -m ai_agents.cli.team_worker schemas
python -m ai_agents.cli.rubric_worker schemas

# Agent 자동 테스트. DB 의존 검사는 별도 테스트 DB 설정 필요
python -m pytest tests -q
```

자동 테스트의 고정 응답·모의 전송 결과와 실제 LLM의 평가 품질은 구분합니다.
MCP·프록시 전체 검사는 별도 의존성과 테스트 DB가 필요하므로 각 하위 README를 따릅니다.

## 실행할 기능 선택

아래 Worker 실행은 실제 큐 메시지를 소비하고 결과를 저장합니다.
백엔드가 큐를 선언하고 MCP·모델 주소·토큰을 준비한 뒤 필요한 Worker만 실행합니다.

| 기능 | 요청 큐 / 타입 | 실행·계약 문서 |
| --- | --- | --- |
| 중간 피드백 | `live_evaluation.requests` / `LIVE_EVALUATE` | [중간 피드백](docs/recent_five_feedback.md) |
| 개인 최종 평가 | `evaluation.requests` / `EVALUATE` (`backend-v1`) | [백엔드 연결](docs/backend_worker_alignment.md), [최종 Worker](docs/final_evaluation_worker.md) |
| 팀 평가 | `team_evaluation.requests` / `TEAM_EVALUATE` | [팀 평가](docs/team_evaluation.md) |
| 기준 생성 | `rubric_generation.requests` / `RUBRIC_GENERATE` | [기준 생성 Worker](docs/rubric_generation_worker.md) |

```bash
# 중간 피드백: FEEDBACK_* 설정과 회사 범위 Worker 토큰 필요
python -m ai_agents.cli.feedback check-config
python -m ai_agents.cli.feedback worker

# 최종 평가: FINAL_* 설정과 백엔드가 공급하는 평가별 토큰 파일 필요
python -m ai_agents.cli.final_worker worker --contract backend-v1 \
  --token-file /path/to/final-tokens.json

# 팀 평가: TEAM_* 설정과 백엔드가 공급하는 요청별 토큰 파일 필요
python -m ai_agents.cli.team_worker worker --token-file /path/to/team-tokens.json

# 기준 생성: RUBRIC_* 설정과 회사 범위 rubric 토큰 필요
python -m ai_agents.cli.rubric_worker check-config
python -m ai_agents.cli.rubric_worker worker
```

`/path/to/...`는 실제 토큰 파일 경로로 바꿉니다. 파일 형식·발급 방법은 기능별 문서를 참고하세요.
최종 Worker의 CLI 기본값은 별도 계약인 `agent-v1`이므로 현재 백엔드에는
**`--contract backend-v1`을 명시**합니다. 루트 `FINAL_MCP_TOKEN`은 단건 실행용이며,
여러 평가를 소비하는 Worker의 평가별 토큰 파일을 대신하지 않습니다.

Docker 실행은 [서버 배포 안내](docs/deploy_server.md)를 따릅니다.
기본 앱 구성은 중간 피드백 Worker를 포함하며 `final`, `team`, `rubric` 프로필로
나머지 Worker를 선택합니다. 코드·Compose에 정의되어 있는 것과 서버에서 실행 중인 것은 다르므로
배포 상태는 `docker compose ps`와 해당 환경의 실행 기록으로 확인합니다.

## 실제 LLM 품질 검증과 결과 기록

MCP 없이 고정 가상 입력으로 평가하려면 루트 `.env`의 `QUALITY_LLM_BASE_URL`,
`QUALITY_LLM_MODEL`, `QUALITY_LLM_API_KEY`를 설정합니다. 모델·배포 버전은
`QUALITY_MODEL_REVISION`에 기록합니다. 아래 `run`은 실제 모델을 호출합니다.

```bash
python -m ai_agents.cli.quality run \
  --dataset samples/feedback/quality-v1.json \
  --output-dir outputs/quality/feedback-run-01 --label feedback-baseline
```

실행 후 `reviews.json`에 사람 검토를 기록하고 보고서를 생성합니다.

```bash
python -m ai_agents.cli.quality report outputs/quality/feedback-run-01/experiment.json \
  --reviews outputs/quality/feedback-run-01/reviews.json \
  --output-dir outputs/quality/feedback-run-01-report
```

`LANGSMITH_API_KEY`, `LANGSMITH_PROJECT`를 설정하고 지원하는 실행 명령에 `--langsmith`를
추가하면 정제된 단계·해시·trace ID를 전송합니다. 원격 저장 여부는 LangSmith에서 trace ID로 확인합니다.
자세한 전후 비교·검토 절차는 [LLM 품질 검증](docs/offline_llm_quality.md),
모델 없는 최종 평가 고정 검증셋은 [기능 검증](docs/final_evaluation_validation.md)을 참고하세요.

**`outputs/`는 Git 제외 대상입니다.** 실행 결과·체크포인트·검증결과서가 로컬에 있어도
GitHub에는 자동으로 올라가지 않습니다. 공유할 증빙은 필요한 파일을 별도로 선정하고,
실험 조건·커밋·평가셋 해시와 함께 보관합니다. 전체 `outputs/`를 한꺼번에 추가하지 않습니다.

## 폴더 구조

```text
src/ai_agents/
  criteria_agent/          평가 기준 생성·내규 처리
  evaluation_agent/
    feedback/              최근 최대 5쌍 중간 피드백
    final/                 개인 최종 평가·근거 검증·코칭
    team/                  팀 집계 기반 코칭
  orchestration/           기능별 실행 흐름·큐 Worker·승인 그래프
  integrations/            MCP·RabbitMQ·토큰 파일 어댑터
  shared/                  공통 요청·응답·스냅샷·평가 계약
  observability/           로컬 추적·LangSmith
  validation/              평가셋 실행·품질 검토·전후 비교
  cli/                     기능별 실행 진입점
  classification/          과거 유형 분류 실험 재현용
mcp_servers/               MCP 서버·DB 스키마·시드·Compose
llm_proxy/                 모델 기록 프록시
samples/                   기능·품질 평가셋과 요청 예시
tests/                     Agent 자동 테스트
docs/                      구현·계약·배포·검증 문서
e2e_raw_40/                과거 분류 실험용 가상 데이터
outputs/                   로컬 실행 산출물 (Git 제외)
```

평가 기준 생성 로직은 `criteria_agent/`, 평가 실행 로직은 `evaluation_agent/`에서 관리합니다.
기준 항목·배점·응답 계약을 바꿀 때는 `shared/` 및 MCP 응답과의 호환성을 함께 확인합니다.
루트의 `ai-agents` / `python -m ai_agents` 명령은 **과거 분류 CLI**로 연결됩니다.
현재 기능은 위의 `python -m ai_agents.cli.<기능>` 명령을 사용합니다.

## 상세 문서

- [문서 목록과 읽는 순서](docs/README.md)
- [MCP 서버·DB 실행 및 검증](mcp_servers/README.md)
- [LLM 기록 프록시](llm_proxy/README.md)
- [개인 최종 평가 페이지 데이터](docs/final_evaluation_page.md)
- [과거 분류 실험과 초기 README](docs/archive/classification_readme_20261026.md)

과거 분류 실험의 40/40 같은 수치는 당시 개발 데이터의 유형 일치 결과입니다.
현재 평가 Agent의 점수 정확도나 전체 서비스 품질 수치로 사용하지 않습니다.
