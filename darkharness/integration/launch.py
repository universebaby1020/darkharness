"""Three-seat run configuration. Credentials are loaded only in explicit start()."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict
import json
import math
import os
from pathlib import Path
import stat

from .artifacts import SecretGuard, mandate_checks, render_mandate, slug, snapshot_check
from .mailbox import IntegrationError, Mailbox, digest, encode
from .policy import ApprovalRouter
from .legacy_git_origin import execution_ledger_run
from .contract import BackendRegistration, RuntimeBinding, SeatSettings
from .git_broker import result_repo_boundary


def _fields(value, allowed, location):
    if not isinstance(value, dict):
        raise IntegrationError(f"CONFIG_OBJECT_REQUIRED:{location}")
    unknown = set(value) - allowed
    if unknown:
        raise IntegrationError(f"CONFIG_UNKNOWN_FIELD:{location}:{sorted(unknown)[0]}")


def _text(value, location):
    if not isinstance(value, str) or not value.strip() or any(c in value for c in '\r\n\x00'):
        raise IntegrationError(f"CONFIG_TEXT_REQUIRED:{location}")
    return value


def _timeout(value, location):
    if type(value) not in {int, float}:
        raise IntegrationError(f"TIMEOUT_INVALID:{location}")
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite or value <= 0:
        raise IntegrationError(f"TIMEOUT_INVALID:{location}")
    return float(value)


def codex_command_kind(command):
    """Supported native framing, including official npm Codex via pinned Node.

    This is not approval for arbitrary Node scripts or shell/permission flags.
    Installation identity is checked locally by preflight, never by inference.
    """
    tails = (['app-server'], ['app-server', '--listen', 'stdio://'])
    binary = Path(command[0]).name.lower()
    if binary in {'node', 'node.exe'}:
        if len(command) < 3:
            raise IntegrationError('CODEX_OFFICIAL_NODE_ENTRYPOINT_REQUIRED')
        entrypoint = Path(command[1])
        if not entrypoint.is_absolute() or tuple(entrypoint.parts[-5:]) != ('node_modules', '@openai', 'codex', 'bin', 'codex.js') or command[2:] not in tails:
            raise IntegrationError('CODEX_OFFICIAL_NODE_ENTRYPOINT_REQUIRED')
        return 'official-node-entrypoint'
    if command[1:] not in ([], *tails):
        raise IntegrationError('CODEX_COMMAND_OPTIONS_UNSUPPORTED')
    return 'native-cli'


def resolve_settings(config):
    """Pure resolution. No auth reads, imports from config or native probes."""
    if not isinstance(config, dict) or not {'workspace', 'seats'} <= config.keys():
        raise IntegrationError('RUN_CONFIG_INCOMPLETE:workspace/seats')
    if not isinstance(config['seats'], list) or not all(isinstance(s, dict) and isinstance(s.get('alias'), str) for s in config['seats']):
        raise IntegrationError('SEAT_IDENTITY_REQUIRED')
    _fields(config, {"run_id", "room_id", "workspace", "credentials_path", "grant_id",
                    "model", "effort", "codex_command", "runtime_env", "turn_timeout_s",
                    "seats", "runtime", "connections", "default_connection",
                    "auto_recover_settled_timeouts", "result_repo"}, "run")
    result_repo = config.get('result_repo')
    if 'result_repo' in config:
        _text(result_repo, 'run.result_repo')
        result_repo_boundary(config['workspace'], result_repo)
    run_runtime = _text(config.get("runtime", "codex"), "run.runtime")
    if run_runtime not in BACKENDS:
        raise IntegrationError("UNSUPPORTED_RUNTIME:run")
    # Validate declared values even if all seats override them.
    for key in ("model", "effort"):
        if key in config:
            _text(config[key], f"run.{key}")
    if "turn_timeout_s" in config:
        _timeout(config["turn_timeout_s"], "run")
    profiles = config.get("connections")
    legacy = "connections" not in config
    if legacy:
        if run_runtime != "codex":
            raise IntegrationError("CONNECTION_PROFILES_REQUIRED")
        profiles = {"legacy": {"runtime": "codex", "command": config.get("codex_command"),
                                "runtime_env": config.get("runtime_env", {})}}
        default = config.get("default_connection", "legacy")
    else:
        if not isinstance(profiles, dict) or not profiles:
            raise IntegrationError("CONNECTION_PROFILES_REQUIRED")
        default = config.get("default_connection")
    for name, profile in profiles.items():
        _text(name, "connection.name")
        _fields(profile, {"runtime", "command", "runtime_env", "model", "effort", "turn_timeout_s"}, f"connection.{name}")
        runtime = _text(profile.get("runtime"), f"connection.{name}.runtime")
        if runtime not in BACKENDS:
            raise IntegrationError(f"UNSUPPORTED_RUNTIME:connection.{name}")
        for key in ("model", "effort"):
            if key in profile:
                _text(profile[key], f"connection.{name}.{key}")
        if "turn_timeout_s" in profile:
            _timeout(profile["turn_timeout_s"], f"connection.{name}")
        command = profile.get("command")
        if "command" not in profile and runtime == run_runtime == "codex":
            command = config.get("codex_command")
        if not isinstance(command, list) or not command:
            raise IntegrationError(f"CONNECTION_COMMAND_REQUIRED:{name}")
        for arg in command:
            _text(arg, f"connection.{name}.command")
        if runtime == "claude_code" and len(command) != 1:
            raise IntegrationError(f"CLAUDE_CLI_PATH_ONLY:{name}")
        if runtime == 'claude_code' and (command[0].lower().endswith(('.exe', '.cmd', '.bat')) or command[0].startswith('/mnt/')):
            raise IntegrationError(f'CLAUDE_NATIVE_LINUX_CLI_REQUIRED:{name}')
        # A connection reference is not permission to change native protection.
        if Path(command[0]).name in {"sh", "bash", "cmd", "cmd.exe", "powershell", "pwsh"} or any(a.split('=')[0] in {"--dangerously-skip-permissions", "--allow-dangerously-skip-permissions",
                     "--dangerously-bypass-approvals-and-sandbox", "--yolo"} for a in command):
            raise IntegrationError(f"UNSAFE_CONNECTION_COMMAND:{name}")
        if runtime == 'codex':
            try:
                codex_command_kind(command)
            except IntegrationError as exc:
                raise IntegrationError(f'{exc}:{name}') from None
        env = profile.get("runtime_env", config.get("runtime_env", {}) if runtime == run_runtime else {})
        if not isinstance(env, dict):
            raise IntegrationError(f"CONNECTION_ENV_INVALID:{name}")
        if set(env) - BACKENDS[runtime].environment_names:
            raise IntegrationError(f"CONNECTION_ENV_UNSUPPORTED:{name}")
        for key, value in env.items():
            _text(value, f"connection.{name}.runtime_env.{key}")
        home = env.get("CODEX_HOME") or env.get("CLAUDE_CONFIG_DIR")
        if home and (not Path(home).is_absolute() or Path(home).resolve().is_relative_to(Path(config["workspace"]).resolve())):
            raise IntegrationError(f"EXTERNAL_NATIVE_AUTH_HOME_REQUIRED:{name}")
        if runtime == "codex" and env.get("CODEX_HOME", "").startswith("/mnt/"):
            raise IntegrationError("NATIVE_AUTH_MUST_NOT_USE_WINDOWS_MOUNT")
    if default is not None and (not isinstance(default, str) or default not in profiles):
        raise IntegrationError("UNKNOWN_DEFAULT_CONNECTION")
    settings = []
    for seat in config["seats"]:
        _fields(seat, {"alias", "display_name", "role", "participant_id", "connection",
                       "model", "effort", "turn_timeout_s"}, "seat")
        name = seat.get("connection", default)
        if name is None:
            raise IntegrationError(f"SEAT_CONNECTION_REQUIRED:{seat['alias']}")
        if not isinstance(name, str) or name not in profiles:
            raise IntegrationError(f"UNKNOWN_SEAT_CONNECTION:{seat['alias']}")
        profile = profiles[name]
        runtime = profile["runtime"]
        sources = {"connection": "seat" if "connection" in seat else "run_default",
                   "runtime": "connection"}
        values = {}
        for key in ("model", "effort", "turn_timeout_s"):
            if key in seat:
                value, source = seat[key], "seat"
            elif key in profile:
                value, source = profile[key], "connection"
            elif key in config:
                if key != "turn_timeout_s" and runtime != run_runtime:
                    raise IntegrationError(f"RUNTIME_DEFAULT_MISMATCH:{seat['alias']}:{key}")
                value, source = config[key], "run"
            elif key == "turn_timeout_s":
                value, source = 3600.0, "USER_DEFAULT_3600"
            else:
                raise IntegrationError(f"SEAT_SETTING_REQUIRED:{seat['alias']}:{key}")
            values[key] = _timeout(value, seat['alias']) if key == "turn_timeout_s" else _text(value, f"seat.{seat['alias']}.{key}")
            sources[key] = source
        command = profile.get("command", config.get("codex_command"))
        env = profile.get("runtime_env", config.get("runtime_env", {}) if runtime == run_runtime else {})
        sources["command"] = "run" if legacy or "command" not in profile else "connection"
        sources["runtime_env"] = ("run" if "runtime_env" in config else "empty_default") if legacy else "connection" if "runtime_env" in profile else "run" if runtime == run_runtime and "runtime_env" in config else "empty_default"
        if result_repo is not None:
            sources['result_repo'] = 'run'
        settings.append(SeatSettings(name, runtime, str(Path(config["workspace"]).resolve()),
                                    values["model"], values["effort"], values["turn_timeout_s"],
                                    tuple(command), tuple(sorted(env.items())), tuple(sorted(sources.items())), result_repo))
    return tuple(settings)


def preflight_backends(settings):
    """Check ALL seats before any external effect, not lazily after Agent.start."""
    for effective in settings:
        BACKENDS[effective.runtime].preflight(effective)


class SerializedOwner:
    """Share B gateway's mutex, epoch and transaction; never acquire a second Store."""
    def __init__(self, owner, mutex):
        self.owner, self.mutex = owner, mutex

    def __getattr__(self, name):
        return getattr(self.owner, name)

    @contextmanager
    def transaction(self, epoch=None):
        with self.mutex:
            with self.owner.transaction(epoch) as db:
                yield db


