# W1 API / integration seam

## Authority and ownership

`darkharness.core.Store(absolute_state_root)` elects via an exclusive `flock` on
the state-root directory inode. `owner.lock` is diagnostic only: removing it
cannot transfer execution rights. A contender returns the current owner identity
and exposes read-only status, not a second writer. No automatic replay occurs.

`store.owner`, `identity()`, `status()`, `epoch`, `instance`, `env`, `db`, `close()`
are available to the next integration layer. `process_identity(pid)` produces
PID + boot ID + `/proc` start marker. `is_alive(identity)` rejects PID reuse and
zombies. Callers must serialize all access to a Store (the gateway uses a mutex).
There is exactly one canonical writer connection per owner.

`store.transaction(epoch=None)` opens a short `BEGIN IMMEDIATE`, checks the
current stored epoch/instance and held root inode, commits or rolls back. Use it
for new integration tables/writes; never create a new unfenced writer. No slow
file/provider I/O belongs inside this transaction. SQLite does not isolate
external writers or same-UID shells.

- `intent(operation, attempt, payload, epoch)` atomically saves a pending
  operation, event and content-addressed payload artifact. Identical reuse is
  idempotent; a different payload conflicts; a different attempt is fenced.
- `transition(operation, attempt, state, epoch, revision)` validates all identities
  together. First terminal state wins. Late nonterminal or competing terminal
  observations append evidence but cannot downgrade the final state.
- `control(kind, id, body, expected_revision)` persists a controller-sourced record
  and event. Absent revision is 0. This is NOT a native shell permission bypass.
- `status()` rediscovers pending references, not execution results.

Terminal: SUCCEEDED / FAILED / CANCELLED / CLOSED_UNRESOLVED.
Nonterminal: QUEUED / RUNNING / PAUSED.

## Wire

`darkharness.ipc.envelope(action, request_id, operation_id=None,
environment_id=None, payload=None, expected_revision=None)` returns the full
version-1 request. Responses have request_id, operation_id, execution_status,
public_reason_code, evidence_refs, next_observable_action and data.

Frame: `DH1:` + 8 hex digits base64 length + `:` + 16 hex SHA256 prefix + `:` +
base64 UTF-8 canonical JSON + newline. This checksum detects corruption; it is
not authentication. Default encoded body bound: 1 MiB, page: 48 KiB; gateway
`--frame-bytes`/`--page-bytes` configure measured transport limits, not execution
budgets. Broken/truncated lines resynchronise at the next magic. Stderr never
enters the framing reader. Oversized output data becomes a JSON artifact reference.

Duplicate request IDs reuse the identical response within a 256-entry session
cache; changed content conflicts. Out-of-order IDs are valid. After eviction or
reconnection this transport cache is not exactly-once protection; durable
operation identity is the effect fence. Live seat binding revocation is checked
**before cached replies, including hello**, not only before new dispatch.

## Actions

`hello`: environment may initially be null; response includes protocol, actual
backend identity, limits and channel. Every subsequent request supplies the
returned environment_id. Wrong environment is rejected. The inherited stdio
session is the controller channel. No payload role/trusted/approved/grant field
can convert a seat session into controller.

| Action | Payload / relevant envelope |
|---|---|
| core.status / environment.inspect | Read-only bootstrap projections |
| operation.intent | operation_id; payload `{attempt, epoch, intent}` |
| operation.transition | operation_id; expected_revision; `{attempt, epoch, state}` |
| operation.status | operation_id; result includes payload_artifact_id and payload_bytes |
| artifact.record | operation_id; `{attempt, epoch, base64}`; atomic fenced artifact event |
| artifact.read | `{artifact_id, cursor:0}`; returns base64 bytes and next_cursor or null |
| settings.save | expected_revision; `{id, ...configuration}`; record only, not runtime activation |
| grant.record | expected_revision; `{id, source, scope:{...}, end_condition}` |
| grant.revoke | expected_revision; `{id}`; preserves revocation record |
| seat.bind | `{seat_id, operations:[ids], artifacts:[ids]}` |
| seat.revoke | `{seat_id}`; live sessions and cached response disclosure revoked |
| workspace.enter | `{linux_cwd:absolute_path}` after hello; returns actual realpath and received UTF-8 hex |
| path.probe | `{sentinel:absolute_path}`; actual argv/hex, cwd realpath, received path/hex, content SHA256 |
| run.bind | expected_revision; `{id, runtime_instance?:actual_identity}` |
| run.status | `{id}`; current_binding/recovery_alive/runtime_alive projections |
| shutdown | Explicit normal shutdown of this foreground channel |

All mutations above are controller-only. Seat tool session: connect to the
Linux abstract AF_UNIX `endpoint` returned by seat.bind, send framed
`{credential: issued_value}` once, then hello and ordinary envelopes. The
credential must remain private (not stdout logs, Git or room messages).
The credential is ephemeral: restart requires a fresh controller binding. Seat
allowlist: core.status, operation.status for bound operation IDs, artifact.read
for bound artifact IDs. Seat status excludes other operations' pending details.
Control actions including grant.create/extend/record are never seat tools.

Controller inherited stdio represents the Host Bridge launcher session, not an
OS proof that any process of the same UID is trusted. An arbitrary same-UID actor
can inspect process memory/files, tamper SQLite, or launch its own gateway when
no owner exists. No hostile same-UID isolation or blanket native tool authority
is claimed. C must implement native permission hooks/Grant consumers separately.

## Run-session supervisor

`run.bind` validates an optional live runtime process identity and records the
current gateway instance and epoch as recovery subject. The foreground gateway
remains live across returned turns/idle periods. `run.status` detects stale
bindings after owner replacement. Explicit rebinding uses the stored revision;
it does not launch a runtime or replay UNKNOWN effects. Recovery execution,
owned process-group cancellation and delivery reconciliation belong to the next
layer. W1 preserves pending references and refuses stale canonical writes.

## Bridge

`launcher_argv(distribution, user, linux_cwd, linux_python, state_root, probe_args)`
builds the exact wsl.exe distribution/user/cd/exec argv. Startup double quotes
are rejected; use framed workspace/sentinel paths. This is a known WSL argv
qualification gap, not an invented shell fallback. `windows_to_linux_path` calls
selected-distro wslpath via explicit argv and cwd `/`.

`Bridge(argv, windows_cwd=None, diagnostic_sink=None, frame_bytes=1048576)` drains
stdout and stderr independently. `request(action,payload,operation_id,
expected_revision,request_id,timeout)` is serialized and verifies correlation.
Optional timeouts are consumer/test limits, not product task budgets. Diagnostics
retain a bounded 256 KiB tail plus total byte count; supply a private sink for
full raw evidence. `close()` requests shutdown then observes launcher exit. A
failed launch/pipe close still waits/cleans up its owned child. Killing a launcher
is not proof that the Linux owner died; that fault observation is NOT_RUN here.
