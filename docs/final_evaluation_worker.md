# 최종 평가 작업 수신·재시도

2026-10-23 추가: 기존 `agent-v1` 계약을 유지하면서 `--contract backend-v1`로
`evaluation.*` 큐의 `EVALUATE`를 받을 수 있습니다. `--token-file`도 지원합니다.
완료 큐 소비자·RESUME 분리 등 서버 선행 작업은
[백엔드 연동 계약](backend_worker_alignment.md)을 먼저 확인하세요.

초기 구현 브랜치: `feat/final-evaluation-worker`. 당시 기반: `2b14be1`.
평가 기준 생성 로직을 변경하지 않고 기존 최종 평가 워크플로를 실행한다.
현재 백엔드는 `backend-v1` 계약을 사용한다. 아래 `agent-v1` 예시는 독립 계약 설명이며,
현재 백엔드에 연결할 때는 다음처럼 평가별 토큰 파일과 계약을 지정한다.

```bash
python -m ai_agents.cli.final_worker worker --contract backend-v1 \
  --token-file /path/to/final-tokens.json
```

토큰 파일은 백엔드가 발급·갱신한다. 해당 형식과 `evaluation.*` 큐는
[백엔드 연결 문서](backend_worker_alignment.md)를 참고한다.

## 독립 agent-v1 계약의 흐름

1. 백엔드가 사용자 본인 권한을 확인하고 평가 건과 고정 스냅샷을 만든다.
2. 신뢰하는 백엔드 producer가 `final_evaluation.requests`에 ID만 발행한다.
3. worker의 `token_provider(event)`가 이 평가 건의 evaluation 토큰을 공급한다.
4. 기존 `FinalEvaluationWorkflow`가 MCP 조회 → 평가 → 초안·코칭 저장을 수행한다.
5. worker가 `final_evaluation.results`에 저장 완료 알림을 발행하고 confirm을 기다린다.
6. 추적 기록과 발행이 성공하면 원본 요청을 ack한다.
7. 백엔드가 알림을 중복 제거해 처리하고 사용자 조회 API 상태에 반영한다.

Agent는 DB나 백엔드 내부 API를 직접 호출하지 않는다. token provider는 배포 애플리케이션이
주입하는 신뢰하는 인증 어댑터이며 모델이 선택하는 도구가 아니다.
현재 제공하는 `--token-file` 어댑터는 백엔드가 만든 토큰 파일을 읽는다.
아래 사용자 정의 `--token-provider` 방식은 별도 통합 환경을 위한 선택지다.

## 요청과 결과

```json
{
  "schema_version": "1",
  "type": "FINAL_EVALUATE",
  "message_id": "final-request-001",
  "evaluation_id": "EVAL-001",
  "operation_id": "final-v1",
  "attempt": 0
}
```

`operation_id`는 같은 평가 실행과 모든 재시도에서 유지한다. 토큰, company/user 권한 정보,
대화, 산출물, 기준 원문은 이벤트에 넣지 않는다. 권한과 데이터 범위는 MCP가 검증한다.
완료 알림에는 evaluation/operation ID, 안정적인 message ID, 두 저장 리비전,
`review_required`, `status=draft_and_coaching_saved`만 포함한다.
이 알림은 **평가 최종 확정이 아니다**. 본인 확인·검토·확정·개인 조회 권한은 백엔드가 담당한다.

```bash
python -m ai_agents.cli.final_worker schemas
python -m ai_agents.cli.final_worker worker \
  --token-provider your_backend_adapter.tokens:resolve_final_token \
  --checkpoint-dir /persistent-volume/final-checkpoints \
  --trace-dir /persistent-volume/final-traces
```

`your_backend_adapter`는 실제 패키지가 아닌 연동 자리표시자다. 함수 형태는 다음과 같다.