def validate_config(config):
    required = {"run_id", "room_id", "workspace", "credentials_path", "grant_id", "seats"}
    if not isinstance(config, dict) or not required <= config.keys():
        raise IntegrationError("RUN_CONFIG_INCOMPLETE")
    for key in ("run_id", "room_id", "workspace", "credentials_path", "grant_id"):
        _text(config[key], f"run.{key}")
    workspace = Path(config["workspace"])
    if not workspace.is_absolute() or not workspace.is_dir():
        raise IntegrationError("ABSOLUTE_WORKSPACE_REQUIRED")
    if 'result_repo' in config:
        _text(config['result_repo'], 'run.result_repo')
    repo = result_repo_boundary(config['workspace'], config.get('result_repo'))
    if not repo.is_dir() or not (repo / '.git').is_dir() or (repo / '.git').is_symlink():
        raise IntegrationError("SCOPED_GIT_REPO_REQUIRED")
    credential = Path(config["credentials_path"])
    if not credential.is_absolute() or credential.resolve().is_relative_to(workspace.resolve()):
        raise IntegrationError("EXTERNAL_CREDENTIAL_PATH_REQUIRED")
    seats = config["seats"]
    if not isinstance(seats, list) or len(seats) != 3 or not all(isinstance(s, dict) for s in seats) or {s.get("role") for s in seats if isinstance(s.get("role"), str)} != {"coordinator", "builder", "reviewer"}:
        raise IntegrationError("THREE_SEAT_ROLES_REQUIRED")
    for field in ("alias", "display_name", "participant_id"):
        for seat in seats:
            _text(seat.get(field), f"seat.{field}")
        if len({s.get(field) for s in seats}) != 3:
            raise IntegrationError("SEAT_IDENTITY_CONFLICT")
    slugs = [slug(s["display_name"]) for s in seats]
    if "" in slugs or len(set(slugs)) != 3:
        raise IntegrationError("MANDATE_SLUG_CONFLICT")
    effective = resolve_settings(config)
    if type(config.get('auto_recover_settled_timeouts', False)) is not bool:
        raise IntegrationError('AUTO_RECOVERY_POLICY_INVALID')
    timeout = _timeout(config.get("turn_timeout_s", 3600.0), "run")
    return {"valid": True, "seats": 3, "credentials": "NOT_READ", "live": "NOT_STARTED", "effective_timeout_s": timeout,
            "timeout_source": "explicit_config" if "turn_timeout_s" in config else "USER_DEFAULT_3600",
            "effective_settings": [{"seat": seat["alias"], **setting.evidence(),
                                    "backend": "LOCAL_PREFLIGHT_REQUIRED",
                                    "execution_qualification": "NOT_RUN"}
                                   for seat, setting in zip(seats, effective)]}


