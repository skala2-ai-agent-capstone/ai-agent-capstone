# 팀 업무 평가·코칭 Agent

브랜치: `feat/team-evaluation`. 개인 중간 피드백·최종 평가와 같은 패키지의
`evaluation_agent/team/`에 별도 실행 경로를 추가했다. 기준 생성 로직은 변경하지 않는다.

## 동작과 범위

```text
백엔드: 관리자 권한 확인 → 확정 평가의 업무별 최신 버전 선택
       → 기간·업무 유형·평가 기준 버전별 집계 → 공개 가능 여부 검사
MCP:   get_team_evaluation_snapshot
Agent: 입력 검증 → 항목 평균·관측 비율 계산 → LLM 근거/코칭 선택
       → 선택 검증 → 팀 리포트 구성 → 로컬 저장
MCP:   save_team_evaluation_report (서버 구현 후 연결)
백엔드: 팀 리포트 조회 권한 확인 → 관리자 화면
```

Agent는 개인 대화, 이름, 이메일, 개인 평가 ID, 원문 코칭을 받지 않는다.
원본 개인 평가를 익명화하거나 합산하는 작업은 정본 데이터를 가진 백엔드 책임이다.
Agent는 서버 집계의 산술·범위 일관성을 검증하고 평균·관측 비율을 계산한다.
실제 원본과 구성원 수의 일치, 확정 여부, 관리자 권한을 독립적으로 증명하지는 못한다.

현재 구현:

- 입력/출력 계약, 집계 지표 계산, LLM 코칭 선택, 결과 검증, 로컬 CLI.
- 팀 전용 MCP 클라이언트 허용 목록과 조회/저장 orchestration.
- 가상 평가셋 8건, 기존 품질 실행기·사람 검토·LangSmith 정제 추적 연결.

아직 서버에 없는 부분:

- 아래 두 MCP 도구의 서버 구현, 백엔드 집계/공개 정책 및 관리자 조회 API.
- 실제 요청 발행·인증 토큰 공급·팀 Worker 배포와 화면 연결.
- GCP의 실제 모델 품질·지연·MCP 저장 검증.

## 점수·공개 정책

동일 업무 유형·동일 기준 버전·동일 기준 해시를 한 그룹으로 취급한다.
점수 범위는 기존 평가와 동일한 0~2다. 보안 준수는 0/2만 가능하다.
각 항목은 합계 점수, 관측 평가 건수, 관측 구성원 수로 표현한다.

- 평균 = 점수 합계 / 관측 평가 건수. 평균은 3자리, 관측 비율은 소수 1자리 표시.
- 관측 비율 = 관측 평가 건수 / 같은 그룹 확정 평가 건수 × 100.
- `statistics: null`은 비공개 또는 공개할 관측 자료 없음. 원인·소수 인원 수는 싣지 않는다.
- 자료가 없으면 0점으로 만들지 않는다. 공개할 지표가 없으면 LLM을 호출하지 않는다.
- 다른 업무·기준 버전 간 총점, 순위, 개선 추세는 생성하지 않는다.
- 이 평균은 평가 건수 가중이다. 업무가 많은 구성원이 더 반영될 수 있어 개인별 동일
  가중 평균이나 팀 전원의 역량으로 해석할 수 없다.

**개발 기본 공개 기준은 서로 다른 구성원 5명 이상**이다. 서버의 `min_members`는
5 이상으로 강화할 수 있고 Agent는 그룹·항목별 관측 구성원 모두에 적용한다.
구성원 수가 적은 그룹은 서버가 전체를 제외하고, 작은 항목은 통계 자체를 null로 보낸다.
`release_approved=false`이면 `groups=[]`이어야 하며 결과는 `withheld`다.
공개 승인됐지만 지표가 없으면 `insufficient_data`다.

5명 기준은 팀과 합의할 임시 제품 설정이며 익명성을 보장하는 인증이 아니다.
반복 기간 조회의 차이, 전체/하위 그룹의 차감, 특정인의 업무 집중, 극단적인 값으로
개인 정보를 추정할 수 있는 집계는 백엔드에서 함께 제한해야 한다. 필요한 보완 억제를
적용한 후에만 공개 승인하며 Agent는 제외된 수치를 복원하지 않는다.

## LLM의 역할

LLM에는 공개된 수치, 업무 유형, 항목, 임시 근거 ID와 고정 코칭 목록만 전달한다.
팀/요청/스냅샷 UUID, 기간, 구성원 수, 기준 자유 문구는 전달하지 않는다.

