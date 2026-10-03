"""Optional protected Band SDK4 Claude Code backend (not Anthropic API).

Uses the real Band config/history/prompt contract and Claude Agent SDK transport.
Upstream room approval, unguarded MCP, fallback and session-manager paths are not
used. Native protection is controller-scoped/native-controlled, not OS isolation.
Private transport seams are pinned and fail closed on dependency version drift.
"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import asdict
from importlib.metadata import version
import json
import os
from pathlib import Path
import re
import shlex
import signal
import uuid

import anyio
from anyio.streams.text import TextReceiveStream, TextSendStream
from claude_agent_sdk import (ClaudeAgentOptions, ClaudeSDKClient, HookMatcher,
    PermissionResultAllow, PermissionResultDeny, ResultMessage, SystemMessage,
    SdkMcpTool, create_sdk_mcp_server)
from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport
from band.adapters.claude_sdk import ClaudeSDKAdapter, ClaudePermissionMode, SimpleAdapter

from .artifacts import git_evidence, slug
from .contract import PermissionRequest
from .durable import DurableSeatMixin
from .git_broker import LocalGitBroker
from .mailbox import IntegrationError, Mailbox, digest, encode
from .protected_tools import GuardedTools
from .runtime import DurableRuntime
from .thread_ownership import ThreadOwnership
from .verification_bridge import VerificationBridge


# Same native tools on each seat; tool choice confers no authority. New native
# tools require reviewed translation, not a profile/plugin supplied by a model.
NATIVE_TOOLS = ("Read", "Glob", "Grep", "Write", "Edit", "Bash")


def dependency_check():
    if version('band-sdk') != '4.0.0' or version('claude-agent-sdk') != '0.2.163' or version('mcp') != '1.30.0':
        raise IntegrationError('CLAUDE_DEPENDENCY_VERSION_UNSUPPORTED:band-sdk==4.0.0/claude-agent-sdk==0.2.163/mcp==1.30.0')
    if os.name != "posix" or not Path('/proc/self/stat').is_file():
        raise IntegrationError("CLAUDE_PROTECTED_TRANSPORT_REQUIRES_LINUX")


class OwnedClaudeTransport(SubprocessCLITransport):
    """SDK command/framing, fresh owned process group, no ambient API billing.

    Only native login homes and minimal process environment references pass in;
    credentials are never fetched/copied. No global subprocess monkey patch.
    """
    def __init__(self, options, record, guard):
        super().__init__(prompt=None, options=options)
        self.record, self.guard = record, guard
        self.identity = self.group = None
        self._stderr_reader = None

    async def connect(self):
        if self._process is not None:
            return
        from .codex import process_identity
        dependency_check()
        if not self._cli_path:
            raise IntegrationError('CLAUDE_EXPLICIT_CLI_REQUIRED')
        env = {k: os.environ[k] for k in ('HOME', 'PATH', 'LANG', 'TMPDIR') if k in os.environ}
        env.update(self._options.env)
        env.update(CLAUDE_CODE_ENTRYPOINT='sdk-py', CLAUDE_AGENT_SDK_VERSION=version('claude-agent-sdk'),
                   CLAUDE_CODE_SDK_READS_SESSION_STATE='1')
        # No ANTHROPIC_API_KEY/AUTH_TOKEN/BASE_URL, AWS/Vertex/Bedrock selection,
        # inherited CLAUDECODE or ambient MCP/plugin flags enter this process.
        self._process = await anyio.open_process(self._build_command(), cwd=self._cwd,
            env=env, stdin=-1, stdout=-1, stderr=-1, start_new_session=True)
        self.identity = process_identity(self._process.pid)
        self.group = os.getpgid(self._process.pid)
        if self.group != self._process.pid:
            raise IntegrationError('CLAUDE_PROCESS_GROUP_OWNERSHIP_FAILED')
        self._stdout_stream = TextReceiveStream(self._process.stdout)
        self._stdin_stream = TextSendStream(self._process.stdin)
        self._stderr_stream = TextReceiveStream(self._process.stderr)
        self._ready = True
        self.record('CLAUDE_PROCESS_STARTED', {'identity': self.identity, 'group': self.group})
        self._stderr_reader = asyncio.create_task(self._capture_stderr())

    async def _capture_stderr(self):
        pending = ''
        discarding = False
        try:
            async for text in self._stderr_stream:
                if discarding:
                    if '\n' not in text:
                        continue
                    text = text.split('\n', 1)[1]
                    discarding = False
                pending += text
                while '\n' in pending:
                    line, pending = pending.split('\n', 1)
                    self.record('CLAUDE_STDERR', {'text': self.guard.redact(line)})
                if len(pending) > 16 * 1024 * 1024:
                    # Never log truncated secret fragments. This is an evidence
                    # frame bound, not a turn timeout or execution permission.
                    self.record('CLAUDE_STDERR_OVERSIZE', {'sha256': digest(pending.encode())})
                    pending = ''
                    discarding = True
        except asyncio.CancelledError:
            pending = ''  # Never emit a cancelled/incomplete credential fragment.
            raise
        except (anyio.EndOfStream, anyio.ClosedResourceError):
            pass
        finally:
            if pending:
                self.record('CLAUDE_STDERR', {'text': self.guard.redact(pending)})

    async def close(self):
        from .codex import process_identity, group_members
        if self.group is not None and group_members(self.group):
            try:
                same = process_identity(self.identity[0]) == self.identity
            except OSError:
                same = False
            if not same:
                self.record('CLAUDE_PROCESS_STOP_UNKNOWN', {'reason': 'leader_identity_unprovable'})
                raise IntegrationError('CLAUDE_PROCESS_OWNERSHIP_UNKNOWN')
            os.killpg(self.group, signal.SIGTERM)
            await asyncio.sleep(0)
            if group_members(self.group):
                os.killpg(self.group, signal.SIGKILL)
        if self._stderr_reader is not None:
            self._stderr_reader.cancel()
            await asyncio.gather(self._stderr_reader, return_exceptions=True)
            self._stderr_reader = None
        await super().close()
        members = group_members(self.group) if self.group is not None else []
        self.record('CLAUDE_PROCESS_STOPPED' if not members else 'CLAUDE_PROCESS_STOP_UNKNOWN', {'members': members})
        if members:
            raise IntegrationError('CLAUDE_PROCESS_TERMINATION_UNKNOWN')


class DurableClaudeAdapter(DurableSeatMixin, ClaudeSDKAdapter):
    """Durable intake + protected SDK MCP + exact native Grant translation."""
    def __init__(self, *, settings, binding, config, mailbox, router, guard, alias,
                 display_name, room_id, coordinator_id, receipt_run_resolver=None,
                 report_parsers=None):
        dependency_check()
        if (config.permission_mode != ClaudePermissionMode.DEFAULT or config.fallback_model is not None
                or config.setting_sources or config.cli.plugin_dirs or config.cli.add_dirs or config.cli.extra_args
                or config.approvals is not None or config.model != settings.model
                or config.effort != settings.effort or str(config.cwd) != settings.workspace):
            raise IntegrationError('UNSAFE_CLAUDE_RUNTIME_CONFIGURATION')
        super().__init__(config=config)
        self.effective_settings, self.binding = settings, binding
        self.mailbox, self.router, self.guard = mailbox, router, guard
        self.alias, self.display_name = alias, display_name
        self.allowed_room, self.workspace = room_id, settings.workspace
        self.coordinator_id = coordinator_id
        self.git_broker = LocalGitBroker(mailbox, router, display_name, slug(display_name)+'@actors.invalid', self.workspace)
        self.verification = VerificationBridge(mailbox, router, guard, report_parsers=report_parsers,
                                               receipt_run_resolver=receipt_run_resolver)
        self.thread_ownership = ThreadOwnership(mailbox, router, settings.fingerprint)
        self.current = ContextVar('dh_claude_current', default=None)
        self._events = asyncio.Queue()
        self.event_sink = None
        self.worker = self.raw_tools = self.history = None
        self.stopping = self.startup_binding_pending = False
        self.auto_recover_settled_timeouts = False  # Codex terminal proofs cannot qualify Claude.
        self._context = self._client_owned = self._transport_owned = None
        self._verification_effect = self._peer_question = None
        self._session = self._compatibility = None
        self._tool_lock = asyncio.Lock()
        self._terminal_tool = False
        self._termination_unknown = False

    async def on_started(self, agent_name, agent_description):
        # Keep actual Band SimpleAdapter metadata, but do not construct the
        # upstream unguarded MCP/backend or room-driven approval manager.
        await SimpleAdapter.on_started(self, agent_name, agent_description)
        self._record('CLAUDE_BINDING_PINNED', self.effective_settings.evidence())

    def _owned_record(self, kind, data):
        self._record_for(self._context, kind, data)

    def _assert_attempt(self, context):
        if context is None or self._context != context or self.stopping or not self.router.active():
            raise IntegrationError('CLAUDE_CALLBACK_ATTEMPT_FENCED')
        row = self.mailbox.read_work(context[0])
        if row['attempt'] != context[1] or row['state'] != 'RUNNING':
            raise IntegrationError('OWNER_ATTEMPT_FENCE')

    async def native_permission(self, name, arguments, identifier, context):
        self._assert_attempt(context)
        action, argv, paths = 'unknown', (), ()
        if name == 'Bash':
            command = arguments.get('command')
            if isinstance(command, str) and not re.search(r'[;&|`$<>\n(){}*?~\\]', command):
                try:
                    argv = tuple(shlex.split(command))
                    action = 'command'
                except ValueError:
                    pass
        elif name in {'Write', 'Edit'}:
            path = arguments.get('file_path')
            if isinstance(path, str) and Path(path).is_absolute():
                paths, action = (path,), 'file_write'
        elif name in {'Read', 'Glob', 'Grep'}:
            path = arguments.get('file_path', arguments.get('path', self.workspace))
            if isinstance(path, str):
                path = str((Path(self.workspace) / path).resolve()) if not Path(path).is_absolute() else path
                paths, action = (path,), 'file_read'
        request = PermissionRequest(context[0], context[1], str(identifier), action, self.workspace, argv, paths,
                                    bool(arguments.get('dangerouslyDisableSandbox')))
        accepted = await self.router.decide(request)
        self._owned_record('CLAUDE_PERMISSION_REPLY', {'tool': name, 'request_id': str(identifier), 'accepted': accepted})
        return accepted

    def _hooks(self, context):
        async def pre_tool(data, tool_use_id, hook_context):
            try:
                name = data.get('tool_name', '')
                if str(Path(data.get('cwd', '')).resolve()) != self.workspace:
                    raise IntegrationError('WORKSPACE_BINDING_MISMATCH')
                # The only MCP server is ours, and every handler fences its
                # captured attempt/Grant separately. Other MCP tools are denied.
                if name.startswith('mcp__dh__'):
                    self._assert_attempt(context)
                    allowed = name[9:] in self._registered_tools
                else:
                    allowed = await self.native_permission(name, data.get('tool_input', {}), tool_use_id, context)
                return {'hookSpecificOutput': {'hookEventName': 'PreToolUse',
                        'permissionDecision': 'allow' if allowed else 'deny',
                        'permissionDecisionReason': 'DarkHarness controller Grant'}}
            except IntegrationError:
                return {'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'deny',
                        'permissionDecisionReason': 'DarkHarness attempt fenced'}}
        return {'PreToolUse': [HookMatcher(matcher=None, hooks=[pre_tool])]}

    def _mcp_tools(self, tools, context):
        result = []
        schemas = tools.get_openai_tool_schemas()
        schemas.append({'name': 'dh_peer_question', 'description': 'Ask the coordinator with full task context; yield and continue only after an authenticated answer.',
                        'inputSchema': {'type': 'object', 'properties': {'question': {'type': 'string'}}, 'required': ['question'], 'additionalProperties': False}})
        self._registered_tools = set()
        for schema in schemas:
            spec = schema.get('function', schema)
            name = spec['name']
            self._registered_tools.add(name)
            async def handler(arguments, name=name):
                async with self._tool_lock:
                    try:
                        self._assert_attempt(context)
                        self.guard.require_clean(arguments)
                        tools.call_id = str(uuid.uuid4())
                        # Room authority is captured, never model-selected.
                        arguments = dict(arguments)
                        if 'chat_id' in arguments:
                            if arguments.pop('chat_id') != self.allowed_room:
                                raise IntegrationError('ROOM_BINDING_DENIED')
                        if self._peer_question is not None or self._verification_effect is not None:
                            raise IntegrationError('CLAUDE_TURN_YIELD_FENCE')
                        if name == 'dh_peer_question':
                            if set(arguments) != {'question'} or not isinstance(arguments['question'], str) or not arguments['question'].strip():
                                raise IntegrationError('QUESTION_SHAPE_INVALID')
                            qid = digest(encode([*context, tools.call_id]).encode())
                            row = self.mailbox.read_work(context[0])
                            body = {'task': json.loads(row['input'])['content'], 'question': arguments['question'], 'session': self._session}
                            self.mailbox.question(qid, context[0], self.coordinator_id, body)
                            self._peer_question = (qid, body)
                            value, ok = {'question_id': qid, 'state': 'YIELDING'}, True
                        else:
                            outcome = await tools.execute_tool_call_structured(name, arguments)
                            value, ok = outcome.value, outcome.ok
                            if ok and name in {'band_send_message', 'band_send_event', 'band_no_reply'}:
                                self._terminal_tool = True
                        self.guard.require_clean(value)
                        return {'content': [{'type': 'text', 'text': encode(value)}], 'is_error': not ok}
                    except IntegrationError as exc:
                        return {'content': [{'type': 'text', 'text': str(exc)}], 'is_error': True}
                    except Exception as exc:
                        self._owned_record('CLAUDE_TOOL_ERROR', {'code': type(exc).__name__})
                        raise IntegrationError('CLAUDE_TOOL_EXECUTION_UNKNOWN') from None
            input_schema = dict(spec.get('parameters', spec.get('inputSchema', {})))
            # SDK distinguishes a JSON schema from its {name: PythonType}
            # shorthand by BOTH type and properties, even for empty objects.
            input_schema.setdefault('type', 'object')
            input_schema.setdefault('properties', {})
            result.append(SdkMcpTool(name, spec.get('description', ''), input_schema, handler))
        return result, schemas

    def _options(self, tools, context, resume):
        registered, schemas = self._mcp_tools(tools, context)
        server = create_sdk_mcp_server(name='dh', version='1.0.0', tools=registered)
        async def can_use_tool(name, arguments, permission_context):
            try:
                if name.startswith('mcp__dh__'):
                    self._assert_attempt(context)
                    allowed = name[9:] in self._registered_tools
                else:
                    allowed = await self.native_permission(name, arguments, permission_context.tool_use_id, context)
            except IntegrationError:
                allowed = False
            return PermissionResultAllow(updated_input=arguments) if allowed else PermissionResultDeny(message='DarkHarness controller Grant denied')
        options = ClaudeAgentOptions(model=self.effective_settings.model, effort=self.effective_settings.effort,
            fallback_model=None, system_prompt=self.config.custom_section, cwd=self.workspace,
            cli_path=self.config.cli.cli_path, env=dict(self.config.cli.env), permission_mode='default',
            tools=list(NATIVE_TOOLS), allowed_tools=[],
            mcp_servers={'dh': server}, strict_mcp_config=True, setting_sources=[], plugins=[],
            hooks=self._hooks(context), can_use_tool=can_use_tool, resume=resume,
            stderr=lambda text: self._owned_record('CLAUDE_STDERR', {'text': self.guard.redact(text)}))
        return options, schemas

    def _build_native_client(self, options, context):
        transport = OwnedClaudeTransport(options, lambda kind, data: self._record_for(context, kind, data), self.guard)
        self._transport_owned = transport
        return ClaudeSDKClient(options=options, transport=transport)

    async def _retire_native(self):
        try:
            if self._client_owned is not None:
                await self._client_owned.disconnect()
        finally:
            if self._transport_owned is not None:
                await self._transport_owned.close()
        self._client_owned = self._transport_owned = None

    def _bind_session(self, session):
        if not isinstance(session, str) or not session:
            raise IntegrationError('CLAUDE_SESSION_RECEIPT_MISSING')
        self.thread_ownership.bind(session, self._compatibility)
        self.thread_ownership.prompted(session, self.binding.prompt_sha256)
        self._session = session
        self.mailbox.update(*self._context, delivery='STARTED', thread=session)
        self._owned_record('CLAUDE_SESSION_BOUND', {'session': session, 'settings_sha256': self.effective_settings.fingerprint,
                                                 'compatibility': self._compatibility})

    async def _query(self, options, content, context):
        client = self._build_native_client(options, context)
        self._client_owned = client
        await client.connect()
        # Connect/dispatch failures are UNKNOWN, never implicit resume fallback.
        await client.query(content, session_id=self.allowed_room)
        self.mailbox.update(*context, delivery='STARTED')
        transcript = []
        async for message in client.receive_response():
            self._assert_attempt(context)
            if isinstance(message, SystemMessage) and message.subtype == 'init':
                selected = message.data.get('model')
                if selected != self.effective_settings.model:
                    raise IntegrationError('CLAUDE_NATIVE_MODEL_MISMATCH')
                self._bind_session(message.data.get('session_id'))
            # Store actual Claude message types, never invented Codex RPC events.
            data = self.guard.sanitize(asdict(message))
            transcript.append({'type': type(message).__name__, 'data': data})
            self._owned_record('CLAUDE_NATIVE_MESSAGE', transcript[-1])
            if self._peer_question is not None or self._verification_effect is not None:
                return {'yield': True, 'transcript': transcript}
            if isinstance(message, ResultMessage):
                self._bind_session(message.session_id)
                return {'yield': False, 'transcript': transcript, 'error': message.is_error,
                        'terminal_tool': self._terminal_tool, 'subtype': message.subtype}
        raise IntegrationError('CLAUDE_NATIVE_TERMINAL_MISSING')

    async def _drain(self):
        while not self.stopping:
            work = self.mailbox.next_ready(self.alias)
            if work is None:
                return
            context = (work['id'], str(uuid.uuid4()))
            self.mailbox.claim(*context)
            self._context = context
            token = self.current.set(context)
            self._peer_question = self._verification_effect = None
            self._terminal_tool = False
            self._session = None
            tools = GuardedTools(self.raw_tools, self, *context)
            try:
                options, schemas = self._options(tools, context, None)
                self._compatibility = digest(encode({'tools': schemas, 'mandate': self.binding.prompt_sha256,
                    'binding': self.effective_settings.fingerprint}).encode())
                sources = {name: digest((Path(__file__).parent/name).read_bytes()) for name in
                           ('claude.py', 'durable.py', 'protected_tools.py', 'git_broker.py', 'verification_bridge.py', 'thread_ownership.py')}
                owned = self.thread_ownership.latest(work['thread'])
                options.resume = owned['thread'] if owned and owned['compatibility'] == self._compatibility else None
                envelope = json.loads(work['input'])
                prior, ref = self.thread_ownership.history(work['id'])
                content = encode({'current_task': envelope, 'owned_prior_context': prior, 'history_ref': ref,
                                  'room': self.allowed_room, 'settings_sha256': self.effective_settings.fingerprint})
                self.guard.require_clean(content)
                self._owned_record('CLAUDE_TURN_DISPATCH', {'resume': options.resume, 'sources': sources,
                                   'settings': self.effective_settings.evidence()})
                async with asyncio.timeout(self.effective_settings.turn_timeout_s):
                    result = await self._query(options, content, context)
                await self._retire_native()
                if self._peer_question is not None:
                    qid, body = self._peer_question
                    self.mailbox.update(*context, state='PAUSED', delivery='YIELDED')
                    await tools.send_message(f'@[[{self.coordinator_id}]] PeerQuestion {qid}\n{encode(body)}\nReply to this seat with /dh-answer {qid} followed by a newline and the complete answer.', mentions=[self.coordinator_id])
                    self._owned_record('CLAUDE_TURN_YIELDED', {'question_id': qid})
                elif self._verification_effect is not None:
                    effect = self._verification_effect
                    self._owned_record('VERIFICATION_WAIT', {'effect': effect, 'sdk_turn_timeout_seconds': self.effective_settings.turn_timeout_s,
                                                            'checker_wait': 'OWNED_ASYNC_NO_DEFAULT_BUDGET'})
                    await self.verification.broker.wait(*context, effect)
                    if self.verification.complete(*context, effect) is None:
                        self.mailbox.update(*context, state='PAUSED', delivery='DELIVERY_UNKNOWN')
                else:
                    state = 'FAILED' if result['error'] or not result['terminal_tool'] else 'SUCCEEDED'
                    self.mailbox.update(*context, state=state, delivery='RETURNED', result={**result,
                        'git': self.guard.sanitize(git_evidence(self.git_broker.root)), 'acceptance': 'NOT_EVALUATED'})
            except BaseException as exc:
                self.mailbox.update(*context, state='PAUSED', delivery='DELIVERY_UNKNOWN', result={'code': type(exc).__name__, 'replay': 'FENCED'})
                self._owned_record('CLAUDE_RUNTIME_ERROR', {'code': type(exc).__name__, 'replay': 'FENCED'})
                if isinstance(exc, asyncio.CancelledError):
                    raise
                return
            finally:
                try:
                    await self._retire_native()
                except BaseException as exc:
                    self.mailbox.update(*context, state='PAUSED', delivery='DELIVERY_UNKNOWN')
                    self._owned_record('CLAUDE_PROCESS_STOP_UNKNOWN', {'code': type(exc).__name__})
                    self._termination_unknown = True
                    self.stopping = True
                self._context = None
                self.current.reset(token)

    async def cancel_owned(self, operation, attempt):
        row = self.mailbox.read_work(operation)
        if row['attempt'] != attempt or row['seat'] != self.alias or row['room'] != self.allowed_room:
            raise IntegrationError('OWNER_ATTEMPT_FENCE')
        if row['state'] in {'SUCCEEDED', 'FAILED', 'CANCELLED', 'CLOSED_UNRESOLVED'}:
            return {'state': row['state'], 'termination': 'NOT_REQUESTED_TERMINAL'}
        if self.worker is not None and not self.worker.done():
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        await self._retire_native()  # Raises on unknown ownership, no false cancellation receipt.
        if self._termination_unknown:
            raise IntegrationError('CLAUDE_PROCESS_TERMINATION_UNKNOWN')
        self.mailbox.update(operation, attempt, state='CANCELLED', delivery='RETURNED',
                            result={'termination': 'OWNED_GROUP_STOPPED', 'checker_external_cleanup': 'NOT_PROVEN'})
        self.mailbox.drain_controls(operation, attempt)
        return {'termination': 'OWNED_GROUP_STOPPED', 'checker_external_cleanup': 'NOT_PROVEN'}

    async def on_cleanup(self, room_id):
        if room_id != self.allowed_room:
            raise IntegrationError('ROOM_BINDING_DENIED')
        self.stopping = True
        if self.worker is not None and not self.worker.done():
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        await self._retire_native()
        await asyncio.to_thread(self.verification.broker.close)
        await SimpleAdapter.on_cleanup(self, room_id)


class ClaudeRuntime(DurableRuntime):
    async def readiness(self):
        settings = self.adapter.effective_settings
        if (settings.result_repo != getattr(self.adapter.router, 'configured_result_repo', None) or
                str(self.adapter.git_broker.root) != self.adapter.router.result_repo):
            raise IntegrationError('RESULT_REPO_BINDING_MISMATCH')
        dependency_check()
        # Local-only readiness does not claim auth, provider/model availability,
        # native CLI qualification or inference. No login/version/inference probe.
        return {'ready': True, 'level': 'LOCAL_COMPONENT', 'authentication': 'NOT_PROBED',
                'inference': 'NOT_RUN', 'execution_qualification': 'NOT_RUN',
                'prompt_sha256': digest(self.adapter.config.custom_section.encode()),
                'effective_settings': self.adapter.effective_settings.evidence()}

    async def start(self, binding):
        if binding != self.adapter.binding:
            raise IntegrationError('RUNTIME_BINDING_MISMATCH')
        await self.adapter.on_started(self.adapter.display_name, 'Scoped factory seat')

    async def attach(self, operation, attempt, session):
        row = self.adapter.mailbox.read_work(operation)
        if row['attempt'] != attempt or row['thread'] != session or row['seat'] != self.adapter.alias:
            raise IntegrationError('OWNER_ATTEMPT_FENCE')
        if row['delivery'] == 'DELIVERY_UNKNOWN':
            raise IntegrationError('UNKNOWN_EFFECT_RECONCILIATION_REQUIRED')
        if not self.adapter.thread_ownership.owned(session):
            raise IntegrationError('CROSS_BINDING_THREAD_DENIED')
        # Selection occurs exclusively through verified ownership in _drain.
        return {'session': session, 'binding': self.adapter.effective_settings.fingerprint}
