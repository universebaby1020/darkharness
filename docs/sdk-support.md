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

Worker's final focused run: **45 tests passed**, including external official mandate checks, busy/continuation/approval paths, opaque credential guard, stale/UNKNOWN fences, foreground framed gateway/shared Store and Linux owned-group cleanup. Readiness test is synthetic and explicitly records inference `NOT_PROBED`; it is not account authentication evidence. Actual runtime and Band operations are separately owned by Main.
