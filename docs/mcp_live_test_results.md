# 실제 MCP 연결 테스트 결과 — 2026-10-21

결과: **업무 조회·분류·승인 범위의 실제 조회·분류·기준 생성·승인 대기까지 통과**했습니다.
평가 모듈의 실제 채점·코칭은 연결하지 않았습니다. 생성된 기준의 담당자 승인도 대신하지 않았습니다.

## 실행 환경

- PostgreSQL 16 / ChromaDB 0.6.3 / business-mcp·rag-mcp Docker 컨테이너
- 저장소의 가상 데이터만 새 볼륨에 적재: 업무 40건, 버전별 평가 건 43개
- MCP SDK 2.3.0 / LangGraph 1.2.16 / `.env`에 설정된 OpenAI `gpt-4o-mini`
- 기준 내규: RAG 시드의 `영업 업무 생성형 AI 활용 지침 (가상 내규)`
- Business/RAG 서버 모두 최종 health 상태 정상

## 확인 결과

| 검사 | 결과 |
| --- | --- |
| 기존 자동 테스트 | 184개 통과 |
| Ruff 검사 | 통과 |
| 실제 `mcp_cli fetch` | RFP 대표 업무 조회·JSON 출력 성공 |
| 실제 전체 평가 건 조회 | 43/43 통과 — 스냅샷별 업무·대화·산출물·첨부 조회 및 내규 조회 |
| 실제 LLM 실행 | 대표 4종 4/4 통과 — 아래 표 참고 |
| 일시 오류 | 주입된 오류 발생 후 재시도 복구 확인. Business 감사 로그에 TEMPORARY_FAILURE 3건 기록 |
| 보안 준수 기준 점수 | 생성된 4종 모두 0·2점만 포함 |
| 승인 전 평가 모듈 호출 방지 | 실제 4종 모두 awaiting_approval, handoff 없음 |
| 평가 결과 DB 쓰기 | 평가 초안 0건 / 코칭 0건 / 검토 요청 0건 |

| 실제 LLM 테스트 입력 | 분류 결과 | 생성 기준 | 최종 상태 |
| --- | --- | --- | --- |
| EVAL-E2E-RFP-01-v2 | rfp_analysis | 5개 | awaiting_approval |
| EVAL-E2E-PR-01-v2 | proposal | 5개 | awaiting_approval |
| EVAL-E2E-QT-01-v2 | quotation | 5개 | awaiting_approval |
| EVAL-E2E-IQ-01-v2 | customer_response | 5개 | awaiting_approval |

업무 유형 정답은 분류 입력에 넣지 않았으며 분류 후 비교에만 사용했습니다.
견적 대표 업무는 실제 `compute_reference_quote`와 승인 요청 조회도 수행했습니다.
내규 출처·인용과 구조 검증은 통과했지만, 생성된 기준의 내용이 적절한지에 대한 최종 판단은 담당자가 해야 합니다.

## 검증 범위의 한계

- **43건 통과는 MCP 조회 테스트**입니다. 40개 업무 전체를 LLM으로 다시 분류한 정확도 측정이 아닙니다.
- 실제 LLM 테스트는 업무 유형별 대표 1건씩 총 4건입니다.
- 실제 생성 기준에 대한 사용자 승인과 평가 입력 생성은 수행하지 않았습니다.
- 승인·수정·취소·승인 후 평가 입력 인계·평가기 주입 흐름은 자동 테스트의 고정 응답으로 검증했습니다.
- 실제 평가·코칭, 재평가·추가 검색, 결과 DB 저장·이의·최종 확정은 이번 검증 대상이 아닙니다.

## 로컬 결과 파일

다음 파일은 `.gitignore`의 `outputs/` 아래에 있으며 커밋되지 않습니다.

- `outputs/mcp/live-test/cli-fetch.json`: 실제 CLI 조회 결과
- `outputs/mcp/live-test/fetch-report.json`: 실제 평가 건 43개 조회 요약
- `outputs/mcp/live-test/llm-report.json`: 실제 LLM 대표 4종 실행 요약
- `outputs/mcp/live-test/EVAL-E2E-*-01-v2-pending.json`: 각 업무의 분류·평가기준 전체
- `outputs/mcp/live-test/run_live.py`: 이번 로컬 검증 실행 스크립트

`pending.json`은 검토용 실행 결과입니다. InMemorySaver의 실행 중 체크포인트 파일이 아니므로,
프로세스가 종료된 뒤 이 JSON만으로 그래프를 재개할 수는 없습니다. 직접 승인 흐름을 확인하려면 아래 CLI를 다시 실행합니다.

```bash
source .venv/bin/activate
python -m work_classifier.mcp_cli run \
  --evaluation-id EVAL-E2E-RFP-01-v2 \
  --output outputs/mcp/manual-rfp-handoff.json
```

이번 테스트에서 서버용 `mcp_servers/.env`를 생성하고, 루트 `.env`에 일치하는 `INTERNAL_API_KEY`를 추가했습니다.
기존 OpenAI 키와 모델은 유지했습니다. 두 `.env`와 결과 JSON은 Git 제외 대상입니다.
서버 컨테이너와 테스트 데이터 볼륨은 다음 수동 테스트에 사용할 수 있도록 유지했습니다.

## 병합 판단

이번 조회·분류·승인 기능을 명시한 브랜치 커밋과 PR 검토를 진행할 수 있습니다.
앞서 main 병합 보류 사유였던 실제 MCP·LLM 연결 미검증은 위 범위에서 해소됐습니다.
PR에서는 실제 사용자 승인·평가까지 완료된 전체 서비스로 표현하지 않고,
조회·분류·기준 생성·승인 제어·평가 모듈 연결 인터페이스의 구현으로 범위를 명시합니다.
