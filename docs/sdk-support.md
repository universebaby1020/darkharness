# Band SDK 4.0.0 support

This integration extends the installed `band.adapters.codex.CodexAdapter`; it does not replace the SDK or patch installed files. `DurableCodexAdapter` uses the inherited turn runner and RPC/event plumbing. `contract.py` defines the provider-independent interface. Compatibility is pinned to SDK **4.0.0**; private hooks below require revalidation before upgrading.

## Inspected upstream hooks

Line numbers refer to the exact SDK 4.0.0 sources, not this extension. Source inspection is not live interoperability evidence.

| Source / lines | Observed SDK behavior | Extension |
|---|---|---|
| `adapters/codex.py:432–528,605–616` | `cwd` forbidden; room workspace resolver required | Mandatory scoped `workspace_for_room` |
| `adapters/codex.py:443,459–463` | approval policy `never`, approval mode `manual`, permission wait 300s, turn timeout 180s | `workspace-write` + `on-request`; manual permission handler overridden with authenticated Grant router; explicit/default timeout recorded, not measured safe |
| `adapters/codex.py:832–893` | busy branch at 860–865 drops incoming work | Durable per-seat serial queue and separate control lane; dispatch returns without waiting for turn |
| `adapters/codex.py:895–1105,1180–1510` | turn runner and structured events | Inherited runner, guarded evidence, durable attempt/state mapping |
| `adapters/codex.py:1631–1639` | client construction hook | SDK stdio client subclass with owned process group and guarded streams |
| `adapters/codex.py:1664–1765` | thread start/resume; fallback history injection at 1712–1720 | Resume supported; fallback injection disabled; unknown effects fenced, not replayed |
| `adapters/codex.py:1767–1836,1888–2054` | dynamic tool registration and server requests; own tools execute at 1947–1959 without manual approval check | Guard all tool results/sends; deduplicate callbacks; `dh_peer_question` routes full context to coordinator, resolves callback and yields; answer is a continuation |
| `adapters/codex.py:2056–2164` | permission handler; manual wait at 2088–2104 | Single callback owner consumes controller-recorded Grant; unknown shell/interpreter/privileged requests denied |
| `adapters/codex.py:3529–3540` | explicit system prompt bypasses rendered default | Exact mandate snapshot prompt avoids inherited human-wait instructions |
| `adapters/codex.py:3542–3554,3600–3643` | turn overrides and sandbox mapping | Fixed native sandbox/approval settings; seat slash commands cannot escalate |
| `integrations/codex/stdio_client.py:59–109` | subprocess not in a new group; close targets parent | Linux new session/group; identity checked before owned-group cleanup; uncertainty fails closed |
| `core/simple_adapter.py:287–295` | default interrupt is a no-op | Interrupt reaches detached active turn and cancels owned process |
| `agent.py:108–122,269–275` | `Agent.create`; control callback wired to interrupt | Real Agent path used by SeatManager |
| `config/loader.py:29–33,69–111` | explicit `config_path` supported | External owner-only 0600 file; loader called with explicit path |
| `prompts/roles.py:92,142,146` | default roles contain silent waiting instructions | Explicit generic mandate prompt; actual callback hooks, not prompt-only routing |

SHA-256 (copied reference and installed source byte-identical):

- `adapters/codex.py`: `b3738db2e31376726e7edbe953c5f3ac5e26f2681066fb20a109a8a819529a4c`
- `integrations/codex/stdio_client.py`: `bf75c455ba027711656bbc77f3d0ea9a1e6b6140f6353163c5fdc82b510c8912`
- `core/simple_adapter.py`: `4d767bc6724aac78578783340d97e51a1c090868dde9b994d6ff9328944b8e0d`

## Launch interface

Use a committed source snapshot and the SDK-pinned Linux virtual environment. Keep run configuration, credentials and state outside the public source/result workspace. `run.example.json` contains placeholders only. Alias, display name, participant ID and typed mention ID are separate identities; IDs are never derived from aliases. Model/effort come from run configuration, not role assignment.

