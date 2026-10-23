# 최종 평가 검증 기록·전후 비교

브랜치: `feat/final-evaluation-validation`, 기반: `2b14be1`.
worker 브랜치와 독립적으로 병합할 수 있다. 평가 기준 생성 코드는 수정하지 않는다.

## 이번에 추가한 것

- `samples/final/functional-v1.json`: 고정 가상 데이터 8건. 4종 업무 정상 저장,
  초안 기준 거부, 잘못된 기준 버전, 다른 평가 건 응답, 없는 근거 선택 차단.
- `validation/final_targets.py`: 고정 MCP 응답과 Chat Completions 응답을 재생하며
  실제 최종 평가 워크플로·검증·체크포인트·저장 인자 생성을 실행한다.
- `validation/report.py`, CLI `report`: 실험 정본을 검증결과서용 Markdown으로 요약한다.
- 최종 평가 1건 CLI의 trace metadata에 실제 `git_commit`을 연결한다.
  기존 `code` dict는 정제 metadata 허용 목록에서 제외되어 추적에 나타나지 않았다.

평가셋은 저장소의 `mcp_servers/data/all_cases.jsonl`, 기준 seed와 기존 테스트 fixture에서
응답을 고정했다. 원본 `e2e_raw_40/README.md`에서 모든 회사·수치·연락처가 가상임을 명시한다.
고정 모델 응답의 0점은 검증용 값이며 업무 평가 정답이 아니다.
이 검사는 **계약·흐름 회귀 검사**다. 실제 MCP/DB, 운영 마스킹, RabbitMQ, 백엔드 권한,
AWS 모델 출력 품질·속도 또는 LangSmith 원격 전송 성공을 증명하지 않는다.

## 실행

각 worktree에서 환경을 준비한다. 다른 worktree의 코드가 import되지 않도록 현재 폴더에서 실행한다.
신규 환경에서는 `uv sync --extra worker`, 이후 아래 명령을 `uv run python`으로 실행해도 된다.

```bash
python -m ai_agents.cli.validation run \
  --dataset samples/final/functional-v1.json \
  --target ai_agents.validation.final_targets:replay_final_evaluation \
  --output-dir outputs/validation/final-a --label final-a \
  --model fixture-final-model --prompt-version stored-final-selection-v1 \
  --rubric-version fixture-pinned-v1 --data-version final-functional-v1
```

`prompt-version`과 `rubric-version`은 이 고정 검증셋의 표기다. 실제 기준 원문·각 업무별
고정 버전은 평가셋 내용에 포함돼 `dataset_sha256`으로 묶인다. 테스트용 모델 응답은
점수 정답 데이터가 아니다. 기준 변경으로 평가셋 자체가 바뀌면 기존 평가셋과의 직접 비교는
거부된다. 실제 모델/기준 개선 효과를 비교할 별도 품질 평가셋은 담당자가 검토해야 한다.

결과 폴더에는 `manifest.json`, `dataset.json`, `results.jsonl`, `experiment.json`,
`report.csv`, `traces/`가 생성된다. 기존 폴더/보고서 파일은 덮어쓰지 않는다.
실험 manifest에 커밋·작업 트리 해시·dirty·의존성·모델 설정·평가셋 해시를 기록한다.
`--langsmith`를 추가하면 정제된 해시·단계·trace ID를 원격 전송한다.
`LANGSMITH_API_KEY`와 `LANGSMITH_PROJECT`는 실행 환경에 설정한다. `.env`를 자동 읽는
최종 평가 CLI와 달리 검증 CLI에서는 실행 환경에 직접 설정해야 한다.
LangSmith 원격 전송 실패 여부는 경고와 원격 trace로 확인한다.

```bash
python -m ai_agents.cli.validation report \
  outputs/validation/final-a/experiment.json \
  --output outputs/validation/final-a/report.md
```

## 커밋별 비교

1. 이 기능을 커밋하고 동일 검증셋으로 A 실행을 남긴다.
2. 다음 변경을 별도 커밋하고 B 실행을 남긴다. `--output-dir`과 `--label`만 바꾼다.
3. 실제로 비교 가능한 동일 자료인지 검사한 뒤 비교 JSON과 Markdown을 만든다.

```bash
python -m ai_agents.cli.validation compare \
  outputs/validation/final-a/experiment.json \
  outputs/validation/final-b/experiment.json \
  --output outputs/validation/final-comparison.json

python -m ai_agents.cli.validation report \
  outputs/validation/final-b/experiment.json \
  --before outputs/validation/final-a/experiment.json \
  --output outputs/validation/final-comparison.md
```

코드 커밋은 달라도 된다. 데이터셋 ID/버전/해시, 자료 버전은 동일해야 한다.
모델·프롬프트·기준 설정 변경은 기존 `--allow-change` 옵션으로 명시한다.
이 옵션은 fixture를 실제 모델로 바꾸거나 평가 기준 원문을 교체하는 기능이 아니다.
미커밋 변경이 있으면 보고서에 표시한다. `dirty=true` 실행은 HEAD 커밋만으로 재현되지 않는다.
같은 미커밋 코드의 A/B 실행은 반복 재현성 확인이며 개선 효과 증거가 아니다.

## 검증결과서에 적을 내용

“4개 업무 유형의 저장 기준 기반 최종 평가 흐름과 기준·평가 범위·인용 근거 오류 차단을
고정 가상 데이터 8건으로 검증했다. 실행별 코드·평가셋 해시와 trace ID를 기록하고,
동일 평가셋의 전후 통과/실패/미검토 변화를 비교할 수 있도록 구현했다.”

통과 건수·실행 시각·커밋은 실제 생성된 `report.md`에서 옮긴다.
기능 회귀 검사의 통과율을 LLM 정확도라고 쓰지 않는다.
화면/자료·기대 결과·실제 결과의 상세 증빙은 `report.csv`와 `experiment.json`을 참조한다.
개인 업무 원문을 포함하는 운영 체크포인트를 검증용 가상 데이터로 옮기지 않는다.

커밋 메시지: `feat: 최종 평가 고정 검증셋과 검증결과서 비교 보고서 추가`
