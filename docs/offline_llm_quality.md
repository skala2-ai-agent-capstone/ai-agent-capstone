# MCP 없는 실제 LLM 평가 품질 검증

고정한 가상 대화·산출물·평가 기준을 기존 평가 Agent에 직접 전달한다.
MCP, DB, 기준 생성 Agent, 운영 결과 저장은 호출하지 않는다.
`run`은 실제 모델 서버를 호출한다. 여기서 오프라인은 입력을 파일로 공급한다는 뜻이다.

## 평가셋과 구현 범위

| 평가셋 | 사례 | 목적 |
| --- | ---: | --- |
| `samples/feedback/quality-v1.json` | 20 | 최근 질문·응답 최대 5쌍의 중간 피드백·프롬프트 추천 |
| `samples/final/quality-v1.json` | 8 | 4개 업무 유형 × 일반/보안 검사 자료 누락 |
| `samples/team/quality-v1.json` | 8 | 팀 집계 근거·코칭 선택·자료 부족/공개 제한 |

팀 평가 실행·집계 계약은 [팀 평가 안내](team_evaluation.md)를 참고한다.
팀 평가의 2건은 모델을 호출하지 않고 보류 결과를 만드는 사례다.

중간 피드백에는 모호한 요청, 좋은 요청, AI의 근거 없는 수치, 모순, 마스킹,
프롬프트 주입, AI 행동과 직원 행동의 구분 등이 포함된다.
최종 평가에는 고정한 `request`, `evidence`, `materials`만 넣는다.
기존 기능 테스트의 고정 모델 응답·평가 점수를 모델에 전달하지 않는다.
기대 결과 설명(`expected_description`)도 모델 입력에서 제외한다.

이 평가셋의 기대 결과는 사람 검토 지침 초안이다. 팀원과 합의한 정답 점수나
운영 정확도 기준이 아니며, 현재 최종 평가 8건만으로 전체 품질을 대표하지 않는다.
모델·프롬프트 개선 후에는 보지 않은 사례와 반복 실행도 추가한다.
팀원의 평가 기준 생성 로직은 수정하지 않았다.

## 1. 입력만 검증하기 — 네트워크 호출 없음

저장소 루트에서 가상 환경을 활성화한 뒤 실행한다.

```bash
source .venv/bin/activate
python -m ai_agents.cli.quality check --dataset samples/feedback/quality-v1.json
python -m ai_agents.cli.quality check --dataset samples/final/quality-v1.json
```

전체 입력을 모델 호출 전에 확인한다. 최종 평가 입력의 근거 해시·평가 기준 연결과
중간 피드백 대화 쌍을 검사한다. 원문 공백·줄바꿈을 보존한다.

## 2. 실제 모델 실행

루트 `.env`의 다음 값을 사용한다. 기존 파일을 덮어쓰지 말고 필요한 항목만 추가한다.

```dotenv
QUALITY_LLM_BASE_URL=http://YOUR_MODEL_HOST:8000/v1
QUALITY_LLM_MODEL=YOUR_DEPLOYED_MODEL
QUALITY_LLM_API_KEY=
QUALITY_MODEL_REVISION=YOUR_WEIGHT_OR_DEPLOYMENT_VERSION
LANGSMITH_API_KEY=
LANGSMITH_PROJECT=sales-ai-evaluation
```

명시적인 OpenAI 호환 Chat Completions 주소가 필수다.
중간 피드백은 JSON mode, 최종 평가는 JSON Schema 응답 형식을 지원해야 한다.
주소가 없으면 실행을 거부하며 다른 모델 서비스로 자동 전환하지 않는다.
인증 없는 자체 서버는 API 키를 비워둘 수 있다.
모델 버전을 모르면 `unspecified`로 기록되며, 가중치까지 재현된다고 주장할 수 없다.

```bash
python -m ai_agents.cli.quality run \
  --dataset samples/feedback/quality-v1.json \
  --output-dir outputs/quality/feedback-v1 --label feedback-baseline --langsmith

python -m ai_agents.cli.quality run \
  --dataset samples/final/quality-v1.json \
  --output-dir outputs/quality/final-v1 --label final-baseline --langsmith
```