```text
python -B -m darkharness.integration validate --config <external-run-config>
python -B -m darkharness.integration prepare --config <external-run-config> --official-root <external-official-checkout> --official-python <python>
python -B -m darkharness.integration.gateway --state-root <linux-state-root> --official-root <external-official-checkout> --official-python <python>
```

`prepare` writes mandates in the configured result repo without reading credentials. It runs the external official toy/tablekeeper mandate checks, not the full competition qualification. `Model:` contains the exact model ID; effort is separate. Harness label `DarkHarness (Band SDK Codex)` identifies this custom runtime, not a claim about native UI labels.

The Host Bridge must launch the integration gateway module (not a second service) using an argv array. Retain B's framed protocol envelope and controller hello. Controller sequence:

1. `hello`; use returned environment identity.
2. `grant.record` with approved external Grant and `expected_revision`; `run.bind` with actual recovery subject.
3. `integration.start`, payload `{"config_path":"<external-run-config>"}`; returns job ID immediately.
4. `integration.job`, payload `{"job_id":"<id>"}`; wait for RETURNED or inspect FAILED reason.
5. `integration.status`; inspect work state/delivery and unknown outbox.
6. `integration.events` (`after`, `count`) then `integration.artifact.read` (`artifact_id`, `cursor`); artifact bytes are base64 pages. These are local runtime evidence, not Band room export.
7. `integration.cancel` with operation ID and payload `seat`/`attempt`, or `integration.stop`. `integration.run_end` uses payload `run_id` and `state` (`STOPPED`, `COMPLETED`, `REVOKED`), with the current revision. Grant revoke also schedules stop.

Gateway and seats share B's single Store, epoch and mutex. Seat socket remains B's read-only session. No independent Store writer is started. `integration.reconcile` refines a work receipt using attempt and a receipt containing evidence refs; it is not an outbox-UNKNOWN delivery adjudicator.

## Authority, secrets and evidence limits

- Grant requires explicit end condition; numeric expiry is optional and validated when present. STOP/revoke remain effective. Exact callback argv matching is not OS sandbox enforcement; native workspace-write governs ordinary repo tools.
- Credential values received by the runtime loader are registered only in the in-memory guard, including opaque keys. Message/event/tool outputs are guarded. SDK raw logging is disabled. Credential mode 0600 does not protect against other same-UID processes.
- Docker/WSL interop are not claimed isolation boundaries. Unknown shell wrappers or privileged requests are denied, not labelled autonomous success.
- Lost external ACK and restarted STARTED work retain UNKNOWN fences. No unconditional replay. Terminal task state is monotonic; message lifecycle is separate. Turn success does not prove WorkItem acceptance.
- The provider-independent facade currently allocates queue attempts rather than preserving an arbitrary caller-supplied dispatch attempt. Use the gateway/mailbox-owned attempt identity for this version. Parent question work remains yielded/paused; its goal acceptance requires reconciliation.
- Required SDK seams were exercised in component tests using synthetic RPC/platform clients. Actual Band detached-tool lifetime, provider inference, room round trips and three-seat teamwork require live verification by the operator. No unattended live success is asserted here.

Focused Linux regression command:

```text
python -B -m unittest discover -s tests -p 'test_integration_*.py' -v
```

The current focused result and source hashes are recorded in `dev-evidence/WO-DH0-01R3/worker-repair/RESULT_KO.md`. Tests are components using installed SDK4 and isolated actual Linux Git/processes, not provider inference or Band traffic. Actual runtime and Band operations remain Main-owned.

## WO-DH0-01R3 로컬 Git와 유지보수 재개

Native 설정은 계속 `workspace-write` / `on-request`다. `/bin/bash -lc 'git add ... && git commit ...'` 같은 shell chain은 여전히 거절한다. 실제 Codex dynamic interface에는 아래 두 도구를 등록하며 SDK의 `tool_call` / `tool_result` 이벤트도 guarded room 경로로 보낸다.

