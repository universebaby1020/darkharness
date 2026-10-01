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
4. SDK4 `thread/resume`는 dynamicTools를 갱신하지 않는다. Durable toolset/source/mandate fingerprint가 같을 때만 resume한다. 구판 thread의 fingerprint가 없거나 달라졌으면 `THREAD_TOOLSET_CUTOVER_INTENT`와 `THREAD_TOOLSET_BOUND`에 old/new native thread 계보를 남기고, 원문 전체를 사용해 새 native thread를 시작한다. UNKNOWN replay 허가는 아니다.

Control parser는 SDK `strip_leading_mentions`를 사용해 normalized `@account/handle /dh-answer ...`를 일반 작업보다 먼저 처리한다. Peer 권한은 mention 문자열이 아니라 실제 sender id와 c_question.peer로 검증한다. Reader task가 dispatch 전에 만들어져도 immutable owned-client binding으로 stdout/stderr를 원 attempt에 연결하고, 다음 attempt는 새 owned client를 쓴다. 오래된 stream을 현재 unrelated attempt로 재라벨링하지 않는다.

Same-UID tampering은 OS 격리라고 주장하지 않는다. Source fingerprint와 process 관측은 직접 확인 가능한 범위의 증거이며, 실 Band room context 재연결·추론·room provenance의 최종 live 확인은 Main이 수행한다.