def prepare(config, official_root, python="python3"):
    validate_config(config)
    settings = resolve_settings(config)
    # Codex prepare remains credential/dependency-free for legacy callers.
    # Unqualified optional backends must fail before writing any mandate.
    for effective in settings:
        if effective.runtime != "codex":
            BACKENDS[effective.runtime].preflight(effective)
    guard = SecretGuard.official(official_root, python)
    folder = result_repo_boundary(config['workspace'], config.get('result_repo')) / 'mandates'
    folder.mkdir(exist_ok=True)
    hashes = {}
    for seat, effective in zip(config["seats"], settings):
        text = render_mandate(seat["display_name"], seat["role"], BACKENDS[effective.runtime].harness, effective.model, effective.effort)
        guard.require_clean(text)
        path = folder / (slug(seat["display_name"]) + ".md")
        raw = text.encode("utf-8")
        if path.exists() and path.read_bytes() != raw:
            raise IntegrationError("EXISTING_MANDATE_MISMATCH")
        path.write_bytes(raw)
        hashes[seat["alias"]] = digest(raw)
    checks = mandate_checks(official_root, folder.parent, python)
    if any(checks.values()):
        raise IntegrationError("MANDATE_OFFICIAL_CHECK_FAILED")
    return {"level": "COMPONENT_PRECHECK", "snapshots": hashes, "official_mandates": checks,
            "effective_settings": [{"seat": seat["alias"], **effective.evidence()} for seat, effective in zip(config["seats"], settings)],
            "unchecked_gates": ["room teamwork", "stage execution", "post-contest gate4 review"]}


