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
from band.core.protocols import to_failure_event
from band.core.types import PlatformMessage
from band.integrations.codex.stdio_client import CodexStdioClient
from band.integrations.codex.types import CodexSessionState
from band.runtime.tools.schema import ToolCallOutcome, serialize_tool_result

from .artifacts import git_evidence, slug
from .git_broker import LocalGitBroker
from .contract import PermissionRequest, RuntimeBinding, RuntimeEvent
from .mailbox import HandoffPart, IntegrationError, digest, encode


class LocalSendRejected(IntegrationError):
    """Pure preflight rejection; no external boundary has been crossed."""


def validate_local_send(raw, method, body):
    # Pinned SDK4 resolver and Fern models, not string-based exception inference.
    from band.runtime.tools.agent import AgentTools
    from band.client.rest import ChatMessageRequest, ChatMessageRequestMentionsItem, ChatEventRequest
    from band.core.content import has_visible_content
    from band.core.exceptions import BandToolError
    from pydantic import ValidationError
    try:
        if method == 'send_message':
            mentions = body['mentions']
            if mentions is not None and (not isinstance(mentions, list) or any(not isinstance(m, (str, dict)) for m in mentions)):
                raise LocalSendRejected('LOCAL_SEND_VALIDATION_REJECTED')
            # This private seam is pinned to SDK4. It is synchronous and pure.
            resolved = AgentTools._resolve_required_mentions(raw, mentions)
            ChatMessageRequest(content=body['content'], mentions=[ChatMessageRequestMentionsItem(**m) for m in resolved])
        elif method == 'send_event':
            ChatEventRequest(**body)
        if not has_visible_content(body['content']):
            raise LocalSendRejected('LOCAL_SEND_VALIDATION_REJECTED')
    except (ValueError, TypeError, AttributeError, BandToolError, ValidationError):
        raise LocalSendRejected('LOCAL_SEND_VALIDATION_REJECTED') from None


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


READ_TOOLS = {"band_get_participants", "band_lookup_peers", "band_fetch_room_context", "band_list_room_files", "band_read_room_file"}
SEND_TOOLS = {"band_send_message", "band_send_event", "band_no_reply"}
GIT_TOOLS = {
    'dh_local_git_commit': {'description': 'Commit existing seat-authored scoped regular files locally using controller Grant. No shell, push, amend or source generation; expected_head is exact 40-hex current commit.',
        'inputSchema': {'type': 'object', 'properties': {'cwd': {'type': 'string'}, 'paths': {'type': 'array', 'items': {'type': 'string'}}, 'message': {'type': 'string'}, 'expected_head': {'type': 'string'}}, 'required': ['cwd', 'paths', 'message', 'expected_head'], 'additionalProperties': False}},
    'dh_review_snapshot': {'description': 'Create an independent exact-revision shallow review checkout in controller-assigned scratch, without overwriting. revision must be exact 40-hex commit, name a fresh directory name.',
        'inputSchema': {'type': 'object', 'properties': {'cwd': {'type': 'string'}, 'revision': {'type': 'string'}, 'name': {'type': 'string'}}, 'required': ['cwd', 'revision', 'name'], 'additionalProperties': False}}
}


