# Agent 폴더 구조 변경 안내

## 팀원에게 전달할 설명

기존 `work_classifier`는 업무 분류로 시작한 이름이지만 현재 기준 생성·평가·추적까지
담당하고 있어 최상위 Python 패키지를 `ai_agents`로 변경했습니다.
기준 생성은 `criteria_agent`, 기존 최종 평가는 `evaluation_agent/final`로 구분하고,
중간 피드백을 구현할 위치는 `evaluation_agent/feedback`으로 마련했습니다.
터미널 실행 진입점과 보조 코드는 `cli` 아래에 모았습니다.

이번 변경은 파일 위치와 import·리소스·실행 경로의 정리입니다.
평가 기준 생성·채점·프롬프트·승인 로직과 `guidance.json` 내용은 유지합니다.
MCP 서버와 DB 구조도 변경하지 않습니다. 중간 피드백은 아직 구현하지 않았습니다.

기존 작업 브랜치에서 수정 중인 파일이 있다면 아래 대응표에 따라 새 경로에 반영하세요.
특히 기준 생성기에서 지침 JSON을 읽는 패키지 경로는 `ai_agents.criteria_agent`입니다.

## 현재 구조

```text
src/ai_agents/
├── classification/            업무 유형 분류
├── criteria_agent/            기준 생성·내규 처리 — 기준 담당 팀원의 주 작업 영역
│   ├── builder.py
│   ├── policy.py
│   └── guidance.json
├── evaluation_agent/          업무 평가
│   ├── final/                 기존 최종 평가·근거 검증·코칭
│   └── feedback/              __init__.py만 있음; 중간 피드백 후속 구현
├── orchestration/             분류·기준 승인·평가 실행 흐름
├── integrations/              MCP 연결·조회·응답 변환
├── shared/                    공통 요청·응답·기준·스냅샷 모델
├── observability/             로컬 추적·LangSmith
├── validation/                평가셋 실행·결과 저장·전후 비교
├── cli/                       실행 진입점·고정 응답 데모 보조 코드
│   ├── classification.py      분류 명령의 main 함수
│   ├── mcp.py                 MCP 조회·기준 승인 실행
│   ├── evaluate.py            승인된 JSON으로 최종 평가 실행
│   ├── workflow.py            전체 승인·평가 흐름 실행
│   ├── e2e.py                 분류 데이터 변환·일괄 실행
│   ├── validation.py          계약 스키마·검증 실험·전후 비교 명령
│   └── demo.py                테스트에서 쓰는 고정 응답 데모
└── __main__.py                python -m ai_agents → 분류 main

mcp_servers/                   별도로 실행하는 business/rag MCP 서버
tests/                         기존 테스트; import 경로만 변경
```

`team_reports/`는 팀 단위 집계 기능을 구현하는 단계에서 추가합니다.
현재 `final/workflow.py`의 평가 연결부와 `orchestration/graph.py`의 승인 그래프는
기존 책임을 그대로 유지합니다. 이번 이동에서 두 흐름을 재작성하거나 합치지 않았습니다.

## 이전 경로 → 새 경로

| 기존 `src/work_classifier/` 기준 | 새 `src/ai_agents/` 기준 |
| --- | --- |
| `classification/` | `classification/` |
| `rubric/` | `criteria_agent/` |
| `evaluation/` | `evaluation_agent/final/` |
| `orchestration/`, `integrations/`, `shared/` | 동일 하위 경로 |
| `observability/`, `validation/`의 실행기·모델 | 동일 하위 경로 |
| `cli.py` | `cli/classification.py` |
| `mcp_cli.py` | `cli/mcp.py` |
| `evaluate_cli.py` | `cli/evaluate.py` |
| `evaluation_workflow_cli.py` | `cli/workflow.py` |
| `e2e.py` | `cli/e2e.py` |
| `workflow_demo.py` | `cli/demo.py` |
| `validation/cli.py` | `cli/validation.py` |

Python import 예시:

```python
from ai_agents.criteria_agent.builder import OpenAIRubricBuilder
from ai_agents.evaluation_agent.final.agent import OpenAIEvaluator
from ai_agents.shared.execution_contracts import EvaluationRequest, FeedbackResult
```

역할 구분에서 주의할 점:

- `criteria_agent/`는 기준을 생성하는 영역입니다. 항목별 점수·필수 근거 제약 일부는
  `evaluation_agent/final/contract.py`, `selection.py`, `validation.py`, `models.py`에도 있습니다.
  팀원이 항목·배점 규칙을 바꿀 때 이 평가 코드와 공통 모델의 영향을 함께 확인해야 합니다.
- `evaluation_agent/final/validation.py`는 한 번의 모델 출력 검증이고,
  최상위 `validation/`은 여러 사례를 실행해 품질을 비교하는 실험 기반입니다.
- `feedback/`는 사용자 질문과 AI 응답을 합친 1회를 기준으로 최근 5회 피드백을
  구현할 위치입니다. 요청별 MCP 조회·저장, 목적별 분기, 피드백 생성은 다음 작업입니다.

## 팀원이 변경을 받은 뒤 실행할 명령

Python import 이름은 `ai_agents`, 설치 패키지와 분류 명령 이름은 `ai-agents`입니다.
구 이름의 호환 패키지·실행 파일은 제공하지 않으므로 로컬 환경을 갱신해야 합니다.