async def hydrate_room_tools(tools, context):
    if context is None:
        raise IntegrationError('ROOM_CONTEXT_NOT_READY')
    roster = await tools.get_participants()  # SDK syncs context and authoritative cache.
    ids = {p.get('id') for p in tools.participants}
    if not roster or None in ids or len(ids) != len(roster) or context.agent_id not in ids or ids != {p.get('id') for p in context.participants}:
        raise IntegrationError('ROOM_PARTICIPANTS_NOT_READY')


class SeatManager:
    def __init__(self, owner, official_root, python="python3"):
        self.owner, self.official_root, self.python = owner, official_root, python
        self.agents, self.adapters, self.runtimes = [], [], []
        self.mailbox = Mailbox(owner)
        self.mailbox.recover()
        self.config = None

    async def start(self, config):
        if self.agents:
            raise IntegrationError("RUN_ALREADY_STARTED")
        # Detach from caller-owned mutable dicts BEFORE validation/preflight.
        config = deepcopy(config)
        validate_config(config)
        settings = resolve_settings(config)
        preflight_backends(settings)
        self.pin_settings(config, settings)
        self.settings = settings
        self.adapters.clear()
        self.runtimes.clear()
        from band import Agent
        from band.config.loader import load_agent_config
        path = Path(config["credentials_path"])
        st = path.lstat()
        if not stat.S_ISREG(st.st_mode) or stat.S_IMODE(st.st_mode) != 0o600 or st.st_uid != os.getuid():
            raise IntegrationError("CREDENTIAL_FILE_0600_OWNER_REQUIRED")
        guard = SecretGuard.official(self.official_root, self.python)
        coordinator = next(s["participant_id"] for s in config["seats"] if s["role"] == "coordinator")
        self.config = config
        try:
            bindings = []
            for seat, effective in zip(config["seats"], settings):
                router = ApprovalRouter(self.owner, config["grant_id"], seat["alias"], config["room_id"], config["run_id"], config["workspace"], result_repo=effective.result_repo)
                if not router.active():
                    raise IntegrationError("CONTROLLER_GRANT_REQUIRED")
                text = render_mandate(seat["display_name"], seat["role"], BACKENDS[effective.runtime].harness, effective.model, effective.effort)
                mandate = Path(router.result_repo) / "mandates" / (slug(seat["display_name"]) + ".md")
                snapshot_check(text, mandate.read_bytes(), digest(text.encode()))
                ident, credential = load_agent_config(seat["alias"], config_path=path)
                guard.register_known(credential)
                if ident != seat["participant_id"]:
                    raise IntegrationError("CREDENTIAL_AGENT_ID_MISMATCH")
                adapter, runtime, binding = BACKENDS[effective.runtime].factory(
                    settings=effective, seat=seat, config=config, text=text,
                    mailbox=self.mailbox, router=router, guard=guard, coordinator=coordinator)
                adapter.startup_binding_pending = True
                adapter.recovery_idle_client_pids = self.idle_client_pids
                agent = Agent.create(**{"adapter": adapter, "agent_id": ident, "api_key": credential})
                self.adapters.append(adapter)
                self.runtimes.append(runtime)
                self.agents.append(agent)
                bindings.append(asdict(binding))
            # Historical timeout proof is evaluated before readiness can create
            # new native processes. No SQLite edits or synthetic Band dispatch.
            self.recover_timeouts_on_startup()
            # Credentials never leave loader/Agent; no config values in output.
            for agent in self.agents:
                await agent.start()
            ready = [await runtime.readiness() for runtime in self.runtimes]
            for agent, adapter in zip(self.agents, self.adapters):
                if await self.bind_room_tools(agent, adapter):
                    adapter._wake()
            with self.owner.transaction(self.owner.epoch) as db:
                Mailbox.event(db, None, "RUNTIME_BINDINGS", {"run_id": config["run_id"], "bindings": bindings, "readiness": ready})
            return {"started": 3, "run_id": config["run_id"], "readiness": ready, "bindings": bindings}
        except BaseException:
            await self.stop()
            raise

    def pin_settings(self, config, settings):
        """Run-level immutable execution meaning, including across restarts.

        Only hashes/seat identity enter the store, never native env or auth homes.
        An origin-only change is equivalent; a different binding needs a new run.
        """
        body = encode({'room': config['room_id'], 'seats': sorted(
            [{'alias': seat['alias'], 'display_name': seat['display_name'],
              'participant_id': seat['participant_id'], 'role': seat['role'],
              'settings_sha256': effective.fingerprint}
             for seat, effective in zip(config['seats'], settings)], key=lambda s: s['alias'])})
        fingerprint = digest(body.encode())
        with self.owner.transaction(self.owner.epoch) as db:
            db.execute('CREATE TABLE IF NOT EXISTS c_run_settings(run_id TEXT PRIMARY KEY, body TEXT NOT NULL, hash TEXT NOT NULL)')
            prior = db.execute('SELECT body,hash FROM c_run_settings WHERE run_id=?', (config['run_id'],)).fetchone()
            if prior and (prior['body'] != body or prior['hash'] != fingerprint):
                raise IntegrationError('RUN_SETTINGS_PIN_MISMATCH:new_run_required')
            db.execute('INSERT OR IGNORE INTO c_run_settings VALUES(?,?,?)', (config['run_id'], body, fingerprint))
        return fingerprint

    def idle_client_pids(self):
        from .codex import OwnedStdioClient, group_members
        found = set()
        for adapter in self.adapters:
            if adapter.worker is not None and not adapter.worker.done():
                continue
            for state in getattr(adapter, '_room_clients', {}).values():
                client = state.client
                if isinstance(client, OwnedStdioClient) and getattr(client, 'evidence', None) and client.evidence.context is None and client.group is not None:
                    found.update(identity[0] for identity in group_members(client.group))
        return found

    def recover_timeouts_on_startup(self):
        results = []
        for adapter in self.adapters:
            if not getattr(adapter, 'auto_recover_settled_timeouts', False):
                continue
            from .codex_timeout import CodexTimeoutRecovery
            recovery = CodexTimeoutRecovery(self.mailbox, adapter.router, adapter.git_broker)
            with self.owner.transaction(self.owner.epoch) as db:
                if not recovery.automatic_allowed(db):
                    continue
                candidates = [dict(r) for r in db.execute("SELECT w.id,w.attempt FROM c_work w LEFT JOIN c_recovery r ON r.operation=w.id AND r.attempt=w.attempt WHERE w.seat=? AND w.room=? AND w.delivery IN ('DELIVERY_UNKNOWN','RECONCILED') AND w.state IN ('PAUSED','FAILED') AND r.continuation IS NULL ORDER BY w.rowid", (adapter.alias, adapter.allowed_room))]
            for work in candidates:
                try:
                    result = recovery.recover(work['id'], work['attempt'])
                    results.append(result)
                    self.mailbox.observe(work['id'], work['attempt'], 'STARTUP_TIMEOUT_RECOVERY', result)
                except IntegrationError as exc:
                    self.mailbox.observe(work['id'], work['attempt'], 'TIMEOUT_RECOVERY_BLOCKED', {'code': str(exc), 'phase': 'startup'})
        return results

    async def bind_room_tools(self, agent, adapter):
        # Pinned SDK4 room execution supplies real platform tools. Do not invent
        # a MessageEvent/new human dispatch merely to recreate a cleared job.
        from band.runtime.tools import AgentTools
        context = agent._runtime.runtime.executions.get(adapter.allowed_room)
        if context is None:
            return False
        tools = AgentTools.from_context(context)
        await hydrate_room_tools(tools, context)
        adapter.raw_tools = tools
        adapter.startup_binding_pending = False
        return True

    async def wake_continuation(self, operation):
        work = self.mailbox.read_work(operation)
        for agent, adapter in zip(self.agents, self.adapters):
            if adapter.alias == work['seat'] and not adapter.stopping:
                if not await self.bind_room_tools(agent, adapter):
                    raise IntegrationError('ROOM_CONTEXT_NOT_READY')
                adapter._wake()
                return {'continuation': operation, 'wake': 'AUTHORIZED_QUEUE_DRAIN'}
        raise IntegrationError('SEAT_NOT_RUNNING')

    async def stop(self):
        errors = []
        for adapter in self.adapters:
            adapter.stopping = True
        for agent in reversed(self.agents):
            try:
                await agent.stop()
            except BaseException as exc:
                errors.append(type(exc).__name__)
        for adapter in self.adapters:
            try:
                await adapter.on_cleanup(adapter.allowed_room)
            except BaseException as exc:
                errors.append(type(exc).__name__)
        self.agents.clear()
        return {"stopped": not errors, "errors": errors}