class GuardedTools:
    """All SDK send paths and dynamic platform tools pass through this facade.

    Unknown platform effects (room creation, memory/contacts, uploads) fail closed.
    Native CLI/MCP effects outside this facade remain native-controlled/observed.
    """
    def __init__(self, raw, adapter, operation, attempt):
        self.raw, self.adapter = raw, adapter
        self.operation, self.attempt = operation, attempt
        self.counter = 0
        self.call_id = None

    def __getattr__(self, name):
        # Read-only adapter metadata and schema methods; effect methods must not
        # silently escape through attribute forwarding.
        if name in {"participants", "is_hub_room", "get_participants", "lookup_peers", "fetch_room_context", "list_room_files", "read_room_file"}:
            return getattr(self.raw, name)
        raise AttributeError(name)

    def get_openai_tool_schemas(self, **kwargs):
        schemas = self.raw.get_openai_tool_schemas(**kwargs)
        return [s for s in schemas if (s.get("function", {}).get("name") or s.get("name")) in READ_TOOLS | SEND_TOOLS] + [{'name': name, **schema} for name, schema in GIT_TOOLS.items()]

    async def _send(self, method, body):
        self.adapter.guard.require_clean(body)
        if not self.adapter.router.active():
            raise IntegrationError("GRANT_INACTIVE")
        self.counter += 1
        identifier = digest(encode([self.operation, self.attempt, self.call_id or "adapter", method, self.counter]).encode())
        old = self.adapter.mailbox.send_readback(identifier, self.operation, body)
        if old is not None:
            return old
        try:
            validate_local_send(self.raw, method, body)
        except LocalSendRejected as exc:
            self.adapter.mailbox.reject_send(identifier, self.operation, self.attempt, body, str(exc))
            raise
        old = self.adapter.mailbox.prepare_send(identifier, self.operation, body)
        if old is not None:
            return old
        try:
            result = await getattr(self.raw, method)(**body)
            receipt = serialize_tool_result(result)
            if receipt is None:
                raise IntegrationError("SEND_ACK_MISSING")
            self.adapter.guard.require_clean(receipt)
            self.adapter.mailbox.sent(identifier, receipt)
            return result
        except BaseException:
            self.adapter.mailbox.observe(self.operation, self.attempt, "DELIVERY_UNKNOWN", {"outbox_id": identifier})
            raise

    async def send_message(self, content, mentions=None):
        return await self._send("send_message", {"content": content, "mentions": mentions})

    async def send_event(self, content, message_type, metadata=None):
        return await self._send("send_event", {"content": content, "message_type": message_type, "metadata": metadata})

    async def send_failure(self, failure):
        content, metadata = to_failure_event(failure)
        return await self.send_event(content, "error", metadata)

    async def no_reply(self, reason=None):
        return {"status": "no_reply"}

    async def execute_tool_call_structured(self, name, arguments):
        try:
            if name in {'band_send_message', 'band_send_event'}:
                allowed = {'content', 'mentions'} if name == 'band_send_message' else {'content', 'message_type', 'metadata'}
                required = {'content'} if name == 'band_send_message' else {'content', 'message_type'}
                if not isinstance(arguments, dict) or not required <= arguments.keys() or not arguments.keys() <= allowed:
                    raise LocalSendRejected('LOCAL_SEND_VALIDATION_REJECTED')
            if name in GIT_TOOLS:
                self.adapter.guard.require_clean(arguments)
                required = GIT_TOOLS[name]['inputSchema']['required']
                if not isinstance(arguments, dict) or set(arguments) != set(required) or not self.call_id:
                    raise IntegrationError('TYPED_GIT_ARGUMENTS_REQUIRED')
                identifier = digest(encode([self.operation, self.attempt, self.call_id, name]).encode())
                method = self.adapter.git_broker.commit if name == 'dh_local_git_commit' else self.adapter.git_broker.snapshot
                try:
                    result = method(self.operation, self.attempt, identifier, **arguments)
                except (OSError, ValueError, TypeError):
                    raise IntegrationError('LOCAL_GIT_DIAGNOSTIC_REDACTED') from None
                self.adapter.guard.require_clean(result)
                self.adapter.mailbox.observe(self.operation, self.attempt, 'LOCAL_GIT_TOOL_RESULT', {'name': name, 'call_id': self.call_id, 'receipt': result})
            elif name == "band_send_message":
                result = await self.send_message(**arguments)
            elif name == "band_send_event":
                result = await self.send_event(**arguments)
            elif name == "band_no_reply":
                result = await self.no_reply(**arguments)
            elif name in READ_TOOLS:
                outcome = await self.raw.execute_tool_call_structured(name, arguments)
                self.adapter.guard.require_clean(outcome.value)
                return outcome
            else:
                raise IntegrationError("PLATFORM_EFFECT_NOT_GRANTED")
            return ToolCallOutcome(value=serialize_tool_result(result), ok=True)
        except IntegrationError as exc:
            self.adapter.mailbox.observe(self.operation, self.attempt, "TOOL_BLOCKED", {"code": str(exc), "name": name})
            return ToolCallOutcome(value={"error": str(exc)}, ok=False, error_message=str(exc))

    async def execute_tool_call(self, name, arguments):
        return (await self.execute_tool_call_structured(name, arguments)).value


