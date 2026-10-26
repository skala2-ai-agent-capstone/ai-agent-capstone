# MCP 응답 검증기 재사용 성능 개선

2026-10-26, 기반 커밋 `1c2dedb`의 최종 평가 자료 수집을 측정했다.
앞선 [MCP 읽기 병렬화](agent_performance.md)가 적용된 상태와 비교한 추가 개선이다.
이번 변경은 `integrations/mcp_adapter.py`의 고정된 Pydantic 검증기 3개를 재사용한다.
기준 생성 로직·평가 프롬프트·모델·MCP 호출·큐 계약·검증 규칙은 바꾸지 않았다.

## 발견한 병목

`decode_mcp_result`와 `parse_work_case_response`가 호출될 때마다 같은
`TypeAdapter`를 생성하고 재귀 `JsonValue` 스키마를 다시 구성했다.
가상 40건을 5회 수집한 프로파일에서는 검증기 초기화가 3,320회 발생했다.
수정 후에는 모듈 로딩 때 3회 구성하며, 실제 입력 검증은 이전과 같은 3,320회 수행했다.

재사용 대상은 JSON 객체, 산출물 참조 목록, 고객 첨부 메타데이터 목록의 **검증 스키마**다.
업무 데이터나 응답 결과를 저장하는 캐시는 아니다. 요청마다 입력을 다시 검사하고,
다른 실행·스냅샷·버전을 거절하는 검사와 원래 오류 처리를 유지한다.

## 비교 결과

같은 40개 가상 업무를 변경 전후 실행했다. 사례별 반복 측정 중앙값을 합산했다.
프로파일러를 켠 실행 시간은 아래 수치에 섞지 않았다.

| 조건 | 반복/사례 | 변경 전 합계 | 변경 후 합계 | 감소 |
| --- | ---: | ---: | ---: | ---: |
| 외부 지연 없는 자료 수집 | 9회 | 130.867ms | 34.721ms | 73.47% |
| 각 도구에 20ms 모의 지연 | 3회 | 7,854.406ms | 7,512.076ms | 4.36% |

두 조건 모두 40건의 수집 결과 해시, 도구별 호출 횟수, 최대 동시 호출 수가 변경 전후 같았다.
40건을 한 번 실행할 때 도구 호출 수는 504회로 동일하다.
자료·근거를 생략하거나 검증을 건너뛰어서 얻은 개선이 아니다.

측정 범위는 근거·자료 수집 함수이며 모듈 import와 fixture 생성 시간은 포함하지 않는다.
개발 머신에서 전후 순차 측정했으므로 스케줄링·머신 부하의 영향이 있을 수 있다.
특히 모의 지연 조건의 작은 차이를 운영 서버 지연 개선율로 일반화하지 않는다.
**실제 LLM 응답 시간, 서버 전체 처리량·p50/p95, 평가 품질·비용은 이번에 측정하지 않았다.**

## 검증

- 전체 Agent·프록시 테스트: **1,087 통과 / 22 건너뜀**, 실패 0.
- 관련 응답·병렬 수집 테스트: **139 통과**.
- 잘못된 입력을 연속 호출해도 검증 실패를 유지하는지 확인했다.
- 한 호출의 반환 객체를 수정해도 다음 호출에 섞이지 않는지 확인했다.
- 첨부 목록의 누락·빈 목록·값 있는 목록과 기존 공백 정규화 동작을 확인했다.
- 변경한 Python 파일의 Ruff 검사와 `git diff --check` 통과.

건너뛴 항목은 테스트 DB 등 실행 환경이 필요한 검사이며 통과에 포함하지 않는다.
실제 서버 배포·유료 모델 호출·LangSmith 원격 전송은 하지 않았다.

## 재현과 증빙

저장소 루트에서 설치된 가상환경으로 실행한다. 기존 출력 파일은 덮어쓰지 않는다.

```bash
python -m pytest tests/test_mcp_adapter.py tests/test_parallel_reads.py -q

python tests/benchmark_evaluation_collection.py \
  --delay-ms 0 --repeats 9 --output outputs/performance/new-no-delay.json

python tests/benchmark_evaluation_collection.py \
  --delay-ms 20 --repeats 3 --output outputs/performance/new-20ms.json
```

변경 전후는 같은 의존성·데이터·측정 조건으로 실행하고 건별 해시와 호출 수를 함께 비교한다.
이번 원시 증빙은 로컬 `outputs/performance/response-20261026/`에 보관했다.

| 파일 | 내용 |
| --- | --- |
| `comparison.json` | 커밋·미커밋 상태·환경·소스 해시·전후 결과·검증 건수 |
| `before-no-delay.json`, `after-no-delay.json` | 지연 없는 40건 비교 |
| `before-20ms.json`, `after-20ms.json` | 모의 지연 40건 비교 |
| `before-cpu.json`, `after-cpu.json`, `before.prof`, `after.prof` | 별도 프로파일 실행과 초기화·검증 횟수 |
| `source-before/`, `source-after/`, `performance.patch` | 비교한 코드와 측정기 기록 |
| `pytest.xml` | 전체 테스트 결과 |

`outputs/`는 Git 제외 대상이다. 이 문서를 올려도 원시 증빙은 자동으로 업로드되지 않는다.
측정 당시 미커밋 변경을 포함했으므로 재현 시 `comparison.json`의 소스 해시와 변경 패치를 함께 확인한다.
README 정리와 성능 코드 변경은 별도 커밋으로 관리한다.