LangSmith 없이 실행하려면 `--langsmith`를 뺀다. 기존 출력 폴더는 덮어쓰지 않는다.
모든 사례를 순서대로 실행하고, 개별 생성 실패도 기록하며 다음 사례를 진행한다.
실패가 있으면 종료 코드 1, 입력·설정 등 실행 오류는 2다.
SDK의 네트워크 재시도는 0회, Agent의 출력 검증 재시도는 최대 1회다.
따라서 정상 실행은 20/8번, 출력 재생성이 필요하면 최대 40/16번 모델을 호출한다.

## 3. 사람 품질 검토

각 실행 폴더에 `reviews.json`을 생성하며 처음에는 `pending`이다.
모델 호출 없이 보류 결과를 만든 팀 사례는 `not_applicable`로 표시하고 모델 품질
점수 대상에서 제외한다.
`report.csv` 또는 `experiment.json`에서 입력·기대 결과·실제 출력을 확인한 다음
해당 사례의 검토 필드만 바꾼다. ID와 해시는 바꾸지 않는다.

```json
{
  "status": "reviewed",
  "reviewer": "검토자 이름",
  "reviewed_at": "2026-10-23T15:00:00+09:00",
  "ratings": {"grounding": 2, "judgment": 1, "actionability": 1, "scope": 2},
  "notes": "대화 인용은 정확하나 AI가 작성한 내용을 직원의 검증 행동으로 일부 해석함."
}
```

위 조각은 편집 필드 예시다. 기존 사례의 `case_id`, `output_sha256` 등은 유지한다.
검토 점수는 **평가 Agent 출력의 품질**이며 직원 업무 점수가 아니다.

| 항목 | 0점 | 1점 | 2점 |
| --- | --- | --- | --- |
| 근거 타당성 | 허위·무관한 근거 | 일부 연결이 약함 | 판단과 실제 근거가 연결됨 |
| 판단 타당성 | 핵심 판단이 잘못됨 | 부분적으로 타당함 | 기준·누락 자료·행위 주체를 올바르게 해석함 |
| 코칭 실행 가능성 | 부적절하거나 실행 불가 | 일반적 조언 위주 | 해당 상황에 바로 적용할 행동·요청을 제시함 |
| 평가 범위 준수 | 범위 밖 평가·지시 따름 | 일부 과도한 추정 | 주어진 대화·기준·자료 범위에서 판단함 |

보안 자료가 없으면 위반 여부나 만점을 추정해서는 안 된다.
AI가 생성한 내용을 직원이 검증·수정했다고 간주해서도 안 된다.
생성 실패 사례는 품질 점수를 붙이지 않고 실패율로 별도 보고한다.
미검토 점수는 0이 아니라 미정이다. 검토 내용은 로컬 기록이며 LangSmith 점수로
자동 업로드하지 않는다.

## 4. 검증결과서용 기록 생성과 전후 비교

```bash
python -m ai_agents.cli.quality report outputs/quality/final-v1/experiment.json \
  --reviews outputs/quality/final-v1/reviews.json \
  --output-dir outputs/quality/final-v1-report

# 개선 코드에서 같은 평가셋으로 final-v2를 실행하고 별도로 사람 검토한 후:
python -m ai_agents.cli.quality compare \
  outputs/quality/final-v1/experiment.json outputs/quality/final-v2/experiment.json \
  --before-reviews outputs/quality/final-v1/reviews.json \
  --after-reviews outputs/quality/final-v2/reviews.json \
  --allow-change prompt_version \
  --output-dir outputs/quality/final-v1-v2
```

프롬프트가 그대로면 `--allow-change prompt_version`은 생략한다.
모델이나 배포 설정 변경은 각각 `--allow-change model`, `--allow-change model_settings`로
명시한다. 평가셋이 달라지면 직접 비교를 거부한다.
평가기준을 입력으로 고정한 평가셋의 기준 변경 역시 새 평가셋 실험으로 관리한다.
양쪽에서 모두 검토한 동일 사례만 품질 평균을 비교하며 검토자 변경도 표시한다.