LLM은 강점 근거 최대 3개, 개선 근거와 해당 항목의 활동 최대 3개를 선택한다.
숫자와 근거 설명은 코드로 렌더링하여 임의 점수·개인 이름·근거 없는 자유 문장을
리포트에 추가할 수 없게 했다. 코칭 문구는 5개 항목별 2개씩, 총 10개다.
따라서 현재 버전은 자유 서술형 분석이 아닌 **집계 근거에 따른 활동 선택 방식**이다.
실제 품질 평가에서는 선택의 유용성·우선순위·코칭 목록의 충분성을 검토한다.

강점 후보는 평균 1.5/2 이상, 개선 후보는 미만이다. 이 경계는 코칭용 임시 기준으로
팀원의 개인 평가 기준을 바꾸지 않는다. 해당 band가 있으면 최소 한 항목을 선택하며,
형식·ID·band·활동 항목이 잘못되면 1회 재생성 후 실패 처리한다.
추후 경계나 활동을 바꾸면 코드·목록 해시를 기록하고 동일 평가셋으로 비교한다.

## 백엔드 없이 실행

```bash
source .venv/bin/activate
python -m ai_agents.cli.team_evaluation check --snapshot samples/team/snapshot.json
python -m ai_agents.cli.team_evaluation schemas
```

`check`, `schemas`는 네트워크 호출이 없다. 루트 `.env`에 실제 모델 정보를 설정한다.

```dotenv
TEAM_LLM_BASE_URL=http://YOUR_MODEL_HOST:8000/v1
TEAM_LLM_MODEL=YOUR_DEPLOYED_MODEL
TEAM_LLM_API_KEY=
TEAM_MODEL_REVISION=
LANGSMITH_API_KEY=
LANGSMITH_PROJECT=ai-agent-quality
```

```bash
python -m ai_agents.cli.team_evaluation run \
  --snapshot samples/team/snapshot.json \
  --output-dir outputs/team/baseline --langsmith
```

이 명령은 모델만 호출하며 MCP/DB에 저장하지 않는다. LangSmith 없이 실행하려면
`--langsmith`를 뺀다. OpenAI 호환 Chat Completions의 JSON mode가 필요하다.
기존 프록시 호환을 위해 `X-Stage: evaluate`를 쓰며 work/user ID를 보내지 않는다.
원문 프록시 로그의 접근 권한은 별도 관리한다. LangSmith에는 정제 추적만 보낸다.

결과 폴더: `snapshot.json`, `report.json`, `save-request.json`, `manifest.json`, `traces/`.
기존 폴더는 덮어쓰지 않는다. 저장용 요청 파일이 있어도 서버 저장 완료를 뜻하지 않는다.
공개 지표가 전혀 없으면 모델 설정 없이도 보류 결과를 생성할 수 있다.

### 실제 모델 품질 평가 8건

기존 `QUALITY_LLM_*` 설정을 사용하며 `TEAM_LLM_*`와 별개다.

```bash
python -m ai_agents.cli.quality check --dataset samples/team/quality-v1.json
python -m ai_agents.cli.quality run \
  --dataset samples/team/quality-v1.json \
  --output-dir outputs/quality/team-v1 --label team-baseline --langsmith
python -m ai_agents.cli.quality report outputs/quality/team-v1/experiment.json \
  --reviews outputs/quality/team-v1/reviews.json \
  --output-dir outputs/quality/team-v1-report
```

평가셋은 혼합/강점만/개선만/공개 제한/관측 없음/일부 누락/서로 다른 업무와 기준/강화된
공개 기준의 8건이다. 이 중 2건은 보류하여 모델 호출이 0회다. 자동 성공 8건은
모델 평가 8건을 뜻하지 않는다. 미호출 2건은 `reviews.json`에서 `not_applicable`이며
LLM 품질 점수 대상에서 제외한다. 나머지 6건의 선택 품질을 사람이 검토한다.
사람 검토·전후 비교는 [품질 실행기 안내](offline_llm_quality.md)를 따른다.

## MCP·백엔드 담당자에게 전달할 계약

**새 도구 제안이며 기존 MCP 서버에 구현돼 있다는 뜻이 아니다.** 정확한 JSON Schema는
`python -m ai_agents.cli.team_evaluation schemas`로 출력한다.

| 도구 | 요청 | 응답 data |
| --- | --- | --- |
| `get_team_evaluation_snapshot` | `request_id` UUID 하나 | `TeamSnapshot` |
| `save_team_evaluation_report` | `TeamSaveRequest` | `TeamSaveReceipt` |

MCP 공통 응답은 `ok`, `data`, `meta.run_id`, `meta.snapshot`이다.
조회와 저장에서 run/snapshot 문맥이 바뀌면 클라이언트는 거부한다.

백엔드가 할 일:

