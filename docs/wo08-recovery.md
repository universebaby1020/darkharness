# WO08 고정 권한 · 증거 기반 복구 계약

후보 구현이다. 실제 Band/공급자 실행을 검증했다는 뜻이 아니다. factory, mandate,
Grant는 dispatch 전에 준비하고 run 종료까지 고정한다. 기존 Controller/owner만
사용하며 새 issuer, Store writer, provider, GUI를 추가하지 않는다.

## B1–B4

- Router 생성 시 기존 repo boundary 검증 후 result_repo와 `.git`의 lstat dev/ino를
  고정한다. legacy non-Git workspace는 부재 자체를 고정한다. 이후 상위 폴더의
  `.git` placeholder는 active를 바꾸지 않는다. repo 교체·symlink, revoke,
  expiry, run_end, seat/room/workspace/repo binding 변경은 여전히 거절한다.
  scope 이유 전이는 owner transaction에 기록한다. Git 효과마다 `_identity`의
  중첩 repo/symlink 검사는 그대로 수행한다.
- SDK 자동 emit/lifecycle/diff/token 보고는 끈다. direct SDK send_event/send_failure는
  로컬 sanitized best effort이며 외부 POST/outbox와 예외가 없다. 모델 band_*의
  guard/TOOL_BLOCKED/UNKNOWN fence는 유지한다. native 증명은 SDK 보고 부재를
  허용하지만 native token delta와 전체 RPC/효과/cessation 조건은 유지한다.
- NOT_STARTED는 오류 문자열로 정하지 않는다. DISPATCHING + durable process-clean,
  callback/Git/checker/question 없음, 업무 outbox는 REJECTED만 허용한다.
  TURN_START_INTENT가 있으면 단일 native turn/start 요청과 동일 client/RPC id의
  단일 definitive error 응답이 필요하다. intent→요청→응답 순서, RPC id 타입과
  정형 error code/message도 대조한다. 이전 응답 재사용·malformed error,
  sent-without-reply와 TURN_ACCEPTED는 fence다.
  60초 후 같은 작업을 다시 깨우고 재시도는 세 번까지 허용한다. 최초 실행을 포함한
  연속 네 번째 실패에서 FAILED/RETURNED로 끝낸다. owner 재시작에도 횟수는 유지한다.
  재시작 자체는 cessation 증거가 아니다. coordinator 알림은 내부 evidence work다.
- SDK4 send 경계는 call-local REST facade로 max_retries=0을 전달한다. 공유 SDK 객체나
  설치 소스는 바꾸지 않는다. typed connect failure와 400/403/404/413/422만
  NOT_SENT/REJECTED다. 408/409/429/5xx/ReadTimeout/receipt 불명은 DELIVERY_UNKNOWN이다.
  사용자 정의 raw send 구현은 hidden retry 부재를 입증하지 못하므로 보수적으로 fence한다.
- checker thread/Popen 시작 전 실패는 FAILED/NOT_EXECUTED와 단일 continuation이다.
  일시적 authority 불명이나 원장 조회 오류는 kill 권한이 아니다. kill은 명시적인
  cancel/revoke/run_end/control에만 한다. 설정된 timeout/log limit도 결과 관측이며
  자동 kill하지 않는다. 자연 종료한 leader의 살아 있는 descendants도 자동 kill하지
  않고 unreaped leader로 group identity를 pin한 채 감시한다. 명시적인 취소로 정리한다.
  따라서 장기 checker/descendant에는 새 기본 시간 예산을 넣지 않는다.
- UNKNOWN/TIMED_OUT 등 완료 continuation은 evidence only/replay FENCED다. 기존 effect
  및 run 전체 UNKNOWN fence는 줄이지 않는다. native cessation 불명은 READY child를
  게시하지 않는다. 도구 응답/interrupt/retire 오류도 VerificationYield 처리를 잃지 않는다.

## B5 공개 사전 준비 계약

기존 Controller가 Grant 기록 **전**, `prepare_settled_provider_recovery`를 호출한다.
인수: 기존 scope, 정확한 run_id/workspace/rooms/seats, dispatch_sender(인증된 사용자 id),
dispatch_room, dispatch_sha256(첫 지시 `PlatformMessage.content` UTF-8의 SHA-256).
이 함수는 pure scope 준비 함수이고 Grant를 발급·기록하지 않는다. 기존 scope의
workspace/room/seat ceiling을 넓히지 않는다. 실행 중 capability를 추가하지 않는다.

**raw Band 본문과 SDK intake 본문은 별개다.** SDK4는 `on_message` 전에
`band.runtime.formatters.replace_uuid_mentions(content, participants)`로 `@[[id]]`를
`@handle`로 치환한다. handle이 없으면 공백을 합친 이름(`@Synthetic-Reviewer` 형태)을
사용한다. 따라서 raw wire UTF-8 hash가 맞아도 SDK intake hash와 다를 수 있다.
Main은 **dispatch 전** 인증된 정확한 participant roster(`id/handle/name/type`)와
정확히 pin한 설치 SDK 버전·formatter 소스로 실제 intake 변환을 수행하고, 그 결과의
UTF-8 SHA-256을 `dispatch_sha256`으로 사전 등록해야 한다. roster/SDK 소스 pin과 raw
wire hash는 별도 operator evidence로 보존한다. 문자열 유사도, mention 제거, 임의
공백/대소문자 보정, raw/intake 이중 허용은 하지 않는다. dispatch 후 Grant를 고치거나
이번 후보를 운영 중인 C sweep에 설치하지 않는다. 새 authority issuer나 네트워크
readback API는 필요하지 않다. 이 변경은 WO08 D 제출 factory 후보이며, Main의
사전 설정 교정과 이후 독립 clone/E2E/guard 검증은 별도다.

