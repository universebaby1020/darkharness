"""Pinned Band SDK 4.0.0 extension, not a replacement runtime.

Private extension seams are listed in SUPPORT.md. Installed SDK is never patched.
Native effects are native-controlled; observed events are not retroactive authority.
"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import asdict, replace
from datetime import datetime, timezone
from importlib.metadata import version
import json
import os
from pathlib import Path
import re
import shlex
import signal
import uuid

from band.adapters.codex import CodexAdapter, CodexAdapterConfig, strip_leading_mentions
from band.core.protocols import to_failure_event, TurnResultAlreadyReported
from band.core.types import PlatformMessage
from band.integrations.codex.stdio_client import CodexStdioClient
from band.integrations.codex.types import CodexSessionState
from band.runtime.tools.schema import ToolCallOutcome, serialize_tool_result

from .artifacts import git_evidence, slug
from .git_broker import LocalGitBroker
from .verification import VerificationBroker
from .verification_bridge import VerificationBridge, VerificationYield
from .thread_ownership import ThreadOwnership
from .contract import PermissionRequest, RuntimeBinding, RuntimeEvent
from .mailbox import HandoffPart, IntegrationError, Mailbox, digest, encode


class PeerYield(BaseException):
    """Internal control signal: intentionally bypasses SDK generic-error fallback."""


def process_identity(pid):
    stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return (int(pid), Path("/proc/sys/kernel/random/boot_id").read_text().strip(), stat[19])


def group_members(group):
    found = []
    for p in Path("/proc").iterdir():
        if p.name.isdigit():
            try:
                stat = (p / "stat").read_text().rsplit(")", 1)[1].split()
                if int(stat[2]) == group and stat[0] != "Z":
                    found.append(process_identity(int(p.name)))
            except (OSError, ValueError, ProcessLookupError):
                continue
    return found


class ClientEvidence:
    """One client belongs to at most one attempt, independent of reader ContextVars."""
    def __init__(self, adapter, context=None):
        self.adapter, self.context = adapter, context
        self.client_id = str(uuid.uuid4())

    def bind(self, context):
        if self.context is not None and self.context != context:
            raise IntegrationError('CLIENT_ATTEMPT_REBIND_DENIED')
        self.context = context

    def record(self, kind, data):
        self.adapter._record_for(self.context, kind, {'payload': data, 'client_id': self.client_id})


class OwnedStdioClient(CodexStdioClient):
    """Reuses SDK framing/RPC; owns a fresh Linux process group and captures I/O."""
    def __init__(self, *, record, guard, **kwargs):
        super().__init__(**kwargs)
        self.record, self.guard = record, guard
        self.identity = None
        self.group = None

    async def connect(self):
        if self._connected:
            return
        if os.name != "posix":
            raise IntegrationError("LINUX_RUNTIME_REQUIRED")
        # Independent extension of process creation, not a copied SDK implementation.
        self._proc = await asyncio.create_subprocess_exec(*self.command, cwd=self.cwd,
                       env=self.env, stdin=asyncio.subprocess.PIPE,
                       stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                       start_new_session=True, limit=16 * 1024 * 1024)
        self.identity = process_identity(self._proc.pid)
        self.group = os.getpgid(self._proc.pid)
        if self.group != self._proc.pid:
            raise IntegrationError("PROCESS_GROUP_OWNERSHIP_FAILED")
        self._connected = True
        self.record("PROCESS_STARTED", {"identity": self.identity, "group": self.group, "cwd": self.cwd})
        self._reader_task = asyncio.create_task(self._read_stdout_loop())
        self._stderr_task = asyncio.create_task(self._read_stderr_loop())

    async def _dispatch_rpc_message(self, raw):
        # The SDK remains the only RPC callback dispatcher/read owner.
        try:
            body = json.loads(raw)
        except ValueError:
            body = {"invalid_frame_sha256": digest(raw.encode())}
        self.record("STDOUT_RPC", self.guard.sanitize(body))
        return await super()._dispatch_rpc_message(raw)

    async def _send_json(self, payload):
        self.record("STDIN_RPC", self.guard.sanitize(payload))
        return await super()._send_json(payload)

    async def _read_stderr_loop(self):
        while self._proc and self._proc.stderr:
            line = await self._proc.stderr.readline()
            if not line:
                break
            self.record("STDERR", {"text": self.guard.redact(line.decode("utf-8", "replace"))})

    async def close(self):
        if self.group is not None:
            members = group_members(self.group)
            if members:
                # Refuse PID reuse. If leader exited but descendants remain, group
                # identity cannot be proved by the original leader; retain UNKNOWN.
                try:
                    same = process_identity(self.identity[0]) == self.identity
                except OSError:
                    same = False
                if not same:
                    self.record("PROCESS_STOP_UNKNOWN", {"members": members})
                    raise IntegrationError("PROCESS_OWNERSHIP_UNKNOWN")
                os.killpg(self.group, signal.SIGTERM)
                await asyncio.sleep(0)
                if group_members(self.group):
                    os.killpg(self.group, signal.SIGKILL)
        await super().close()
        members = group_members(self.group) if self.group is not None else []
        self.record("PROCESS_STOPPED" if not members else "PROCESS_STOP_UNKNOWN", {"members": members})
        if members:
            raise IntegrationError("PROCESS_TERMINATION_UNKNOWN")


from .protected_tools import GuardedTools, LocalSendRejected, validate_local_send, READ_TOOLS, SEND_TOOLS, GIT_TOOLS


from .durable import DurableSeatMixin


class DurableCodexAdapter(DurableSeatMixin, CodexAdapter):
    def __init__(self, *, mailbox, router, guard, alias, display_name, room_id, workspace,
                 coordinator_id, config, event_sink=None, report_parsers=None, receipt_run_resolver=None,
                 auto_recover_settled_timeouts=False):
        if version("band-sdk") != "4.0.0":
            raise IntegrationError("SDK_VERSION_UNSUPPORTED")
        if config.cwd is not None or config.approval_policy != "on-request" or config.sandbox != "workspace-write" or config.sandbox_policy is not None or config.enable_self_config_tools:
            raise IntegrationError("UNSAFE_RUNTIME_CONFIGURATION")
        if not config.system_prompt or not config.model or not config.reasoning_effort or not config.workspace_for_room:
            raise IntegrationError("EXPLICIT_RUNTIME_CONFIG_REQUIRED")
        super().__init__(config=config)
        self.mailbox, self.router, self.guard = mailbox, router, guard
        self.git_broker = LocalGitBroker(mailbox, router, display_name, slug(display_name) + '@actors.invalid', workspace)
        self.verification = VerificationBridge(mailbox, router, guard, report_parsers=report_parsers, receipt_run_resolver=receipt_run_resolver)
        self.thread_ownership = ThreadOwnership(mailbox, router)
        self._verification_effect = None
        self._cutover_history = None
        self.alias, self.display_name = alias, display_name
        self.allowed_room, self.workspace = room_id, str(Path(workspace).resolve())
        self.coordinator_id = coordinator_id  # Actual participant ID, not alias/slug.
        self.event_sink = event_sink
        self.current = ContextVar("dh_current", default=None)
        self.worker = None
        self.raw_tools = None
        self.startup_binding_pending = False
        self.history = CodexSessionState()
        self._turn_identity = None
        self._yielded = False
        self._sdk_outcome = None
        self._answered_callbacks = set()
        self._events = asyncio.Queue()
        self.stopping = False
        self.auto_recover_settled_timeouts = auto_recover_settled_timeouts
        self.recovery_idle_client_pids = lambda: set()

    def recover_settled_timeout(self, operation, attempt):
        from .codex_timeout import CodexTimeoutRecovery
        recovery = CodexTimeoutRecovery(self.mailbox, self.router, self.git_broker, self.recovery_idle_client_pids)
        with self.mailbox.owner.transaction(self.mailbox.owner.epoch) as db:
            auto = self.auto_recover_settled_timeouts and not self.stopping and recovery.automatic_allowed(db)
        return recovery.recover(operation, attempt) if auto else recovery.reconcile(operation, attempt)

    def _build_client(self, config):
        state = self._require_active_client_state()
        if str(Path(state.workspace).resolve()) != self.workspace:
            raise IntegrationError("WORKSPACE_BINDING_MISMATCH")
        evidence = ClientEvidence(self, self.current.get())
        client = OwnedStdioClient(command=config.codex_command, cwd=state.workspace,
                                 env=config.codex_env, record=evidence.record, guard=self.guard)
        client.evidence = evidence
        return client

    async def _drain(self):
        while not self.stopping:
            work = self.mailbox.next_ready(self.alias)
            if work is None:
                return
            attempt = str(uuid.uuid4())
            self.mailbox.claim(work["id"], attempt)
            token = self.current.set((work["id"], attempt))
            self._active_room.set(self.allowed_room)
            room_state = self._room_client(self.allowed_room)
            # Never select a sender-unfiltered per-message SDK thread.
            # Latest verified own thread is durable across native client retirement.
            if room_state.client is not None and hasattr(room_state.client, 'evidence'):
                room_state.client.evidence.bind((work['id'], attempt))
            self._yielded = False
            self._sdk_outcome = None
            envelope = json.loads(work["input"])
            msg = PlatformMessage(id=envelope.get("id", work["id"]), room_id=self.allowed_room,
                  content=encode({'original_task': envelope['content'], 'peer_answers': envelope.get('peer_answers', []), 'recovery_receipt': envelope.get('recovery_receipt')}) if envelope.get('peer_answers') or envelope.get('recovery_receipt') else envelope['content'], sender_id=envelope.get("sender_id", self.coordinator_id),
                  sender_type=envelope.get("sender_type", "agent"), sender_name=envelope.get("sender_name"),
                  message_type="text", metadata=envelope.get("metadata", {}),
                  created_at=datetime.fromisoformat(envelope.get("created_at", datetime.now(timezone.utc).isoformat())))
            tools = GuardedTools(self.raw_tools, self, work["id"], attempt)
            owned = self.thread_ownership.latest(work['thread'])
            thread = owned['thread'] if owned else None
            history = CodexSessionState(thread_id=thread, room_id=self.allowed_room)
            self._verification_effect = None
            self._cutover_history = None
            try:
                # Call actual SDK turn runner. Slash commands deliberately disabled:
                # seat messages cannot change sandbox/model/grants or resolve asks.
                await super()._run_turn(msg=msg, tools=tools, history=history,
                      participants_msg=envelope.get("participants_msg"), contacts_msg=envelope.get("contacts_msg"),
                      is_session_bootstrap=True, room_id=self.allowed_room, command=None)
                await self._retire_owned_client()
                evidence = self.guard.sanitize(git_evidence(self.workspace))
                self._record("GIT_RESULT", evidence)
                outcome = self._sdk_outcome or "failed"
                # Runtime turn completed is NOT parent WorkItem acceptance.
                state = {"completed": "SUCCEEDED", "interrupted": "CANCELLED"}.get(outcome, "FAILED")
                self.mailbox.update(work["id"], attempt, state=state, delivery="RETURNED", result={"runtime_status": outcome, "git": evidence, "acceptance": "NOT_EVALUATED"})
            except VerificationYield as yielded:
                # Model turn is gone; the work remains RUNNING/STARTED while
                # the owned trusted checker runs outside SDK's turn timeout.
                await self._retire_owned_client()
                self._record('VERIFICATION_WAIT', {'effect': yielded.effect_id, 'sdk_turn_timeout_seconds': self.config.turn_timeout_s, 'checker_wait': 'OWNED_ASYNC_NO_DEFAULT_BUDGET'})
                try:
                    await self.verification.broker.wait(work['id'], attempt, yielded.effect_id)
                    child = self.verification.complete(work['id'], attempt, yielded.effect_id)
                    if child is None:
                        self.mailbox.update(work['id'], attempt, state='PAUSED', delivery='DELIVERY_UNKNOWN', result={'verification_effect': yielded.effect_id, 'replay': 'FENCED'})
                except asyncio.CancelledError:
                    self.mailbox.update(work['id'], attempt, state='PAUSED', delivery='DELIVERY_UNKNOWN')
                    raise
                except BaseException as exc:
                    self.mailbox.update(work['id'], attempt, state='PAUSED', delivery='DELIVERY_UNKNOWN', result={'verification_effect': yielded.effect_id, 'replay': 'FENCED'})
                    self._record('VERIFICATION_COMPLETION_UNKNOWN', {'effect': yielded.effect_id, 'code': type(exc).__name__})
                    return
            except PeerYield:
                self._record("TURN_YIELDED", {"peer": self.coordinator_id})
            except asyncio.CancelledError:
                self.mailbox.update(work["id"], attempt, state="PAUSED", delivery="DELIVERY_UNKNOWN")
                raise
            except BaseException as exc:
                # Failure return may leave native effects: fence, don't auto replay.
                self.mailbox.update(work["id"], attempt, state="PAUSED", delivery="DELIVERY_UNKNOWN", result={"code": type(exc).__name__})
                self._record("RUNTIME_ERROR", {"code": type(exc).__name__, "diagnostic": self.guard.redact(str(exc)), "replay": "FENCED"})
                if isinstance(exc, TurnResultAlreadyReported):
                    try:
                        # The SDK exception is only a candidate. Retire the owned
                        # client first; canonical terminal/effect/readback proof
                        # must pass before any status refinement or continuation.
                        await self._retire_owned_client()
                        recovered = self.recover_settled_timeout(work['id'], attempt)
                        self._record('TIMEOUT_RECOVERY_RESULT', recovered)
                        if recovered.get('id'):
                            continue
                    except IntegrationError as blocked:
                        self._record('TIMEOUT_RECOVERY_BLOCKED', {'code': str(blocked)})
                return
            finally:
                try:
                    await self._retire_owned_client()
                except BaseException as exc:
                    self._record('PROCESS_STOP_UNKNOWN', {'code': type(exc).__name__})
                self.current.reset(token)

    async def _retire_owned_client(self):
        # Readiness may pre-create a client. Once bound, its raw stream is never
        # relabelled for the next attempt. Resume uses the durable thread id.
        state = self._active_client_state()
        if state and isinstance(state.client, OwnedStdioClient):
            await state.client.close()
            state.client = None
            state.initialized = False
            self._room_threads.pop(self.allowed_room, None)

    async def _ensure_thread(self, *, room_id, history, tools, is_session_bootstrap):
        # Implementation revision is provenance, not native schema compatibility.
        sources = {name: digest((Path(__file__).parent / name).read_bytes()) for name in ('codex.py', 'durable.py', 'protected_tools.py', 'runtime.py', 'git_broker.py', 'recovery.py', 'verification.py', 'verification_bridge.py', 'thread_ownership.py')}
        prompt_hash = digest(self.config.system_prompt.encode())
        fingerprint = digest(encode({'tools': self._build_dynamic_tools(tools), 'mandate': prompt_hash,
                                     'binding': getattr(getattr(self, 'effective_settings', None), 'fingerprint', None)}).encode())
        owned = self.thread_ownership.latest(history.thread_id)
        old_thread = owned['thread'] if owned else None
        cutover = bool(owned and owned['compatibility'] != fingerprint)
        self._room_threads.pop(room_id, None)
        # Every new transport explicitly resumes our own verified latest thread.
        # No ThreadResume.history hack and no sender-unfiltered SDK history.
        history = CodexSessionState(thread_id=old_thread if not cutover else None, room_id=room_id)
        if cutover:
            prior = await self._client.request('thread/read', {'threadId': old_thread, 'includeTurns': True}, retry_on_overload=False)
            self.guard.require_clean(self.guard.sanitize(prior))
            self._cutover_history = self.thread_ownership.history(self.current.get()[0], self.guard.sanitize(prior))
            self._record('THREAD_TOOLSET_CUTOVER_INTENT', {'old_thread': old_thread, 'fingerprint': fingerprint, 'history_ref': self._cutover_history[1], 'reason': 'SDK4_resume_cannot_refresh_dynamicTools'})
        elif owned is None:
            # Necessary migration/cutover from unverified legacy metadata: keep
            # original durable own tasks without claiming legacy thread ownership.
            self._cutover_history = self.thread_ownership.history(self.current.get()[0])
        # Room-scoped SDK prompt cache must never suppress a fresh thread mandate.
        self._prompt_injected_rooms.discard(room_id)
        if owned and not cutover and owned['prompt_hash'] == prompt_hash:
            self._prompt_injected_rooms.add(room_id)
        thread = await super()._ensure_thread(room_id=room_id, history=history, tools=tools, is_session_bootstrap=True)
        if thread != old_thread or cutover:
            self._prompt_injected_rooms.discard(room_id)
            if owned and not cutover:
                # Resume fallback is a new thread; preserve owned prior history.
                prior = await self._client.request('thread/read', {'threadId': old_thread, 'includeTurns': True}, retry_on_overload=False)
                self._cutover_history = self.thread_ownership.history(self.current.get()[0], self.guard.sanitize(prior))
        self.thread_ownership.bind(thread, fingerprint)
        with self.mailbox.owner.transaction(self.mailbox.owner.epoch) as db:
            db.execute('INSERT OR REPLACE INTO c_thread_tools VALUES(?,?)', (thread, fingerprint))
        self._record('THREAD_TOOLSET_BOUND', {'thread': thread, 'old_thread': old_thread, 'fingerprint': fingerprint, 'cutover': cutover, 'sources': sources})
        return thread

    def _build_turn_input(self, *, msg, participants_msg, contacts_msg, room_id):
        if self._cutover_history is not None:
            body, ref = self._cutover_history
            msg = replace(msg, content=encode({'current_message': msg.content, 'owned_prior_context': body, 'history_ref': ref}))
        # SDK raw room history is neither owned nor authenticated. Never inject it.
        self._needs_history_injection.discard(room_id)
        self._raw_history_by_room.pop(room_id, None)
        return super()._build_turn_input(msg=msg, participants_msg=participants_msg, contacts_msg=contacts_msg, room_id=room_id)

    async def _start_turn(self, params):
        if not self.router.active():
            raise IntegrationError("GRANT_INACTIVE")
        result = await super()._start_turn(params)
        turn = result.get("turn", {})
        if not turn.get("id"):
            raise IntegrationError("TURN_RECEIPT_MISSING")
        self._turn_identity = (params["threadId"], turn["id"])
        operation, attempt = self.current.get()
        self.mailbox.update(operation, attempt, delivery="STARTED", thread=params["threadId"])
        if any(item.get('text') == '[System Instructions]\n' + self.config.system_prompt for item in params.get('input', [])):
            self.thread_ownership.prompted(params['threadId'], digest(self.config.system_prompt.encode()))
            self._record('THREAD_MANDATE_INJECTED', {'thread': params['threadId'], 'prompt_hash': digest(self.config.system_prompt.encode())})
        self._record("TURN_ACCEPTED", {"session": params["threadId"], "turn": turn["id"], "cwd": self.workspace})
        return result

    async def _emit_turn_outcome(self, **kwargs):
        self._sdk_outcome = kwargs.get("turn_status")
        self._record("TURN_OUTCOME", {k: v for k, v in kwargs.items() if k not in {"tools", "msg"}})
        return await super()._emit_turn_outcome(**kwargs)

    async def _handle_approval_request(self, *, tools, msg, room_id, event, params):
        if event.id is None:
            return
        callback = (self._turn_identity, str(event.id))
        if callback in self._answered_callbacks:
            self._record("DUPLICATE_CALLBACK_FENCED", {"id": str(event.id)})
            return
        self._answered_callbacks.add(callback)
        operation, attempt = self.current.get()
        action, argv, paths = "unknown", (), ()
        if event.method == "item/commandExecution/requestApproval":
            action = "command"
            command = params.get("command")
            if isinstance(command, str):
                try:
                    # Shell operators/expansions are not structured argv authority.
                    if not re.search(r"[;&|`$<>\n]", command):
                        argv = tuple(shlex.split(command))
                except ValueError:
                    pass
        elif event.method == "item/fileChange/requestApproval":
            action = "file_write"
            paths = tuple(self._extract_file_change_paths(params))
        request = PermissionRequest(operation, attempt, str(event.id), action,
                    params.get("cwd", ""), argv, paths,
                    bool(params.get("additionalPermissions") or params.get("grantRoot") or params.get("network")))
        accepted = False if room_id in self._closing_rooms else await self.router.decide(request)
        await self._client.respond(event.id, {"decision": "accept" if accepted else "decline"})
        self._record("APPROVAL_REPLY", {"request_id": str(event.id), "accepted": accepted, "action": action})

    def _build_dynamic_tools(self, tools):
        result = super()._build_dynamic_tools(tools)
        result.append({"name": "dh_peer_question", "description": "Ask the coordinator with full context, yield this turn, resume from a later answer. Never wait synchronously.",
                       "inputSchema": {"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"], "additionalProperties": False}})
        return result

    async def _handle_server_request(self, *, tools, msg, room_id, event):
        params = event.params if isinstance(event.params, dict) else {}
        if event.id is not None and event.method in {"item/tool/call", "item/tool/requestUserInput"}:
            operation, attempt = self.current.get()
            if not self.mailbox.callback(operation, attempt, str(event.id), {"method": event.method, "params": params}):
                self._record("DUPLICATE_CALLBACK_FENCED", {"id": str(event.id)})
                return False
        if event.method == "item/tool/requestUserInput" or (event.method == "item/tool/call" and params.get("tool") == "dh_peer_question"):
            if event.id is None:
                raise IntegrationError("QUESTION_CALLBACK_ID_MISSING")
            operation, attempt = self.current.get()
            qid = digest(encode([operation, attempt, str(event.id)]).encode())
            context = {"task": msg.content, "questions": params, "session": self._turn_identity}
            self.guard.require_clean(context)
            self.mailbox.question(qid, operation, self.coordinator_id, context)
            if event.method == "item/tool/requestUserInput":
                # Callback is resolved exactly once with a non-answer. The actual
                # peer answer is NOT invented; it arrives as a new continuation.
                result = {"answers": {str(q.get("id")): {"answers": ["Deferred to coordinator; current turn is yielding."]} for q in params.get("questions", []) if isinstance(q, dict)}}
            else:
                result = {"contentItems": [{"type": "inputText", "text": "Question routed; current turn ends. Reply will be a continuation."}], "success": True}
            await self._client.respond(event.id, result)
            if self._turn_identity:
                await self._client.request("turn/interrupt", {"threadId": self._turn_identity[0], "turnId": self._turn_identity[1]}, retry_on_overload=False)
            # Observe termination before releasing the execution lane. Preserve
            # thread mapping for thread/resume on continuation; no effect replay.
            await self._client.close()
            self._client = None
            self._initialized = False
            self._room_threads.pop(room_id, None)
            self.mailbox.update(operation, attempt, state="PAUSED", delivery="YIELDED")
            self._yielded = True
            await tools.send_message(f"@[[{self.coordinator_id}]] PeerQuestion {qid}\n{encode(context)}\nReply to this seat with /dh-answer {qid} followed by a newline and the complete answer.", mentions=[self.coordinator_id])
            raise PeerYield()
        if isinstance(tools, GuardedTools):
            tools.call_id = str(params.get("callId") or event.id)
        self._verification_effect = None
        settled = await super()._handle_server_request(tools=tools, msg=msg, room_id=room_id, event=event)
        if self._verification_effect is not None:
            # SDK has authenticated/responded to the native tool callback once.
            # Retire the model turn without awaiting checker runtime in SDK180s.
            effect = self._verification_effect
            if self._turn_identity:
                await self._client.request('turn/interrupt', {'threadId': self._turn_identity[0], 'turnId': self._turn_identity[1]}, retry_on_overload=False)
            self._record('VERIFICATION_YIELD', {'effect': effect})
            raise VerificationYield(effect)
        return settled

    async def cancel_owned(self, operation, attempt):
        work = self.mailbox.read_work(operation)
        if work["attempt"] != attempt or work["seat"] != self.alias:
            raise IntegrationError("OWNER_ATTEMPT_FENCE")
        if work["state"] in {"SUCCEEDED", "FAILED", "CANCELLED", "CLOSED_UNRESOLVED"}:
            return {"state": work["state"], "termination": "NOT_REQUESTED_TERMINAL"}
        self._active_room.set(self.allowed_room)
        if self.worker and not self.worker.done():
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        state = self._active_client_state()
        if state and state.client:
            await state.client.close()
            state.client = None
            state.initialized = False
        # Native group stop is not proof of external Docker cleanup. The
        # trusted checker owns cleanup; its final evidence remains separately stored.
        self.mailbox.update(operation, attempt, state="CANCELLED", delivery="RETURNED", result={"termination": "OWNED_GROUP_STOPPED", "checker_external_cleanup": "NOT_PROVEN"})
        self.mailbox.drain_controls(operation, attempt)
        return {"termination": "OWNED_GROUP_STOPPED", "checker_external_cleanup": "NOT_PROVEN"}

    async def on_cleanup(self, room_id):
        self.stopping = True
        if self.worker and not self.worker.done():
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        await asyncio.to_thread(self.verification.broker.close)
        await super().on_cleanup(room_id)


from .runtime import DurableRuntime


class CodexRuntime(DurableRuntime):
    """Provider translation facade implementing the runtime-independent contract."""
    def __init__(self, adapter):
        self.adapter = adapter

    async def readiness(self):
        a = self.adapter
        a._active_room.set(a.allowed_room)
        a._room_client(a.allowed_room)
        await a._ensure_client_ready()
        # account/read must never log token fields; return only authentication kind.
        account = await a._client.request("account/read", {}, retry_on_overload=False)
        auth = account.get("account") or {}
        models = await a._client.request("model/list", {}, retry_on_overload=False)
        visible = a._visible_model_ids(models)
        if auth.get("type") != "chatgpt" or a.config.model not in visible:
            raise IntegrationError("RUNTIME_NOT_READY")
        settings = getattr(a, 'effective_settings', None)
        return {"ready": True, "authentication": "chatgpt", "model": a.config.model,
                "effort": a.config.reasoning_effort, "inference": "NOT_PROBED",
                "effective_settings": settings.evidence() if settings else None,
                "effective_timeout_s": a.config.turn_timeout_s,
                "timeout_source": getattr(a, "turn_timeout_source", "adapter_config")}

    async def start(self, binding):
        settings = getattr(self.adapter, 'effective_settings', None)
        expected = getattr(self.adapter, 'binding', None)
        if expected is not None and binding != expected:
            raise IntegrationError('RUNTIME_BINDING_MISMATCH')
        if binding.runtime != 'codex' or (settings and binding.settings_sha256 != settings.fingerprint):
            raise IntegrationError("RUNTIME_BINDING_MISMATCH")
        if binding.workspace != self.adapter.workspace or binding.prompt_sha256 != digest(self.adapter.config.system_prompt.encode()):
            raise IntegrationError("RUNTIME_BINDING_MISMATCH")
        await self.adapter.on_started(self.adapter.display_name, "Scoped factory seat")

    async def attach(self, operation, attempt, session):
        row = self.adapter.mailbox.read_work(operation)
        if row["attempt"] != attempt or row["thread"] != session:
            raise IntegrationError("OWNER_ATTEMPT_FENCE")
        if row["delivery"] == "DELIVERY_UNKNOWN":
            raise IntegrationError("UNKNOWN_EFFECT_RECONCILIATION_REQUIRED")
        if not self.adapter.thread_ownership.owned(session):
            raise IntegrationError("CROSS_BINDING_THREAD_DENIED")
        self.adapter.history = CodexSessionState(thread_id=session, room_id=row["room"])