```python
async def resolve_final_token(event: FinalEvaluationEvent) -> SecretStr:
    # 백엔드가 인증한 작업과 동일한 평가/스냅샷 범위의 토큰을 반환한다.
    # 재전달·만료 시에도 스냅샷은 유지해야 한다. 원문이나 토큰을 로그에 출력하지 않는다.
    ...
```

`FINAL_LLM_MODEL`, `FINAL_LLM_BASE_URL`, `FINAL_LLM_API_KEY`, `FINAL_BUSINESS_MCP_URL`,
`FINAL_RAG_MCP_URL`은 기존 최종 평가 설정과 같다. `FINAL_AMQP_URL`을 추가한다.
worker에서는 단일 `FINAL_MCP_TOKEN`을 모든 평가에 공유하지 않는다.
의존성은 기존 worker extra (`uv sync --extra worker`)에 포함된다.
`--langsmith`를 명시하면 기존 정제 추적 경로를 사용한다. 인증 토큰·업무 원문은 추적하지 않는다.

## 큐·재전달 계약

| 큐 | 운영 설정/처리 주체 |
| --- | --- |
| `final_evaluation.requests` | durable 요청 큐, 신뢰하는 백엔드만 발행 |
| `final_evaluation.retry` | durable 지연 재시도 큐. TTL 후 요청 큐로 dead-letter하도록 운영에서 설정 |
| `final_evaluation.dlq` | 영구 실패/재시도 소진. 백엔드가 읽어 실패 상태·수동 복구에 사용 |
| `final_evaluation.results` | durable 저장 완료 알림. 백엔드가 message ID를 unique 키로 중복 제거 |

worker는 큐를 새로 선언하거나 TTL/권한을 바꾸지 않는다. 큐가 없으면 시작에 실패한다.
기본 재시도는 최초 실행 + 최대 3회다. MCP 연결/전송 실패, 모델 연결 실패·HTTP 408/429/5xx만
자동 재시도한다. 권한/버전/검증 오류, 원인이 분류되지 않은 오류는 DLQ로 보낸다.
MCP gateway가 retryable 플래그를 보존하지 않는 서버 오류는 gateway 내부 재시도 이후
worker가 추가 재시도하지 않는다. 부정확한 최신 버전으로 대체하지 않는다.

브로커 발행 실패 시 원본은 ack하지 않고 재전달된다. 이 경우의 전달 횟수는 업무 attempt와
별개이며 브로커 delivery-limit/운영 경보가 필요하다. 작업 취소/프로세스 종료 역시 ack하지 않는다.
동일 체크포인트 볼륨에서 다시 받으면 이미 저장한 초안/코칭을 재생성하지 않는다.
완료 알림은 중복 발행될 수 있으므로 exactly-once 전송으로 간주하면 안 된다.

다중 호스트의 동일 평가 동시 실행 방지는 백엔드 lease/단일 소유자 라우팅이 필요하다.
체크포인트가 다른 호스트로 이동하지 않으면 재전달을 안전하게 이어갈 수 없으므로
영속 볼륨과 라우팅을 함께 구성해야 한다. Agent의 로컬 flock만으로 분산 잠금을 보장하지 않는다.

## 검증과 커밋 체크포인트

`tests/test_final_worker.py`: 실제 최종 평가 워크플로 + 모의 MCP/모델로 저장 후 알림 실패와
재전달 시 평가 1회·저장 1회씩을 확인한다. 추가로 재시도 한도, 잘못된 입력의 원문 미전파,
취소, 잘못된 저장 결과 거부, 작업별 토큰 주입, 브로커 confirm/ack 순서를 검증한다.
RabbitMQ 전송 객체는 대역이며 실제 브로커·백엔드 인증·AWS 모델은 이 검사에 포함되지 않는다.

커밋 메시지: `feat: 최종 평가 작업 수신과 저장 완료 알림 및 재시도 처리 구현`
다른 worktree의 변경을 포함하지 말고 이 브랜치에서 커밋한다.