- `dh_local_git_commit`: `{cwd, paths, message, expected_head}`. Seat가 이미 작성한 regular files만 읽어 Git plumbing으로 커밋한다. Author/committer는 설정된 seat 이름과 `.invalid` 주소다. 별도 commit index로 무관한 staged 변경을 제외하고, 실제 index의 선택 경로만 index lock/CAS로 맞춘다. Ref도 기대 HEAD로 CAS한다. Hooks, filters, fsmonitor, signing, global config, shell, network, amend/merge/rebase는 실행하지 않는다.
- `dh_review_snapshot`: `{cwd, revision, name}`. 정확한 40-hex commit의 독립 shallow checkout을 새 scratch 하위 디렉터리에 만든다. 원 저장소의 객체를 hardlink하지 않고, remote/config를 복사하지 않는다. Pinned tree의 파일 수·바이트 수와 object bytes를 측정하고 복사 후 tree/파일 bytes를 대조한다. 명시된 `max_bytes` / `max_files`가 있으면 추가로 검사하며, 필수 영구 예산값은 아니다.

기존 authenticated `grant.record`의 scope에 다음 capability를 더한다. Seat payload는 Grant가 아니다. `workspace`는 실제 canonical result repo이며, `.git`, 경로 이탈, symlink, nested repo 및 gitlink tree는 허용하지 않는다. 디렉터리 범위 `.`은 승인된 repo의 일반 파일을 뜻한다. 파일명마다 새 human 승인을 요구하지 않는다. Reviewer scratch는 source commit 범위에서 제외된다.

```json
{
  "run_id": "<same run>",
  "workspace": "<canonical result repo>",
  "seats": ["<configured seat aliases>"],
  "rooms": ["<existing room id>"],
  "local_git_write": {"directories": ["."], "paths": []},
  "review_snapshot": {"scratch": "<canonical result repo>/.review"},
  "continuation": {"operations": ["<known interrupted/yielded/returned parent id>"]}
}
```

Capability 없는 호출은 실패한다. Git intent와 candidate/index artifact는 효과 전에 같은 Core Store에 보관하고, exact ref/index readback receipt 후 ACK한다. 재호출은 같은 request receipt만 돌려준다. 미확인 receipt는 UNKNOWN이며 새 커밋을 추측해 반복하지 않는다. Controller-only `integration.git.reconcile`은 operation id와 payload `{attempt, grant_id, effect_id}`로 현재 ref/parent/tree와 index artifact를 대조한다. 선택 index가 기존 fingerprint와 정확히 같을 때만 기존 candidate의 index 설치를 마친다. 다른 ref/index 상태는 UNKNOWN을 유지한다.

유지보수 재개 순서 (기존 controller framed envelope의 `operation_id`를 parent로 사용):

1. Main이 기존 provider/process를 정상 중단하고 새 소스와 mandate snapshot을 먼저 확정한다. `integration.stop`은 유지보수이며 user STOP를 대신 기록하지 않는다. User STOP/revoke는 기존 run/Grant end control로 유지된다.
2. 새 gateway의 동일 Store에서 `integration.recovery.observe`, payload `{attempt, grant_id}`. Settled delivery, ACKed outbox/Git 효과, owned process identity 및 두 번의 동일한 canonical source/ref/index fingerprint를 검증하고 durable `evidence_id`를 돌려준다. Native DELIVERY_UNKNOWN 또는 미확인 효과는 이 API로 해제하지 않는다.
3. Main이 `integration.start`와 `integration.job`으로 실제 seats/readiness를 확인한다. `integration.continue`, payload `{attempt, grant_id, evidence_id}`. 원문 input, 제공된 peer answer, parent operation/attempt, original thread, 검증 receipt를 보존한 READY child를 만든다. 새 inbox/human message는 만들지 않는다. `wake_job_id`를 `integration.job`으로 확인한다. 실제 SDK room execution context의 `AgentTools.from_context`로 도구를 재연결한다. Room context가 아직 없으면 `ROOM_CONTEXT_NOT_READY`이며, 같은 receipt로 controller 재시도할 수 있다.
4. SDK4 `thread/resume`는 dynamicTools를 갱신하지 않는다. `c_owned_thread`/`c_latest_thread`의 같은 run/room/seat 소유 기록으로 자기 latest thread를 선택하며, SDK의 sender-unfiltered history metadata는 선택 근거로 쓰지 않는다. 실제 tool schema/mandate 호환성과 소스 revision 증거를 분리한다. 구현 파일만 변경되면 맥락을 버리지 않는다. 도구/mandate 변경은 `thread/read(includeTurns=true)`로 자기 기존 native history와 durable 전체 작업 원문을 artifact에 연결한 후 새 thread를 시작한다. 과거 tool effect를 재생하지 않으며 ThreadResume.history는 사용하지 않는다. 신규 native thread마다 정확한 mandate를 주입하고, 같은 thread의 검증된 prompt hash가 있을 때만 주입을 생략한다. 구판 소유 증거가 없으면 다른 좌석 thread를 추정해 resume하지 않고, 기존 자기 durable task 원문을 보존해 새 thread를 시작한다. UNKNOWN replay 허가는 아니다.

