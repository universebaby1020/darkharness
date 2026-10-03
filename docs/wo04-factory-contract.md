# WO04 공개 factory 계약

이 문서는 새 B 후보의 result repo, timeout 복구, 진단과 공개 guard 계약을 요약한다. 아직 release 성공 선언은 아니다. Codex mandate의 정확한 Harness 표기는 **Codex (DarkHarness Band SDK adapter)**다. 좌석별 연결·모델·effort·시간제한은 [좌석 설정](seat-settings.md), SDK별 증거와 기존 복구 흐름은 [SDK 지원](sdk-support.md)을 따른다. 역할 이름이나 helper 이름만으로 실행 자격, 권한 또는 실제 복구 성공을 추론하지 않는다.

## 1. Workspace와 결과 저장소

`result_repo`는 **선택적 명시 필드**다. 지정하면 workspace의 실제 하위에 있는 canonical 절대경로의 독립 Git 저장소여야 한다. 예를 들어 `workspace=/workspace`, `result_repo=/workspace/result`는 비개인 설명용 경로다. 결과 저장소는 미리 준비되어 있어야 하며, validate가 저장소를 생성하거나 다른 저장소를 찾아 대신 선택하지 않는다. workspace 자체, 바깥 경로, symlink, workspace 안의 상위 `.git` 아래 중첩 저장소는 허용하지 않는다. 직접 `.git` 디렉터리와 실제 repo identity를 확인하며 linked worktree나 간접 object store를 독립 저장소로 인정하지 않는다.

Run config와 기존 인증된 Grant scope에는 **동일한 workspace와 result_repo**를 등록한다. config만 바꿔 같은 Grant로 다른 repo를 선택할 수 없다. workspace는 계속 native cwd와 파일 권한 상한이다. 결과 repo 선택이 workspace를 축소·교체하거나 native 권한을 확대하지 않는다. Codex의 `workspace-write` / `on-request`도 유지된다. mandate, typed Git와 verification의 결과 repo 기준을 native cwd와 혼동하지 않는다.

`dh_local_git_commit`의 cwd는 결과 repo이고, `local_git_write.paths`와 `directories`는 그 repo에 상대적이다. `.`은 승인된 repo의 일반 파일 범위이지 workspace 전체가 아니다. review scratch, `.git`, 경로 이탈, symlink와 nested repo를 stage 권한으로 포함하지 않는다. `result_repo`를 생략하면 기존처럼 workspace와 결과 repo가 같은 root로 동작한다. 생략된 값은 기존 fingerprint 형식을 유지하지만 명시 값은 fingerprint와 RuntimeBinding에 포함된다. 같은 run에 고정된 값의 변경은 재시작으로 우회할 수 없으며, 다른 실행 의미를 선택하려면 새 run identity가 필요하다.

## 2. Timeout 자동 복구 준비

Controller용 `prepare_settled_timeout_recovery(scope, *, run_id, workspace, rooms, seats)`는 **순수 준비 함수**다. 원 scope를 수정하지 않고 deep-copy한 scope에 capability를 넣어 반환한다. 호출자는 run 시작 전에 반환값을 기존 인증된 `grant.record` 흐름으로 등록한다. 함수 자체는 Grant 발급, Store 기록, credential 읽기, run 시작, room cursor 선택이나 작업 재개를 하지 않는다. 새 state/run/Grant lifecycle과 기존 방 admission은 호출자의 책임이며, helper 호출은 그 절차의 완료 증거가 아니다.

입력 run은 부모 scope의 run과 정확히 같아야 하고 workspace도 canonical 절대경로로 정확히 일치해야 한다. rooms와 seats는 비어 있지 않은 중복 없는 명시 목록이며 부모 scope의 부분집합이어야 한다. 기존 recovery capability와 충돌하면 거절한다. Run config에는 `auto_recover_settled_timeouts: true`를 **명시**하고, 등록된 `settled_timeout_recovery`에는 enabled 및 같은 run/workspace와 허용 room/seat 범위를 담는다. 자동 복구는 default-off이고 config만으로 활성 권한이 생기지 않는다.

정산은 기존 canonical SDK terminal proof를 그대로 요구한다. 정확한 자기 operation/attempt/thread/turn/client, SDK timeout outcome, native interrupt ACK와 interrupted terminal, owned process cessation 및 정산된 효과를 검증한다. 오류 문자열이나 seat의 safe 주장은 proof가 아니다. UNKNOWN, 미정산 전송·Git·checker 효과, pending control, STOP/revoke와 진전 없는 연속 timeout의 차단을 유지한다. 정산된 timeout도 사용자 목표 acceptance 성공을 뜻하지 않는다. 원 전체 input과 자기 thread, ACK receipt를 보존하는 continuation이지 외부 효과의 무조건 재실행이 아니다.

## 3. 실패와 재시도 진단