```bash
# 저장소 루트에서: 기존 환경의 다른 도구를 유지하며 새 패키지 설치
uv sync --locked --inexact

# 이전 이름으로 설치했던 패키지 제거
uv pip uninstall --python .venv/bin/python work-classifier

# 모델·MCP를 호출하지 않는 확인 명령
.venv/bin/ai-agents validate samples/*.json
.venv/bin/python -m ai_agents.cli.validation schemas
.venv/bin/python -m pytest -q
```

pip를 쓰는 팀원은 `python -m pip install -r requirements.txt`로 새 패키지를 설치한 뒤
`python -m pip uninstall work-classifier`로 이전 패키지를 제거하면 됩니다.

| 용도 | 새 실행 명령 |
| --- | --- |
| 분류 입력 검사 | `python -m ai_agents validate samples/*.json` |
| 분류 | `python -m ai_agents classify samples/03_quotation.json` |
| MCP 조회 | `python -m ai_agents.cli.mcp fetch --evaluation-id <ID>` |
| 기준 승인까지 실행 | `python -m ai_agents.cli.mcp run --evaluation-id <ID>` |
| 전체 승인·평가 실행 | `python -m ai_agents.cli.workflow --evaluation-id <ID>` |
| 승인된 입력 평가 | `python -m ai_agents.cli.evaluate --help`로 필수 인자 확인 |
| 분류 일괄 실행 | `python -m ai_agents.cli.e2e --help` |
| 스키마·검증·비교 | `python -m ai_agents.cli.validation --help` |

기존 CLI의 인자·데이터 형식은 유지합니다. 검증 실행기의 `--target`도
`ai_agents.validation.targets:validate_request`처럼 새 import 경로로 바꿔야 합니다.
이전 실험 결과의 manifest에는 당시 target 이름과 커밋이 남아 있는 것이 맞으므로
`outputs/`의 과거 기록은 수정하지 않습니다.

## 이슈·PR 진행 제안

이슈 작성은 필수가 아닙니다. 여러 담당자에게 영향을 주는 이번 변경은 이슈 하나로
범위와 완료 조건을 공유하고, PR에서 실제 diff와 검증 결과를 검토하면 좋습니다.
이슈·PR은 아직 생성하지 않았습니다.

이슈 제목 예시: **Agent 패키지 구조 정리 및 실행 경로 통일**

- 목적: 기준 생성·최종 평가·중간 피드백의 작업 위치를 명확히 한다.
- 범위: 파일 이동, import·리소스·패키징·명령·문서 갱신.
- 완료 조건: 기존 테스트 통과, 새 CLI 동작, 배포 패키지에 기준 지침 JSON 포함.
- 별도 작업: 최근 대화 5회 기반 중간 피드백 구현. 이 변경에 기능을 섞지 않는다.

현재 `feat/agent-package-layout`은 `feat/evaluation-contracts-observability`의
`0673649`에서 시작했습니다. 기반 브랜치 PR을 먼저 main에 머지한 뒤 이 변경을 올리거나,
검토를 먼저 받으려면 이 PR의 base를 기반 브랜치로 지정하고 기반 PR 머지 후 main으로
조정하세요. main에 기반 변경이 없는 동안 곧바로 main을 대상으로 비교하면 이전 기반
구현까지 diff에 함께 표시됩니다.

이번 변경은 기능 추가가 아닌 구조 정리이므로 커밋 메시지 예시는 다음과 같습니다.

```text
refactor: Agent 역할에 맞게 패키지 구조와 실행 경로 정리
```

커밋·push는 작업자가 직접 수행합니다. 현재 저장소 루트에서 실행할 명령:

```bash
git add -A -- src tests docs README.md pyproject.toml uv.lock requirements.txt
git diff --cached --stat
git diff --cached --check
git commit -m "refactor: Agent 역할에 맞게 패키지 구조와 실행 경로 정리"
git push -u origin feat/agent-package-layout
```

이 명령은 루트의 개인 기획안 PDF를 추가하지 않습니다. Git은 파일 이동을 유사도에
따라 인식하므로 add 전에는 삭제와 새 폴더로 보이다가, add 후에는 rename으로 보일 수 있습니다.

## 이번 폴더 정리의 검증 결과

- 기존 테스트 **569개 통과**, `ruff check src tests` 및 `git diff --check` 통과.
- 이동한 기존 Python 모듈 48개: import·패키지 리소스 경로와 모듈 설명을 제외한
  실행 구문(AST)이 이동 전과 동일함을 확인. `guidance.json`은 바이트 단위로 동일.
- 새 패키지 설치 후 저장소 밖에서도 6개 실행 진입점의 `--help` 정상 동작.
- 설치된 `ai-agents validate`로 가상 입력 12개 검사 통과.
- 새 `--target` 경로로 계약 사례 5개 통과.
  로컬 결과: `outputs/validation/package-layout-smoke/` (미커밋 상태 실험).
- wheel 빌드 성공. 소스 경로를 제외한 상태에서 wheel의 모듈 50개를 import했고,
  패키지 안의 기준 지침 JSON이 소스와 동일함을 확인.
- 실제 모델·MCP 서버·LangSmith 전송은 이번 구조 변경 확인에서 호출하지 않음.
- 커밋·push·이슈·PR 생성은 수행하지 않음.
