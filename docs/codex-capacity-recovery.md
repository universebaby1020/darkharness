# Codex native capacity 실패의 명시적 Controller 회복

`integration.capacity.recover`는 원래 모델이 native `serverOverloaded`로 실패했고
해당 attempt에 native 도구·업무 효과가 관측되지 않은 경우만 다룬다. ACK된 SDK
상태 보고는 실제 외부 효과로 별도 검증·보존한다. 이 명시적 action 자체는
자동 재시도, 모델 변경, provider 변경, timeout 권한 확대를 제공하지 않는다.
WO08에서 승인한 별도 run-scoped `settled_provider_recovery`만 사전 등록된
정책과 인증된 dispatch receipt 아래에서 자동 재시도한다(`wo08-recovery.md`).
현재 SDK 자동 텔레메트리는 로컬 best effort로 억제하며, 보고가 없어도 native
terminal/프로세스/전체 RPC/효과 원장과 token delta 검증은 유지한다. 작업 acceptance는
회복 후에도 `NOT_EVALUATED`다.

Controller gateway 요청은 기존 envelope의 `operation_id`에 정확한 실패 작업을,
payload에는 `{ "attempt": "<failed attempt>", "grant_id": "<active grant>" }`만
넣는다. 같은 활성 Grant의 `scope.continuation.operations`에 그 작업이 직접 포함되어야
한다. `safe`, 임의 receipt, 외부 evidence 참조, 새 모델·입력 등은 받지 않는다.
Seat 연결은 이 action을 호출할 수 없다.

검증은 다음의 실제 owner 기록을 함께 사용한다.

- SDK 4.0.0 `band.adapters.codex`의 기존 source hash pin 및 보고 형식·token usage·failure
  변환을 소유하는 `band.integrations.codex.types`, `band.core.protocols`의 source hash pin.
- 정확한 operation/attempt와 owned run/room/seat/thread, native client와 process group.
- `turn/start` 요청·응답, `TURN_ACCEPTED`, native `error`의 `willRetry=false`,
  같은 turn의 `turn/completed(status=failed)`와 정확한 capacity error 객체.
- SDK `TURN_OUTCOME`의 failed/error 일치, `settled_reply=false`, `include_reply=false`,
  빈 final text, `TurnResultAlreadyReported`/`FENCED`, 이후 `PROCESS_STOPPED(members=[])`.
- `c_run_settings` hash, seat settings와 `c_owned_thread.binding`, 기존
  `RUNTIME_BINDINGS`의 같은 model/effort/workspace. 재시작의 기존 settings pin도 유지한다.
- 전체 attempt RPC 스트림과 durable callback/effect 기록. native terminal의
  `items=[]`, 특히 `itemsView=notLoaded`만으로 효과 부재를 판단하지 않는다.
- 기존 `Recovery`의 프로세스 종료 확인, 두 번 동일한 canonical source/index readback,
  미정산 outbox/Git/다른 native 작업 차단 및 continuation 직전 source 재검증.

이 좁은 경로는 userMessage 이외의 native item, 모든 server callback,
approval/user-input 요청, 알려지지 않은 RPC, 미응답 요청, 해당 작업의 업무 outbox/Git/
verification/question 효과를 거절한다. 예외는 정확히 재구성 가능한 ACK된 SDK 보고다:
thread resumed, turn started/failed, 누적 token usage, native capacity error. 각 보고는
adapter/send_event counter ID, 본문 artifact hash, 정확한 SDK envelope/type/room/thread/turn,
원래 입력 요약·실제 duration, canonical SEND_INTENT/SEND_ACK와 성공 receipt를 대조한다.
token usage는 같은 native thread의 관측 누적치가 변하지 않은 좁은 경우만 지원하며,
이전 turn의 누적치를 현재 turn의 실행량으로 주장하지 않는다. 보고 본문의 추가·변경,
업무 text send, 임의 ACK, UNKNOWN 전송은 거절한다. 보고 receipt는 기존
`settled_outbox`와 새 `acknowledged_sdk_reports`에 명시적으로 보존한다.
다른 작업의 이미 정산된 효과는 유지하지만
같은 seat의 미정산 checker도 차단한다. 일반 오류·timeout·cancel·STOP/revoke·run UNKNOWN은
capacity 증거가 아니다. 새로운 native schema는 이 경로에서 자동 수용하지 않는다.

검증 성공 시 기존 owner transaction으로 `FAILED/RECONCILED`,
`SETTLED_NATIVE_CAPACITY` 및 hash-backed evidence 참조를 기록하고 기존
`Recovery.observe/resume`으로 원 전체 입력·parent·thread를 보존한 단일 READY child를 만든다.
같은 요청의 재호출은 같은 child를 반환한다. child에서 다시 capacity 오류가 발생하면
부모의 capacity 회복 권한을 상속하지 않으며, 해당 child의 별도 명시적 Controller
바인딩 없이는 이 경로로 재시도하지 못한다. 자동 loop나 retry budget을 추가하지 않는다.

운영 연결 시 기존 owner를 정상 종료하고 새 gateway가 같은 state의 단일 owner가 된 뒤,
좌석을 시작하기 전에 이 action을 호출할 수 있다. 이때 원장의 기존 runtime binding을
사용하며 새 readiness 이벤트는 요구하지 않는다. 좌석이 없으면 child만 READY로 남는다.
그 후 기존 run/config/settings로 시작하면 기존 queue drain이 실행한다. 실행 중 좌석이
있으면 기존 `wake_continuation` 경로를 사용한다. 이 API는 Grant 발급·복구용 DB 수동 수정·
운영 STOP 지우기를 제공하지 않는다. 실제 교체·권한 바인딩·호출은 Controller 운영 원장에
각각 기록해야 한다.

검증 명령: `python -m unittest discover -s tests -p test_integration_capacity.py -v`.
portable component는 SDK pin/process/source를 명시적으로 모형 처리한다. Linux 테스트는
실제 SDK source pin, 임시 Git repository와 `/proc` readback을 사용한다. 둘 다 익명
synthetic native trace이며 실제 provider 응답·실운영 회복 성공의 증거를 대신하지 않는다.