Control parser는 SDK `strip_leading_mentions`를 사용해 normalized `@account/handle /dh-answer ...`를 일반 작업보다 먼저 처리한다. Peer 권한은 mention 문자열이 아니라 실제 sender id와 c_question.peer로 검증한다. Reader task가 dispatch 전에 만들어져도 immutable owned-client binding으로 stdout/stderr를 원 attempt에 연결하고, 다음 attempt는 새 owned client를 쓴다. 오래된 stream을 현재 unrelated attempt로 재라벨링하지 않는다.

Same-UID tampering은 OS 격리라고 주장하지 않는다. Source fingerprint와 process 관측은 직접 확인 가능한 범위의 증거이며, 실 Band room context 재연결·추론·room provenance의 최종 live 확인은 Main이 수행한다.

## Trusted verification와 continuation

- 같은 shared SerializedOwner 위에 seat/router당 VerificationBridge/VerificationBroker 하나를 유지한다. `dh_verify` 입력은 `{check_id, checkout_receipt, revision}`뿐이며 check_id는 인증된 Grant의 verification.checks 키를 schema enum으로 노출한다. Operation/attempt/effect ID는 native callback 실행 문맥에서 만든다. Git 신규 ACK 직후 실제 creating router로 record_git_origin을 호출한다. Canonical Git receipt/hash는 변경하지 않는다. 구판 ACK 재생만으로 run을 역추정·등록하지 않는다. 과거 receipt는 Main의 authenticated execution ledger resolver가 필요하다.
- 검증은 start_async 뒤 native callback을 한 번 응답하고 turn/interrupt로 모델 턴을 종료한다. VerificationYield 동안 원 작업은 RUNNING/STARTED로 계속 소유하며 기존 SDK turn_timeout_s(기본180초)와 실제 checker wait를 별도 기록한다. 모델 polling이나 새 기본 checker 시간제한은 없다. 실제 SUCCEEDED/FAILED 결과만 원 전체 작업/parent/thread와 guarded 결과/artifact hash를 보존한 단일 READY continuation을 만든다. UNKNOWN, 취소, STOP/revoke, stale attempt/config는 evidence-only이고 자동 재실행하지 않는다. 시작만으로 작업 성공을 기록하지 않는다.
- verification.py의 기존 trusted `(outdir, exit)->bool` parser는 유지한다. `json-criteria-v1`은 외부 Grant의 report_criteria를 읽으며 immutable `{request,config}`를 받는다. 모든 report_files에 기준을 선언하고 최소 하나의 revision_path anchor를 요구한다. 모든 anchor는 정확한 request revision과 일치해야 한다. revision이 없는 aggregate summary는 anchor를 생략할 수 있지만 필수 per-report anchors와 파일은 그대로 요구한다. equals/at_least/positive/zero/equal_paths/each는 literal JSON key/index 경로만 사용하며 eval/import/model 코드 실행은 없다. schema/state/mode/stage/count/skip 조건과 suite 값은 외부 설정에만 둔다. exit0만으로 PASS가 아니다.
- `dh_verification_read` 입력은 `{effect_id, artifact, offset, limit}`. 같은 run/authorizedseat/room과 실제 result c_artifact hash, 선언된 로그/report 파일 hash를 검증하고 전체 text를 secret guard로 먼저 가린 후 page를 반환한다. page 최대16384 chars, 단일 artifact export 최대1MiB이며 이는 진단 export 범위이지 checker runtime/disk 제한이 아니다. 더 큰 private 로그는 controller가 진단한다. 모델에는 private output path나 원시 stdout/stderr를 직접 반환하지 않는다.
- cleanup은 소유 검증 job을 취소하고 기존 broker.close의 bounded join/UNKNOWN을 보존한다. Native process group 종료는 external Docker cleanup 증거가 아니며 cancellation receipt에도 NOT_PROVEN으로 남긴다. 실제 official checker/Docker/live room 보고는 Main의 확인 범위다.

