# DarkHarness

An original, runtime-independent Linux execution foundation with an explicit
Windows-to-WSL Host Bridge. This is Factory infrastructure, not a challenge
submission or a prebuilt domain solution.

See the [dated integration evidence and remaining limits](docs/integration-status-20261002.md)
for the distinction between component checks, real Docker checks, and native seat qualification.

## Current scope

Implemented: empty-state bootstrap, directory-inode OS owner lock, SQLite WAL
ledger, epoch/attempt fencing, monotonic terminal states, atomic intent/event
records, framed IPC, paged artifacts, separate scoped seat channel, control
records and a live foreground recovery-subject binding.

The optional Band integration adds SDK Codex dispatch, durable inbox/outbox,
complete handoff assembly, scoped permission routing and peer continuations.
Automatic recovery of uncertain external effects, GUI/installer and full approval
enforcement of native shell tools remain incomplete. A foreground session is maintained until explicit shutdown
or transport EOF; seat/room idleness does not stop it.

## Run from source

Python 3.12+, Linux `/proc`, `flock`, and `/usr/bin/stat` are required for core.
No runtime dependencies are needed. In a Linux checkout:

```sh
python3 -B -m darkharness.gateway --state-root /tmp/dh-explicit-state
```

The state-root argument has **no default**. Source and state are separate.
Use a disposable Linux filesystem root for development. Filesystem type is
reported; locking on drvfs/9p is explicitly unverified, not globally prohibited.
Stdout contains only framed responses, not human-readable logs. Use the Bridge
consumer or the documented Python IPC helpers; diagnostics are on stderr.

On Windows, from the source checkout (replace placeholders with observed values):

```text
python -B -m darkharness.host_bridge --distribution <observed-distro> --user <observed-user> --linux-cwd <exact-linux-source-copy> --linux-python <absolute-linux-python> --state-root <explicit-linux-state>
```

Startup arguments use an argv array, never a shell. Windows `wsl.exe` parsing of
literal double quotes in startup argv is not supported and is rejected before
launch. After `hello`, use `workspace.enter` and `path.probe` UTF-8 payloads for
user paths containing double quotes. Unicode, spaces and single quotes in startup
paths are supported by the tested route. The Windows integration test reports
T02 PARTIAL, not full argv-double-quote qualification.

## Reproducible checks

```sh
python3 -B -m unittest discover -s tests -p test_foundation.py -v
```

Linux runs the actual process/fault/security tests; Windows runs wire, launcher,
guard and stderr-consumer tests and explicitly skips Linux-only tests.

Real Windows/WSL path test (observed distro/user, exported source copy required):

```text
python -B tools/test_windows_wsl.py --distribution <distro> --user <user> --linux-python <python> --source-linux <exact-copy> --windows-evidence <private-evidence-directory>
```

It preserves Windows sentinels as private test evidence. It never terminates or
reconfigures WSL to release an inherited Windows cwd handle.

## Public repository hygiene

Before commits, configure **only this repository**:

```sh
git config --local core.autocrlf false
git config --local core.hooksPath .githooks
```

The hook runs `python -B tools/public_guard.py` against the full staged index.
It checks credential shapes, sensitive filenames, private absolute paths and
personal emails. Use an actual actor name and a nonpersonal `.invalid` email.
Before publication Main must also scan every commit in the publication range,
including `python -B tools/public_guard.py --revision <commit>` for each revision.
The guard is prevention, not proof that arbitrary secrets cannot escape.

See [the W1 API and boundaries](docs/w1-api.md). Installation packaging is
present but installed-entry qualification is NOT_RUN. The implementation does
not copy EUNHArness code or include track specifications/answers.

## Optional Band SDK integration

Use a Linux virtual environment and install the pinned optional dependency:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install '.[band]'
```

The integration uses Band SDK 4.0.0 and an independently authenticated Codex CLI.
Keep agent credentials, run configuration and state outside this repository.
Configure connection/runtime, model, effort and turn timeout independently per
seat with named controller-owned profiles. Legacy flat Codex configuration still
works; the default turn timeout is 3600 seconds. Role names select neither a model
nor authority. See [seat settings](docs/seat-settings.md) for precedence, safe
validation/effective settings and the optional protected Claude Code backend.
Current diagnostic choices remain unchanged; Claude inference qualification is
NOT_RUN. Claude dependencies belong in a separate environment, not the active
shared Codex runtime.

Read [the SDK support matrix and launch sequence](docs/sdk-support.md) before
starting seats. Component tests exercise the real SDK with synthetic RPC clients;
they do not establish live Band teamwork, unattended completion or contest
qualification. The controller retains UNKNOWN fences instead of replaying
unconfirmed effects.

For the component suite, set `DH_OFFICIAL_ROOT` to an external, trusted official
checker checkout and run the tests inside the SDK virtual environment:

```sh
python -B -m unittest discover -s tests -v
```

Third-party credential-pattern attribution is in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