class DurableCodexAdapter(CodexAdapter):
    def __init__(self, *, mailbox, router, guard, alias, display_name, room_id, workspace,
                 coordinator_id, config, event_sink=None):
        if version("band-sdk") != "4.0.0":
            raise IntegrationError("SDK_VERSION_UNSUPPORTED")
        if config.cwd is not None or config.approval_policy != "on-request" or config.sandbox != "workspace-write" or config.sandbox_policy is not None or config.enable_self_config_tools:
            raise IntegrationError("UNSAFE_RUNTIME_CONFIGURATION")
        if not config.system_prompt or not config.model or not config.reasoning_effort or not config.workspace_for_room:
            raise IntegrationError("EXPLICIT_RUNTIME_CONFIG_REQUIRED")
        super().__init__(config=config)
        self.mailbox, self.router, self.guard = mailbox, router, guard
        self.git_broker = LocalGitBroker(mailbox, router, display_name, slug(display_name) + '@actors.invalid', workspace)
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

    def _record(self, kind, data):
        self._record_for(self.current.get(), kind, data)

    def _record_for(self, context, kind, data):
        data = self.guard.sanitize(data)
        operation, attempt = context if context else (None, None)
        self.mailbox.observe(operation, attempt, kind, data)
        event = RuntimeEvent(operation, attempt, kind, data)
        self._events.put_nowait(event)
        if self.event_sink:
            self.event_sink(event)

    def _build_client(self, config):
        state = self._require_active_client_state()
        if str(Path(state.workspace).resolve()) != self.workspace:
            raise IntegrationError("WORKSPACE_BINDING_MISMATCH")
        evidence = ClientEvidence(self, self.current.get())
        client = OwnedStdioClient(command=config.codex_command, cwd=state.workspace,
                                 env=config.codex_env, record=evidence.record, guard=self.guard)
        client.evidence = evidence
        return client

    async def on_message(self, msg, tools, history, participants_msg, contacts_msg, *, is_session_bootstrap, room_id):
        if room_id != self.allowed_room or self.stopping:
            raise IntegrationError("ROOM_BINDING_DENIED")
        self.raw_tools = tools
        self.history = history
        envelope = asdict(msg)
        envelope["created_at"] = msg.created_at.isoformat()
        envelope["participants_msg"] = participants_msg
        envelope["contacts_msg"] = contacts_msg
        envelope["session_thread"] = history.thread_id
        # Control lane is consumed before normal work, including while busy.
        text = strip_leading_mentions(msg.content).strip()
        # Also accept the native platform typed form before SDK normalization.
        text = re.sub(r"^(?:\s*@\[\[[^\]]+\]\])+\s*", "", text).strip()
        if text.startswith("/dh-answer "):
            self.mailbox.receive_control(self.alias, room_id, msg.sender_id, msg.id, msg.content)
            first, sep, answer = text.partition("\n")
            qid = first.split(maxsplit=1)[1]
            if not sep:
                raise IntegrationError("PEER_ANSWER_INCOMPLETE")
            self.mailbox.answer(qid, msg.sender_id, answer)
            await self._hydrate_startup_tools(tools)
            self._wake()
            return
        part = None
        content = msg.content
        try:
            payload = json.loads(content)
        except ValueError:
            payload = None
        if isinstance(payload, dict) and "dh_handoff" in payload:
            try:
                part = HandoffPart(**payload["dh_handoff"])
                content = payload["content"]
            except (KeyError, TypeError):
                raise IntegrationError("HANDOFF_SHAPE_INVALID") from None
        self.mailbox.receive(self.alias, room_id, msg.sender_id, msg.id, content, envelope=envelope, part=part)
        await self._hydrate_startup_tools(tools)
        self._wake()
        # No await of the model turn or peer reply on the Band room dispatch lane.

    async def _hydrate_startup_tools(self, tools):
        # A message may race maintenance startup's explicit room bind. Its
        # receipt is already durable; never start queued work on an empty cache.
        if self.startup_binding_pending:
            from .launch import hydrate_room_tools
            await hydrate_room_tools(tools, getattr(tools, '_ctx', None))
            self.raw_tools = tools
            self.startup_binding_pending = False

    def _wake(self):
        if not self.stopping and not self.startup_binding_pending and self.raw_tools is not None and (self.worker is None or self.worker.done()):
            self.worker = asyncio.create_task(self._drain())

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
            # Thread choice belongs to this durable WorkItem, never a cached
            # thread left by a different queued operation on the same seat.
            self._room_threads.pop(self.allowed_room, None)
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
            thread = work["thread"] or envelope.get("session_thread")
            history = CodexSessionState(thread_id=thread, room_id=self.allowed_room) if thread else self.history
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
            except PeerYield:
                self._record("TURN_YIELDED", {"peer": self.coordinator_id})
            except asyncio.CancelledError:
                self.mailbox.update(work["id"], attempt, state="PAUSED", delivery="DELIVERY_UNKNOWN")
                raise
            except BaseException as exc:
                # Failure return may leave native effects: fence, don't auto replay.
                self.mailbox.update(work["id"], attempt, state="PAUSED", delivery="DELIVERY_UNKNOWN", result={"code": type(exc).__name__})
                self._record("RUNTIME_ERROR", {"code": type(exc).__name__, "diagnostic": self.guard.redact(str(exc)), "replay": "FENCED"})
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
        # SDK4 cannot refresh dynamicTools on thread/resume. Reuse a thread only
        # when its durable source/mandate/tool fingerprint matches; otherwise
        # start a native thread with explicit, evidenced old-thread lineage.
        sources = {name: digest((Path(__file__).parent / name).read_bytes()) for name in ('codex.py', 'git_broker.py', 'recovery.py')}
        fingerprint = digest(encode({'tools': self._build_dynamic_tools(tools), 'mandate': digest(self.config.system_prompt.encode()), 'sources': sources}).encode())
        old_thread = self._room_threads.get(room_id) or history.thread_id
        cutover = False
        if old_thread:
            with self.mailbox.owner.transaction(self.mailbox.owner.epoch) as db:
                registered = db.execute('SELECT fingerprint FROM c_thread_tools WHERE thread=?', (old_thread,)).fetchone()
            cutover = registered is None or registered[0] != fingerprint
        if cutover:
            self._room_threads.pop(room_id, None)
            self._record('THREAD_TOOLSET_CUTOVER_INTENT', {'old_thread': old_thread, 'fingerprint': fingerprint, 'reason': 'SDK4_resume_cannot_refresh_dynamicTools'})
            history = CodexSessionState(room_id=room_id)
        thread = await super()._ensure_thread(room_id=room_id, history=history, tools=tools, is_session_bootstrap=is_session_bootstrap)
        with self.mailbox.owner.transaction(self.mailbox.owner.epoch) as db:
            db.execute('INSERT OR REPLACE INTO c_thread_tools VALUES(?,?)', (thread, fingerprint))
        self._record('THREAD_TOOLSET_BOUND', {'thread': thread, 'old_thread': old_thread if cutover else None, 'fingerprint': fingerprint, 'cutover': cutover})
        return thread

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
        return await super()._handle_server_request(tools=tools, msg=msg, room_id=room_id, event=event)

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
        self.mailbox.update(operation, attempt, state="CANCELLED", delivery="RETURNED", result={"termination": "OWNED_GROUP_STOPPED"})
        self.mailbox.drain_controls(operation, attempt)
        return {"termination": "OWNED_GROUP_STOPPED"}

    async def on_interrupt(self, room_id, mode):
        # Real SDK control hook is required because on_message returned early.
        # STOP/interrupt never authorize replay of already-started effects.
        if room_id != self.allowed_room:
            raise IntegrationError("ROOM_BINDING_DENIED")
        self.stopping = True
        with self.mailbox.owner.transaction(self.mailbox.owner.epoch) as db:
            rows = db.execute("SELECT id,attempt FROM c_work WHERE seat=? AND delivery IN ('DISPATCHING','STARTED','DELIVERY_UNKNOWN')", (self.alias,)).fetchall()
        for row in rows:
            self.mailbox.control(digest(encode([row[0], row[1], str(mode)]).encode()), row[0], row[1], "cancel", {"source": "SDK_control", "mode": str(mode)})
            await self.cancel_owned(row[0], row[1])

    async def on_cleanup(self, room_id):
        self.stopping = True
        if self.worker and not self.worker.done():
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        await super().on_cleanup(room_id)


