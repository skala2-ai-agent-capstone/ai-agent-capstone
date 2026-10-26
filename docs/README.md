# 문서 목록

처음 실행할 때는 [프로젝트 README](../README.md)에서 설치와 기능별 명령을 확인합니다.
문서에 적힌 과거 브랜치·커밋·테스트 건수는 해당 시점의 기록이며 현재 배포 상태를 뜻하지 않습니다.

## 기능별 구현·계약

| 목적 | 문서 |
| --- | --- |
| 중간 피드백 요청·최근 최대 5쌍·MCP 저장 | [recent_five_feedback.md](recent_five_feedback.md) |
| 저장 기준 기반 개인 최종 평가 | [evaluation_agent.md](evaluation_agent.md) |
| 현재 백엔드 최종 큐·토큰 공급·RESUME 경계 | [backend_worker_alignment.md](backend_worker_alignment.md) |
| 최종 Worker의 두 큐 계약·재시도 | [final_evaluation_worker.md](final_evaluation_worker.md) |
| 최종 페이지 데이터 구성 | [final_evaluation_page.md](final_evaluation_page.md) |
| 전체 리포트 저장·조회 계약 제안 | [mcp_final_report_request.md](mcp_final_report_request.md) |
| 팀 집계·팀 코칭·큐 Worker | [team_evaluation.md](team_evaluation.md) |
| 내규 업로드 후 기준 초안 생성 Worker | [rubric_generation_worker.md](rubric_generation_worker.md) |
| 사용자가 선택한 업무 유형 연결 | [user_selected_work_type.md](user_selected_work_type.md) |
| MCP 조회·승인 그래프 실행 | [mcp_agent_integration.md](mcp_agent_integration.md) |

## 실행 환경·데이터

- [서버 Compose 배포](deploy_server.md)
- [MCP 서버·DB 안내](../mcp_servers/README.md)
- [LLM 기록 프록시](../llm_proxy/README.md)
- [LLM 호출 기록 데이터 명세](llm_exchanges_data_spec.md)
- [DB 관계도](erd_single_db.mmd)
- [MCP 내부 API 스냅샷](mcp_openapi.yaml) / [HTML 스냅샷](mcp_swagger.html)

API 스냅샷과 실제 서비스의 최신 Schema가 다를 수 있습니다. 연동 대상 커밋의 코드와
실행 중인 서버의 Schema를 대조하고, 개인 개발 환경의 주소를 공통 배포 주소로 간주하지 않습니다.

## 추적·검증

- [실행 계약·로컬 추적·LangSmith](execution_contracts_observability.md)
- [MCP 없는 실제 LLM 품질 평가·사람 검토·비교](offline_llm_quality.md)
- [최종 평가 고정 검증셋](final_evaluation_validation.md)
- [중간 피드백 검증 절차](recent_five_feedback_verification.md)
- [MCP 연동 검증 기록](mcp_live_test_results.md)
- [자료 수집 성능 비교 기록](agent_performance.md)

실행기·평가셋·문서는 Git으로 관리하지만 `outputs/` 아래 원시 실행 결과·리포트는
`.gitignore`에 따라 제외됩니다. 검증결과서의 수치는 실행 환경·버전·모의/실제 호출 여부를 함께 기록합니다.

## 과거 기록

- [work_classifier → ai_agents 패키지 이동 당시 기록](agent_package_layout.md)
- [과거 분류 실험과 초기 README](archive/classification_readme_20261026.md)

과거 분류 코드와 데이터는 재현용으로 유지합니다. 현재 업무 흐름의 선행 단계로 실행하지 않습니다.