def _deny_room():
    raise IntegrationError("ROOM_BINDING_DENIED")


def _native_environment(settings, actor):
    env = dict(settings.runtime_env)
    env.update({"GIT_AUTHOR_NAME": actor, "GIT_COMMITTER_NAME": actor,
                "GIT_AUTHOR_EMAIL": slug(actor) + "@actors.invalid",
                "GIT_COMMITTER_EMAIL": slug(actor) + "@actors.invalid"})
    return env


def codex_sdk_config(settings, room, text, actor):
    from band.adapters.codex import CodexAdapterConfig
    class ControllerCodexConfig(CodexAdapterConfig):
        @classmethod
        def settings_customise_sources(cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings):
            # Connection profiles, not ambient CODEX_* or .env/secrets, own the
            # native binding. Never read secret settings during validation.
            return (init_settings,)
    return ControllerCodexConfig(model=settings.model, reasoning_effort=settings.effort,
        workspace_for_room=lambda actual: settings.workspace if actual == room else _deny_room(),
        sandbox="workspace-write", approval_policy="on-request", approval_mode="manual",
        system_prompt=text, include_base_instructions=False, enable_self_config_tools=False,
        codex_command=settings.command, codex_env=_native_environment(settings, actor),
        turn_timeout_s=settings.turn_timeout_s, inject_history_on_resume_failure=False,
        emit_turn_lifecycle_events=True, emit_diff_events=True, emit_token_usage_events=True)