| 기록 | 용도 |
| --- | --- |
| `manifest.json` | 실행 시작 시 코드·설정 기록 |
| `experiment.json` | 완료 상태 포함 전체 결과·최종 메타데이터 |
| `dataset.json`, `results.jsonl`, `report.csv` | 고정 입력·기대 결과·실제 출력·사례별 결과 |
| `traces/*.jsonl` | 로컬 단계별 추적·trace ID |
| `reviews.json` | 실행·출력 해시에 연결된 사람 검토 |
| 리포트 폴더 `report.md`, `summary.json`, `reviews.csv` | 검증결과서에 옮길 요약·검토 증빙 |
| 비교 폴더 `comparison.md`, `comparison.json` | 변경 전후 지표·실험 조건 차이 |

코드 커밋·미커밋 상태·작업 트리 해시, 평가셋·프롬프트·기준 해시, 모델·배포 버전,
실제 호출 설정, 호출 횟수, 사례 처리 시간, 모델이 제공한 토큰 사용량을 기록한다.
사용량이 없으면 `null`로 남긴다. 모델 서버의 배포 버전은 사용자가 정확히 입력해야 한다.
`dirty=true`이면 커밋 이후 같은 입력으로 다시 실행해 최종 증빙을 남기는 것이 좋다.

LangSmith에는 기존 정제 추적 경로로 원문 대신 해시·단계·상태를 보낸다.
전송 요청 여부와 감지한 실패는 최종 `experiment.json`에 기록한다.
실패 미감지가 원격 저장 성공을 보증하지 않으므로 trace ID로 화면에서 확인한다.
로컬 결과에는 가상 입력과 실제 출력이 포함된다. 이 실행기는 합성 평가셋용이다.

`test_transport` 표시는 모델 응답을 대체한 실행기 시험이다. 실제 LLM 품질이나
지연 시간의 증거로 쓰지 않으며 `real_model` 실행과 비교할 수 없다.
자동 생성·검증 성공률과 사람 품질 점수는 항상 구분한다.
`outputs/`는 Git 제외 대상이므로 검증결과서 증빙 폴더는 별도 보관한다.

## 구현 검증 기록 — 2026-10-23

브랜치 `feat/offline-llm-quality-validation`, 기준 커밋 `0094ad8`에 미커밋 변경을
포함한 상태에서 검증했다. 실제 모델·LangSmith 원격 호출은 실행하지 않았다.

| 검증 | 관측 결과 |
| --- | --- |
| Ruff (`src`, `tests`) | 통과 |
| 전체 회귀 테스트 (`tests`, `llm_proxy/tests`) | 870 통과 / 16 건너뜀 |
| DB 테스트 건너뜀 사유 | 테스트용 PostgreSQL 주소 미지정 |
| 실제 SDK + HTTP 응답 대역, 중간 피드백 | 20/20 생성·출력 검증 통과 |
| 실제 SDK + HTTP 응답 대역, 최종 평가 A/B | 각각 8/8 생성·출력 검증 통과 |
| A/B 품질 비교 | 사람 검토 0건, 모든 품질 평균 미정 |
| MCP 호출 | 금지한 상태에서 실행 통과 |

`tests/test_llm_quality.py`에서 출력 오류 재생성, API 오류, 토큰 누락, 기대 문장
미전달, 리뷰 해시 연결, 다른 평가셋·실행 모드 비교 거부, LangSmith 전송 실패
기록, 취소 전파를 검증했다. 대역 응답은 품질 정답으로 사용하지 않는다.

로컬 증빙 위치:

- `outputs/validation/offline-quality/pytest.xml`
- `outputs/validation/offline-quality/smoke-20261023T044824/feedback-report/`
- `outputs/validation/offline-quality/smoke-20261023T044824/final-a-report/`
- `outputs/validation/offline-quality/smoke-20261023T044824/final-b-report/`
- `outputs/validation/offline-quality/smoke-20261023T044824/comparison-report/`

두 최종 실행은 같은 코드의 실행기 비교 시험이다. 모델 개선 효과를 뜻하지 않는다.
검증결과서에는 현재 결과를 기능 검증으로 기재하고, 실제 모델 품질 결과는 모델
실행 및 사람 검토 후 별도 기재한다.
