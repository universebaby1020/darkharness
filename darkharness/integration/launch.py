"""Three-seat run configuration. Credentials are loaded only in explicit start()."""
from __future__ import annotations

from contextlib import contextmanager
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
    required = {"run_id", "room_id", "workspace", "credentials_path", "grant_id", "model", "effort", "codex_command", "seats"}
    if not isinstance(config, dict) or not required <= config.keys():
        raise IntegrationError("RUN_CONFIG_INCOMPLETE")
    workspace = Path(config["workspace"])
    if not workspace.is_absolute() or not workspace.is_dir():
        raise IntegrationError("ABSOLUTE_WORKSPACE_REQUIRED")
    if not (workspace / ".git").exists():
        raise IntegrationError("SCOPED_GIT_REPO_REQUIRED")
    credential = Path(config["credentials_path"])
    if not credential.is_absolute() or credential.resolve().is_relative_to(workspace.resolve()):
        raise IntegrationError("EXTERNAL_CREDENTIAL_PATH_REQUIRED")
    seats = config["seats"]
    if not isinstance(seats, list) or len(seats) != 3 or {s.get("role") for s in seats} != {"coordinator", "builder", "reviewer"}:
        raise IntegrationError("THREE_SEAT_ROLES_REQUIRED")
    for field in ("alias", "display_name", "participant_id"):
        if len({s.get(field) for s in seats}) != 3 or not all(s.get(field) for s in seats):
            raise IntegrationError("SEAT_IDENTITY_CONFLICT")
    slugs = [slug(s["display_name"]) for s in seats]
    if "" in slugs or len(set(slugs)) != 3:
        raise IntegrationError("MANDATE_SLUG_CONFLICT")
    if not config["model"] or not config["effort"] or not isinstance(config["codex_command"], list) or not config["codex_command"]:
        raise IntegrationError("RUNTIME_SELECTION_REQUIRED")
    if type(config.get('auto_recover_settled_timeouts', False)) is not bool:
        raise IntegrationError('AUTO_RECOVERY_POLICY_INVALID')
    timeout = config.get("turn_timeout_s", 3600.0)
    if type(timeout) not in {int, float} or not math.isfinite(timeout) or timeout <= 0:
        raise IntegrationError("TIMEOUT_INVALID")
    return {"valid": True, "seats": 3, "credentials": "NOT_READ", "live": "NOT_STARTED", "effective_timeout_s": timeout,
            "timeout_source": "explicit_config" if "turn_timeout_s" in config else "USER_DEFAULT_3600"}