class CodexRuntime:
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
        return {"ready": True, "authentication": "chatgpt", "model": a.config.model,
                "effort": a.config.reasoning_effort, "inference": "NOT_PROBED"}

    async def start(self, binding):
        if binding.workspace != self.adapter.workspace or binding.prompt_sha256 != digest(self.adapter.config.system_prompt.encode()):
            raise IntegrationError("RUNTIME_BINDING_MISMATCH")
        await self.adapter.on_started(self.adapter.display_name, "Scoped factory seat")

    async def attach(self, operation, attempt, session):
        row = self.adapter.mailbox.read_work(operation)
        if row["attempt"] != attempt or row["thread"] != session:
            raise IntegrationError("OWNER_ATTEMPT_FENCE")
        if row["delivery"] == "DELIVERY_UNKNOWN":
            raise IntegrationError("UNKNOWN_EFFECT_RECONCILIATION_REQUIRED")
        self.adapter.history = CodexSessionState(thread_id=session, room_id=row["room"])

    async def dispatch(self, turn):
        if turn.workspace != self.adapter.workspace or turn.room != self.adapter.allowed_room:
            raise IntegrationError("WORKSPACE_BINDING_MISMATCH")
        self.adapter.mailbox.receive(self.adapter.alias, turn.room, turn.sender, turn.operation,
              turn.full_input, envelope={"id": turn.operation, "sender_id": turn.sender, "parent_operation": turn.parent_operation})
        self.adapter._wake()

    async def events(self):
        while True:
            yield await self.adapter._events.get()

    async def permission_reply(self, request):
        return await self.adapter.router.decide(request)

    async def peer_answer(self, identifier, sender, answer):
        result = self.adapter.mailbox.answer(identifier, sender, answer)
        self.adapter._wake()
        return result

    async def cancel(self, operation, attempt):
        return await self.adapter.cancel_owned(operation, attempt)

    async def resume(self, operation, attempt):
        row = self.adapter.mailbox.read_work(operation)
        if row["attempt"] != attempt or row["delivery"] != "YIELDED":
            raise IntegrationError("UNKNOWN_EFFECT_RECONCILIATION_REQUIRED")
        self.adapter._wake()  # Only verified continuations, never original replay.

    async def status(self, operation):
        return self.adapter.mailbox.read_work(operation)

    async def collect(self, operation):
        return {"work": self.adapter.mailbox.read_work(operation), "git": git_evidence(self.adapter.workspace)}