등록되는 exact capability:

```text
settled_provider_recovery = {
  enabled: true, run_id, workspace, rooms, seats,
  max_attempts: 3, backoff_s: [60, 180, 300]
}
provider_dispatch = {sender_id, room_id, content_sha256}
```

`max_attempts`와 backoff는 exact integer다. 사용자 확정 정책은 dispatch 후 8시간이며
별도 임의 기본값이나 Grant expiry 재기록으로 계산하지 않는다. adapter가 시작될 때
owner의 `c_provider_policy`에 body/hash/등록시각을 immutable하게 보관한다. 다른 body의
재등록은 실패한다. 인증된 SDK room intake의 `User`(SDK4 사용자), 기존 `user`/`human`
메시지만 dispatch를 결속한다. `Agent`/`agent` 및 임의 sender class는 허용하지 않으며
대소문자를 일괄 정규화하지 않는다.
정확한 sender/room/content hash, platform message id, timezone-aware created_at을
`c_run_dispatch` 및 hash-backed artifact에 보관한다. created_at은 정책 등록 이후,
현재 관측시각 이전이어야 한다. 첫 receipt는 바꿀 수 없다. Main은 실제 SDK에 들어올
정확한 본문을 사전에 hash하고 정책 등록 이후 dispatch해야 한다. 시간 정밀도/시계 차이를
추정해서 보정하지 않는다. 미결속/시각 불명/receipt 충돌은 자동 복구를 허용하지 않는다.

기준 deadline = 신뢰된 dispatch created_at + 8 * 3600초. Grant body/revision/factory/mandate를
rewrite하지 않는다. 복구 직전 pinned policy와 scope를 일치시키고 dispatch artifact의
hash/run/policy/platform id/sender/room/content hash/시각을 readback한다.

허용 native codexErrorInfo 문자열은 serverOverloaded, usageLimitExceeded,
internalServerError 세 개다. responseTooManyFailedAttempts와 httpConnectionFailed는
키가 정확히 하나이고 그 값이 객체인 tagged object만 허용한다. 문자열 형태의 두
변형은 허용하지 않는다. 원장에는 payload 대신 변형 키만 남기며 error 프레임과
turn.error 전체 객체의 동일성도 검사한다. responseStream 변형과 안전 거절은
대상에 포함하지 않는다. error 객체의 다른 schema/transport closed,
userMessage 외 native item, callback, 업무 outbox(ACK도 포함), Git/checker/question,
미응답 RPC, cessation/source/settings 불명은 기존 fence다. 첫 dispatch의 thread/start는
provider 경로에서만 cwd/model/on-request/workspace-write와 응답 owned thread를 대조하여
허용한다. 기존 명시적 capacity action의 허용 요청 목록은 넓히지 않는다.

기존 terminal_evidence 및 Recovery source/quiescence/readback을 모두 통과한 효과 0
attempt만 같은 원 입력의 단일 child를 만든다. lineage당 최대 세 retry와
60/180/300초 wait를 `c_provider_retry`, `c_retry_wait`에 durable하게 보관하며 READY
게시 전에 not_before를 예약한다. 재호출은 같은 child/due를 돌려준다. usageLimit은
동일 attempt/client의 native account/rateLimits/updated에서 사용률 100 이상인 모든
window의 resetsAt까지 기다린다. 관측이 없거나 malformed면 숫자를 추정하지 않는다.
자동 경로는 dispatch/reset/schedule 증거를 reconciliation 전에 검사하므로,
reset 누락으로 child가 생기지 않을 때 기존 DELIVERY_UNKNOWN도 풀리지 않는다.
예정 retry 시각이 dispatch+8시간보다 늦거나 횟수가 소진되면 FAILED/RETURNED와
coordinator 내부 실패 알림으로 종결한다. 일반 run 종료/취소 운용은 기존 Controller
책임이고 scope가 종료되면 queue drain도 멈춘다.

## B6 적용 범위와 한계

SDK 완료 대리 답장 및 peer question 문맥은 sanitize 후 보낸다. 확정 local refusal은
reply NOT_SENT(code)로 기록하며 업무 model tool 실패를 성공으로 바꾸지 않는다.
로컬에서 미송신된 질문은 FAILED/RETURNED로 종결하고 내부 blocker 알림을 사용한다.
transport uncertainty는 여전히 fence다. 질문 전체 사전 검사를 callback 응답 앞에
옮기는 추가 정리는 아직 포함하지 않았다. native Git 환경에 GIT_OPTIONAL_LOCKS=0,
close에 짧은 group cessation polling을 추가했다. continuation 크기 상한/Git NOT_APPLIED/
추가 timeout-quiescence/CORRECT_INPUT_ERRORS 확대는 이 변경에 포함하지 않았다.

## 과거 사례와 검증 경계

과거 두 번째 GRANT_INACTIVE는 TURN_ACCEPTED + 업무 ACK 3건 뒤 발생한 사례다.
본 NOT_STARTED 규칙으로 재분류할 수 없다. 상위 `.git` 생성 가설은 이번 합성 fixture로
일반 결함을 검사했을 뿐 과거 runtime 직접 증명으로 쓰지 않는다.

시험: `test_wo08_sweep`, `test_wo08_paths`, `test_wo08_provider` 및 기존 capacity/timeout/
recovery/wiring/verification 회귀. controller snapshot→verify→read E2E도 합성이다.
최종 깨끗한 Linux clone suite 및 baseline..candidate 강화 guard는 Main이 tip 고정 후
독립 수행한다. 실제 계정/제출 run/push는 이 작업에 포함되지 않는다.