def prepare(config, official_root, python="python3"):
    validate_config(config)
    guard = SecretGuard.official(official_root, python)
    folder = Path(config["workspace"]) / "mandates"
    folder.mkdir(exist_ok=True)
    hashes = {}
    for seat in config["seats"]:
        text = render_mandate(seat["display_name"], seat["role"], "DarkHarness (Band SDK Codex)", config["model"], config["effort"])
        guard.require_clean(text)
        path = folder / (slug(seat["display_name"]) + ".md")
        raw = text.encode("utf-8")
        if path.exists() and path.read_bytes() != raw:
            raise IntegrationError("EXISTING_MANDATE_MISMATCH")
        path.write_bytes(raw)
        hashes[seat["alias"]] = digest(raw)
    checks = mandate_checks(official_root, config["workspace"], python)
    if any(checks.values()):
        raise IntegrationError("MANDATE_OFFICIAL_CHECK_FAILED")
    return {"level": "COMPONENT_PRECHECK", "snapshots": hashes, "official_mandates": checks,
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
        validate_config(config)
        self.adapters.clear()
        self.runtimes.clear()
        from band import Agent
        from band.config.loader import load_agent_config
        from band.adapters.codex import CodexAdapterConfig
        from .codex import CodexRuntime, DurableCodexAdapter
        from .contract import RuntimeBinding
        path = Path(config["credentials_path"])
        st = path.lstat()
        if not stat.S_ISREG(st.st_mode) or stat.S_IMODE(st.st_mode) != 0o600 or st.st_uid != os.getuid():
            raise IntegrationError("CREDENTIAL_FILE_0600_OWNER_REQUIRED")
        guard = SecretGuard.official(self.official_root, self.python)
        coordinator = next(s["participant_id"] for s in config["seats"] if s["role"] == "coordinator")
        self.config = config
        try:
            bindings = []
            for seat in config["seats"]:
                router = ApprovalRouter(self.owner, config["grant_id"], seat["alias"], config["room_id"], config["run_id"], config["workspace"])
                if not router.active():
                    raise IntegrationError("CONTROLLER_GRANT_REQUIRED")
                text = render_mandate(seat["display_name"], seat["role"], "DarkHarness (Band SDK Codex)", config["model"], config["effort"])
                mandate = Path(config["workspace"]) / "mandates" / (slug(seat["display_name"]) + ".md")
                snapshot_check(text, mandate.read_bytes(), digest(text.encode()))
                ident, credential = load_agent_config(seat["alias"], config_path=path)
                guard.register_known(credential)
                if ident != seat["participant_id"]:
                    raise IntegrationError("CREDENTIAL_AGENT_ID_MISMATCH")
                env = {name: value for name, value in config.get("runtime_env", {}).items() if name in {"PATH", "CODEX_HOME"}}
                # Credentials remain Band-side; native Codex uses official login.
                if env.get("CODEX_HOME", "").startswith("/mnt/"):
                    raise IntegrationError("NATIVE_AUTH_MUST_NOT_USE_WINDOWS_MOUNT")
                actor = seat["display_name"]
                env.update({"GIT_AUTHOR_NAME": actor, "GIT_COMMITTER_NAME": actor,
                            "GIT_AUTHOR_EMAIL": slug(actor) + "@actors.invalid",
                            "GIT_COMMITTER_EMAIL": slug(actor) + "@actors.invalid"})
                sdk_config = CodexAdapterConfig(model=config["model"], reasoning_effort=config["effort"],
                       workspace_for_room=lambda room, expected=config["room_id"], repo=config["workspace"]: repo if room == expected else _deny_room(),
                       sandbox="workspace-write", approval_policy="on-request", approval_mode="manual",
                       system_prompt=text, include_base_instructions=False, enable_self_config_tools=False,
                       codex_command=tuple(config["codex_command"]), codex_env=env,
                       turn_timeout_s=config.get("turn_timeout_s", 3600.0),
                       inject_history_on_resume_failure=False, emit_turn_lifecycle_events=True,
                       emit_diff_events=True, emit_token_usage_events=True)
                adapter = DurableCodexAdapter(mailbox=self.mailbox, router=router, guard=guard,
                     alias=seat["alias"], display_name=actor, room_id=config["room_id"],
                     workspace=config["workspace"], coordinator_id=coordinator, config=sdk_config,
                     receipt_run_resolver=execution_ledger_run,
                     auto_recover_settled_timeouts=config.get('auto_recover_settled_timeouts', False))
                adapter.turn_timeout_source = "explicit_config" if "turn_timeout_s" in config else "USER_DEFAULT_3600"
                adapter.startup_binding_pending = True
                adapter.recovery_idle_client_pids = self.idle_client_pids
                binding = RuntimeBinding("codex", "0.159.3", config["workspace"], "workspace-write", "on-request",
                      "native-controlled", ("same-UID credential access possible", "Docker/interop not an isolation boundary", "privileged shell wrappers denied", "SDK platform ACK is not exactly-once"),
                      sdk_config.turn_timeout_s, digest(text.encode()))
                agent = Agent.create(**{"adapter": adapter, "agent_id": ident, "api_key": credential})
                self.adapters.append(adapter)
                self.runtimes.append(CodexRuntime(adapter))
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

    def idle_client_pids(self):
        from .codex import OwnedStdioClient, group_members
        found = set()
        for adapter in self.adapters:
            if adapter.worker is not None and not adapter.worker.done():
                continue
            for state in adapter._room_clients.values():
                client = state.client
                if isinstance(client, OwnedStdioClient) and getattr(client, 'evidence', None) and client.evidence.context is None and client.group is not None:
                    found.update(identity[0] for identity in group_members(client.group))
        return found

    def recover_timeouts_on_startup(self):
        results = []
        for adapter in self.adapters:
            if not adapter.auto_recover_settled_timeouts:
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