R2의 `effect_phase=NOT_STARTED`는 Store 상태가 아니라 진단이다. `same_input_safe_retry=true`는 **중복 checker 효과 없이 재시도할 수 있음**만 뜻한다. 같은 잘못된 입력이 성공한다거나 권한이 복원된다는 뜻은 아니다. 알려진 결정적 validation 오류에는 `input_correction_required=true`, `same_input_expected_error`와 `INPUT_CORRECTION_REQUIRED`가 붙는다. 같은 입력·권한이면 다시 실패할 것으로 보므로 입력 또는 Controller 설정을 먼저 고친다.

자기 효과가 이미 존재하거나 미종결이면 기존 effect를 정산하고 fence를 유지한다. `OTHER_EFFECT_OUTSTANDING`은 이 요청의 intent가 없더라도 다른 seat/broker의 run 효과가 미종결이라는 별도 진단이다. 다른 seat의 실행을 UNKNOWN으로 바꾸거나 이 요청을 실행 완료로 정산하지 않는다. 읽을 수 없는 원장은 비실행 증거가 아니며 오류 캐시도 원장의 차단을 약화하지 않는다.

R4의 `DELIVERY_UNKNOWN`은 SEND_INTENT부터 SEND_ACK 전까지 정상 전송 중간 상태일 수 있다. 따라서 그 이름만으로 전송 실패를 단정하지 않는다. 그러나 미정산이면 fence는 여전히 유효하다. 프로세스 안의 모의 inbox/receipt는 실제 Codex 수신 증거가 아니며 SDK/platform ACK도 exactly-once나 목표 수용을 보장하지 않는다.

## 4. 공개 guard의 검사 범위

Guard 출력의 `mode`는 검사 구성을, `base`와 `target`은 실제 해석된 commit 경계를, `checked_commit_count`는 검사한 commit 수를 나타낸다. `ref`는 base를 제외한 `base..target`의 **모든 commit**을 검사하며 merge된 branch도 포함한다. base가 ancestor가 아니면 오류다. `revision`은 지정한 단일 committed tree와 metadata를 검사하므로 count는 1이다. `index`는 stage된 tree와 author/committer·예정 message를 검사하며 target은 INDEX, count는 0이다. 이것은 commit 이력 검사 완료가 아니다.

`published-history`는 target의 전체 reachable ancestry에 대한 **기록 전용** 검사이며 `EXPOSURE_RECORD_ONLY`다. exit 0을 후보 공개 승인으로 읽지 않는다. 구성 오류의 `scope_complete=false`, findings와 guard/config hash도 함께 보존한다. 후보 최종 guard는 Main 책임이며 단일 revision 결과를 전체 ref 검사 결과로 대체하지 않는다.

## 5. 검증 수준과 보고 원칙

현재 후보의 Windows 집중·관련 부품 시험은 전달된 Worker 보고 범위에서 통과와 건너뜀을 구분한다. Linux/SDK 의존 시험의 skip은 PASS가 아니다. 옛 private fixture 포함 전체 결과와 옛 clean-clone 결과는 서로 다른 구성이고 이번 후보의 현재 PASS로 재사용하지 않는다. 숫자는 원문 보고의 명령, 대상 판본/hash, 환경, pass/fail/skip와 함께만 인용한다. exit 0이나 파일 생성만으로 검증 완료를 선언하지 않는다.

Main 추가 관측에 따르면 B 코드 commit `58a007620d686feb92911795f9ce59b1aff8d754`의 clean Linux/SDK4 clone 전체 suite는 private fixture 추가 없이 290 run / 288 pass / 2 skip / 0 failure, exit 0이다. 두 skip은 원래 Main evidence와 ignored Controller E2E helper가 checkout에 없는 경우다. 해당 시험은 main 반영 전 후보 branch의 B 코드 판본에서 수행한 관측이다. **이 관측 시점에 문서를 포함한 최종 tip 전체 suite와 공식 model-free Docker E2E, 실제 runtime/model/Band 수신·timeout 복구는 NOT_RUN**이다. 별도 필수 E2E를 skip으로 면제하지 않는다. R3의 helper 및 proof 소비 부품과 실제 run lifecycle/admission·자동 재개의 실관측을 구분한다. 이전 실관측 세부 근거 없이 이름만으로 현재 성공을 추론하지 않는다. 이 문서 작업은 링크·Markdown/예시 구문 정적 확인만 수행하며 부품 시험을 다시 실행하지 않는다. 앞으로 Main 결과를 받을 때도 합성 Controller 체인, 설치/readiness, 공식 checker 결과와 live 관측을 각각 보고한다.

구현 근거 위치: `darkharness/integration/launch.py:210–260,504–523,586–589`, `contract.py:24–62`, `policy.py:20–60`, `git_broker.py:24–74,108–132,243–256`, `codex_timeout.py:18–52`, `verification.py:416–474`, `tools/public_guard.py:180–241`. 이는 확인한 B 소스 위치이며 실운영 성공 증거가 아니다.