## 증거 기반 SDK turn-timeout 정산과 자동 continuation

- SDK4의 `TurnResultAlreadyReported`는 timeout 자체가 아니다. `codex_timeout.py`는 설치 SDK4 Codex 소스 hash를 pin하고, canonical store에서 정확한 operation/attempt/own thread/turn/client, workspace-write/on-request native turn 시작, SDK failed timeout outcome, controller interrupt 요청/ACK, native interrupted terminal(error=null), 동일 owned client의 PROCESS_STOPPED(members=[]) 순서를 모두 검증한다. 일반 runtime 계약과 SDK-specific detection은 분리된다. 오류 문자열 하나나 seat의 safe 주장은 정산 근거가 아니다.
- 같은 seat의 outbox가 전부 ACKED/REJECTED이고 모든 Git effect가 ACKED여야 한다. 취소·pending 질문/approval/callback/checker, native 외부 효과 불명, owned process 잔존, unresolved run, STOP/revoke는 차단한다. 두 번의 canonical repo/index/source readback과 immutable Git candidate/receipt/index artifact/object readback이 일치해야 한다. 여러 commit은 실제 parent/tree/ref/identity와 연속 chain을 확인하며 마지막 ACKed commit/index만 현재 HEAD/index와 일치시킨다. unrelated HEAD는 허용하지 않는다.
- 성공한 정산은 `SETTLED_TURN_TIMEOUT`과 FAILED/RECONCILED 결과를 남기며 acceptance는 NOT_EVALUATED다. 원 full input/own thread/commit/ACK receipt를 보존해 단일 continuation을 생성한다. 기존 ACKed send payload hash는 canonical recovery proof/readback으로 재사용하며 외부 전송하지 않는다. 실패 native command exit는 native_failures에 남긴다. telemetry ACK나 같은 handoff 반복은 진행으로 세지 않는다. source 변화/새 ACKed handoff/새 authenticated question/checker 결과 진전 없이 연속 timeout이면 TIMEOUT_NO_PROGRESS다. 새 시간/횟수 ceiling은 없으며 기존 turn_timeout_s(default180.0)는 유지한다.
- 자동 복구는 default-off다. 외부 run config에 `auto_recover_settled_timeouts: true`, Controller Grant scope에 `settled_timeout_recovery: {enabled:true,run_id:<정확한 run>,workspace:<canonical workspace>,rooms:[<정확한 room>],seats:[<허용 alias들>]}`를 함께 설정한다. 외부 scope와 모든 바인딩이 일치해야 한다. 새 정상 peer task는 authenticated inbox로, 자식은 실제 c_recovery/c_question/c_verification_continuation 및 hash-verified known result로 원 inbox 계보를 검증한다. input.parent_operation만 믿지 않는다. 기존 continuation.operations는 수동 maintenance용으로 유지한다.
- live 예외 처리와 readiness 이전 startup이 같은 검증을 재사용한다. 초기 사용자 dispatch를 다시 보내거나 운영 SQLite를 직접 편집할 필요가 없다. maintenance stop과 genuine run STOP은 다르며 STOP/revoke를 지워 복구하지 않는다. Controller `integration.reconcile` payload는 `{attempt,grant_id}`이고 검증된 timeout 정산만 수행한다. `integration.timeout.recover`는 같은 payload로 정산+continuation/wake를 수행한다. 임의 receipt/evidence_refs 입력은 거절한다. Event/result의 evidence_refs는 controller가 canonical store에서 생성한 seq+artifact hash다.
- owner store/현재 Grant/native-controlled sandbox가 신뢰 경계다. same-UID 공격자에 대한 OS 격리나 외부 Docker/network cleanup 증명이 아니다. WSL SDK stdio/component 시험과 실제 운영 재개·toy acceptance는 구분한다.