1. 인증된 관리자와 요청의 회사·팀·기간 권한을 결합하고 고정 request/snapshot UUID 발급.
2. `[period_start, period_end)`의 업무 최종 완료 시각을 기준으로, 업무별 최신 확정 평가
   하나만 선택. 기준 매핑과 확정 상태는 서버 정본 사용. 기간의 시간대도 서버에서 고정.
3. 업무 유형·rubric 버전·내용 해시별로 그룹화. 같은 업무는 중복 집계 금지.
   항목이 관측되고 점수가 유효한 확정 평가만 `score_sum/scored_evaluations`에 포함.
   미관측·검토 필요 항목을 합계 분모에 넣지 않고 구성원 수는 distinct로 산출.
4. 기존 기준 원본 ID를 계약의 5개 공통 항목에 명시적으로 매핑. 호환되지 않는 기준은
   기존 기준과 섞지 말고 오류/미지원으로 처리. 이 매핑을 LLM에게 맡기지 않음.
5. 공개 정책을 적용한 집계만 반환. 권한 실패는 오류로 반환하고 데이터가 없는 것으로 위장하지 않음.
6. 저장 시 request/snapshot/팀 권한, 서버가 고정한 집계 해시, 리포트 내용을 재검증.
   `(request_id, snapshot_id)`는 논리 실행 하나이며 같은 중복 방지 키와 같은 payload는
   기존 receipt를 반환. 같은 키에 다른 payload는 충돌로 거부. 재생성은 새 요청 ID 사용.
7. 관리자에게 팀 리포트만 제공하고 개인 상세 리포트 조회 권한을 추가하지 않음.

Agent 연동 API:

```python
async with TeamMCPGateway(team_scoped_token, business_url) as gateway:
    workflow = TeamEvaluationWorkflow(gateway, TeamAgent(client, model))
    snapshot, report = await workflow.prepare(request_id)
    # 호출자가 두 객체를 원자적으로 영속화한 뒤 저장한다.
    receipt = await workflow.save(snapshot, report)
    # 응답 유실 시 영속화한 동일 객체로 save만 재호출한다. LLM 재생성 금지.
```

위 코드는 연결 예시다. 토큰 발급/영속화/큐 Worker는 배포 애플리케이션에서 연결해야 한다.
팀 gateway는 두 도구만 허용하며 개인 평가·대화 조회와 기준 생성·저장 도구는 막는다.
읽기와 저장은 분리해 호출자가 실패한 저장만 재시도할 수 있게 했다.
로컬 파일을 다시 읽어 저장 인자만 만들려면 다음을 사용한다(네트워크 호출 없음).

```bash
python -m ai_agents.cli.team_evaluation save-request \
  --snapshot outputs/team/baseline/snapshot.json \
  --report outputs/team/baseline/report.json \
  --output outputs/team/baseline/save-retry.json
```

서버 조회·저장과 팀 기준 집계가 확정되기 전에는 제안 계약을 실제 DB 구조라고 설명하지 않는다.

## 구현 검증 기록 — 2026-10-23

기준 커밋 `64d8117`에 브랜치의 미커밋 변경을 포함하여 검증했다.

| 검사 | 결과 |
| --- | --- |
| Ruff (`src`, `tests`) | 통과 |
| 전체 Agent·프록시 회귀 검사 | 908 통과 / 16 건너뜀 |
| 팀 기능 테스트 | 38개, 위 전체 결과에 포함 |
| 건너뜀 사유 | 테스트용 PostgreSQL 주소 미지정 |
| 팀 평가셋 실행 A/B | 각각 8/8 기대 실행 성공, 모델 응답 대역 호출 6회 |
| 자료 부족/공개 제한 | 각각 1건, 모델 미호출·품질 점수 대상 제외 |
| 사람 품질 점수 | 미검토, 모델 개선 효과 주장 없음 |

실제 OpenAI SDK의 HTTP 전송을 대역으로 교체하여 요청 형식, 선택 검증, 재생성,
API 오류 분류, 취소, 개인 필드/소수 인원 거부, 버전 분리, 저장 재시도와 결과 변조 거부를
검증했다. MCP 도구는 모의 gateway이며 실제 서버·모델·LangSmith 원격 호출은 하지 않았다.

로컬 증빙(Git 제외):

- `outputs/validation/team-evaluation/pytest.xml`
- `outputs/validation/team-evaluation/smoke-20261023T051823/team-a-report/`
- `outputs/validation/team-evaluation/smoke-20261023T051823/team-b-report/`
- `outputs/validation/team-evaluation/smoke-20261023T051823/comparison-report/`

이 기록은 `test_transport` 실행기 검증이다. 실제 모델 코칭 품질과 지연 시간은
GCP 모델에 연결한 뒤 같은 평가셋을 실행하고 사람 검토를 붙여 별도 기록한다.