def _codex_preflight(settings):
    from importlib.metadata import PackageNotFoundError, version
    import shutil
    try:
        if version("band-sdk") != "4.0.0":
            raise IntegrationError("BAND_SDK_VERSION_UNSUPPORTED")
        # Validate against real installed SDK types before reading credentials.
        codex_sdk_config(settings, "preflight", "", "preflight")
    except (ImportError, PackageNotFoundError):
        raise IntegrationError("CODEX_BACKEND_DEPENDENCY_MISSING:band-sdk==4.0.0") from None
    except ValueError:
        raise IntegrationError("CODEX_BACKEND_SETTINGS_INVALID") from None
    if not shutil.which(settings.command[0], path=dict(settings.runtime_env).get("PATH")):
        raise IntegrationError("CODEX_COMMAND_UNAVAILABLE")
    if codex_command_kind(list(settings.command)) == 'official-node-entrypoint':
        entrypoint = Path(settings.command[1])
        if not entrypoint.is_file():
            raise IntegrationError('CODEX_NODE_ENTRYPOINT_UNAVAILABLE')
        try:
            package = json.loads((entrypoint.parent.parent / 'package.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            raise IntegrationError('CODEX_NODE_PACKAGE_IDENTITY_UNAVAILABLE') from None
        if not isinstance(package, dict) or package.get('name') != '@openai/codex' or package.get('version') != '0.159.3' or not isinstance(package.get('bin'), dict) or package['bin'].get('codex') != 'bin/codex.js':
            raise IntegrationError('CODEX_NODE_PACKAGE_IDENTITY_UNSUPPORTED')


def _codex_factory(*, settings, seat, config, text, mailbox, router, guard, coordinator):
    if settings.result_repo != getattr(router, 'configured_result_repo', None):
        raise IntegrationError('RESULT_REPO_BINDING_MISMATCH')
    from .codex import CodexRuntime, DurableCodexAdapter
    sdk_config = codex_sdk_config(settings, config["room_id"], text, seat["display_name"])
    adapter = DurableCodexAdapter(mailbox=mailbox, router=router, guard=guard,
        alias=seat["alias"], display_name=seat["display_name"], room_id=config["room_id"],
        workspace=settings.workspace, coordinator_id=coordinator, config=sdk_config,
        receipt_run_resolver=execution_ledger_run,
        auto_recover_settled_timeouts=config.get("auto_recover_settled_timeouts", False))
    adapter.effective_settings = settings
    adapter.thread_ownership.binding = settings.fingerprint
    source = dict(settings.sources)["turn_timeout_s"]
    adapter.turn_timeout_source = "explicit_config" if source == "run" else source
    binding = RuntimeBinding(settings.runtime, "0.159.3", settings.workspace,
        "workspace-write", "on-request", "native-controlled",
        ("same-UID credential access possible", "Docker/interop not an isolation boundary",
         "privileged shell wrappers denied", "SDK platform ACK is not exactly-once"),
        settings.turn_timeout_s, digest(text.encode()), settings.connection,
        settings.model, settings.effort, settings.fingerprint, settings.sources, settings.result_repo)
    adapter.binding = binding
    return adapter, CodexRuntime(adapter), binding


def claude_sdk_config(settings, text, actor):
    """Real Band SDK4 option translation, NOT an execution authorization.

    No fallback_model, host plugins/settings or API-key environment is enabled.
    A durable protected Claude bridge is still required before this may execute.
    """
    from band.adapters.claude_sdk import ClaudeCLIOptions, ClaudePermissionMode, ClaudeSDKAdapterConfig
    return ClaudeSDKAdapterConfig(model=settings.model, effort=settings.effort,
        cwd=settings.workspace, custom_section=text, fallback_model=None,
        permission_mode=ClaudePermissionMode.DEFAULT, setting_sources=(),
        turn_timeout_s=settings.turn_timeout_s,
        cli=ClaudeCLIOptions(cli_path=settings.command[0], env=_native_environment(settings, actor)))


def _claude_preflight(settings):
    from importlib.util import find_spec
    from importlib.metadata import PackageNotFoundError, version
    try:
        if version("band-sdk") != "4.0.0":
            raise IntegrationError("BAND_SDK_VERSION_UNSUPPORTED")
        for module in ('claude_agent_sdk', 'mcp'):
            if find_spec(module) is None:
                raise IntegrationError(f'CLAUDE_BACKEND_DEPENDENCY_MISSING:{module}')
        if version('claude-agent-sdk') != '0.2.163' or version('mcp') != '1.30.0':
            raise IntegrationError('CLAUDE_DEPENDENCY_VERSION_UNSUPPORTED:claude-agent-sdk==0.2.163/mcp==1.30.0')
        claude_sdk_config(settings, "", "preflight")
    except (ImportError, PackageNotFoundError):
        raise IntegrationError("CLAUDE_BACKEND_DEPENDENCY_MISSING:band-sdk/claude_agent_sdk") from None
    except ValueError:
        raise IntegrationError("CLAUDE_BACKEND_SETTINGS_INVALID") from None
    import shutil
    from .claude import dependency_check
    dependency_check()
    if not shutil.which(settings.command[0], path=dict(settings.runtime_env).get('PATH')):
        raise IntegrationError('CLAUDE_COMMAND_UNAVAILABLE')


def _claude_factory(*, settings, seat, config, text, mailbox, router, guard, coordinator):
    if settings.result_repo != getattr(router, 'configured_result_repo', None):
        raise IntegrationError('RESULT_REPO_BINDING_MISMATCH')
    from .claude import DurableClaudeAdapter, ClaudeRuntime
    binding = RuntimeBinding(settings.runtime, 'UNQUALIFIED', settings.workspace,
        'workspace-scoped-native-tools', 'controller-grant/on-request', 'native-controlled',
        ('same-UID credential access possible', 'native CLI execution qualification NOT_RUN',
         'SDK platform ACK is not exactly-once', 'optional Linux transport and SDK versions pinned'),
        settings.turn_timeout_s, digest(text.encode()), settings.connection, settings.model,
        settings.effort, settings.fingerprint, settings.sources, settings.result_repo)
    adapter = DurableClaudeAdapter(settings=settings, binding=binding,
        config=claude_sdk_config(settings, text, seat['display_name']), mailbox=mailbox,
        router=router, guard=guard, alias=seat['alias'], display_name=seat['display_name'],
        room_id=config['room_id'], coordinator_id=coordinator,
        receipt_run_resolver=execution_ledger_run)
    return adapter, ClaudeRuntime(adapter), binding


# Only trusted controller code may extend this registry. JSON selects a name;
# it cannot supply a callable, Python import, permission policy or plugin.
BACKENDS = {
    "codex": BackendRegistration("Codex (DarkHarness Band SDK adapter)", "0.159.3",
        frozenset({"PATH", "CODEX_HOME"}), _codex_preflight, _codex_factory),
    "claude_code": BackendRegistration("DarkHarness (Band SDK Claude Code)", "UNQUALIFIED",
        frozenset({"PATH", "CLAUDE_CONFIG_DIR"}), _claude_preflight, _claude_factory),
}
