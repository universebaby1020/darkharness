"""Shared protected platform/typed Git/verification routing for native backends."""
from __future__ import annotations
import asyncio
import json
from band.core.protocols import to_failure_event
from band.runtime.tools.schema import ToolCallOutcome, serialize_tool_result
from .mailbox import IntegrationError, Mailbox, digest, encode
from .verification import VerificationBroker

class LocalSendRejected(IntegrationError):
    """Pure preflight rejection; no external boundary has been crossed."""


# Band SDK 4.0.0 ChatEventRequest documents this platform serialized-byte cap.
# It belongs to the SDK send boundary, not the run/Grant or Core scheduler.
SDK4_EVENT_METADATA_MAX_BYTES = 65536


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
            request = ChatEventRequest(**body)
            metadata = request.model_dump(mode='json', exclude_unset=True).get('metadata')
            if metadata is not None:
                # JSON escaping and UTF-8 expansion count too. The SDK diff
                # value cap alone does not bound the containing metadata object.
                serialized = json.dumps(metadata, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
                if len(serialized) > SDK4_EVENT_METADATA_MAX_BYTES:
                    raise LocalSendRejected('LOCAL_EVENT_METADATA_TOO_LARGE')
        if not has_visible_content(body['content']):
            raise LocalSendRejected('LOCAL_SEND_VALIDATION_REJECTED')
    except (ValueError, TypeError, AttributeError, BandToolError, ValidationError):
        raise LocalSendRejected('LOCAL_SEND_VALIDATION_REJECTED') from None


READ_TOOLS = {"band_get_participants", "band_lookup_peers", "band_fetch_room_context", "band_list_room_files", "band_read_room_file"}
SEND_TOOLS = {"band_send_message", "band_send_event", "band_no_reply"}
GIT_TOOLS = {
    'dh_local_git_commit': {'description': 'Commit existing seat-authored scoped regular files locally using controller Grant. No shell, push, amend or source generation; expected_head is exact 40-hex current commit.',
        'inputSchema': {'type': 'object', 'properties': {'cwd': {'type': 'string'}, 'paths': {'type': 'array', 'items': {'type': 'string'}}, 'message': {'type': 'string'}, 'expected_head': {'type': 'string'}}, 'required': ['cwd', 'paths', 'message', 'expected_head'], 'additionalProperties': False}},
    'dh_review_snapshot': {'description': 'Create an independent exact-revision shallow review checkout in controller-assigned scratch, without overwriting. revision must be exact 40-hex commit, name a fresh directory name.',
        'inputSchema': {'type': 'object', 'properties': {'cwd': {'type': 'string'}, 'revision': {'type': 'string'}, 'name': {'type': 'string'}}, 'required': ['cwd', 'revision', 'name'], 'additionalProperties': False}}
}


class _SingleSendEndpoint:
    def __init__(self, endpoint):
        self.endpoint = endpoint

    async def create_agent_chat_message(self, **kwargs):
        kwargs['request_options'] = {**(kwargs.get('request_options') or {}), 'max_retries': 0}
        return await self.endpoint.create_agent_chat_message(**kwargs)

    async def create_agent_chat_event(self, **kwargs):
        kwargs['request_options'] = {**(kwargs.get('request_options') or {}), 'max_retries': 0}
        return await self.endpoint.create_agent_chat_event(**kwargs)


class _SingleSendRest:
    def __init__(self, rest):
        self.rest = rest

    @property
    def agent_api_messages(self):
        return _SingleSendEndpoint(self.rest.agent_api_messages)

    @property
    def agent_api_events(self):
        return _SingleSendEndpoint(self.rest.agent_api_events)


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
        return [s for s in schemas if (s.get("function", {}).get("name") or s.get("name")) in READ_TOOLS | SEND_TOOLS] + [{'name': name, **schema} for name, schema in GIT_TOOLS.items()] + self.adapter.verification.schemas()

    async def _send(self, method, body):
        self.adapter.guard.require_clean(body)
        if not self.adapter.router.active():
            raise IntegrationError("GRANT_INACTIVE")
        # A recovery child consumes historical ACKs, never posts the same settled
        # payload again. Match only authenticated canonical predecessor proofs.
        from .codex_timeout import CodexTimeoutRecovery
        recovery = CodexTimeoutRecovery(self.adapter.mailbox, self.adapter.router, self.adapter.git_broker)
        with self.adapter.mailbox.owner.transaction(self.adapter.mailbox.owner.epoch) as db:
            prior, _ = recovery._predecessors(db, self.operation)
            if prior:
                raw = db.execute('SELECT body FROM c_artifact WHERE hash=?', (prior['id'],)).fetchone()
                if not raw or digest(raw[0]) != prior['id'] or raw[0].decode() != prior['proof']:
                    raise IntegrationError('CONTINUATION_LINEAGE_CONFLICT')
                h = digest(encode(body).encode())
                for old in json.loads(prior['proof']).get('settled_outbox', []):
                    if old['hash'] != h or old['state'] != 'ACKED':
                        continue
                    actual = db.execute('SELECT * FROM c_outbox WHERE id=?', (old['id'],)).fetchone()
                    if not actual or dict(actual) != old:
                        raise IntegrationError('CONTINUATION_OUTBOX_CHANGED')
                    Mailbox.event(db, self.operation, 'RECOVERED_SEND_READBACK', {'attempt': self.attempt, 'outbox_id': old['id'], 'evidence_id': prior['id'], 'effect': 'ALREADY_ACKED_NOT_REPLAYED'})
                    return json.loads(old['receipt'])
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
        phase = 'RAW_SEND'
        from band.runtime.tools.agent import AgentTools
        from copy import copy
        # SDK4 normally hides HTTP retries inside a single durable SEND_INTENT.
        # Use a call-local facade, never mutate installed SDK or shared raw tools.
        single_attempt = getattr(type(self.raw), method, None) is getattr(AgentTools, method)
        sender = self.raw
        if single_attempt:
            sender = copy(self.raw)
            sender.rest = _SingleSendRest(self.raw.rest)
        try:
            result = await getattr(sender, method)(**body)
            phase = 'RECEIPT_SERIALIZE'
            receipt = serialize_tool_result(result)
            phase = 'RECEIPT_VALIDATE'
            if receipt is None:
                raise IntegrationError("SEND_ACK_MISSING")
            phase = 'RECEIPT_GUARD'
            self.adapter.guard.require_clean(receipt)
            phase = 'MAILBOX_SENT'
            self.adapter.mailbox.sent(identifier, receipt)
            return result
        except BaseException as exc:
            # Keep the fence for every crossed-boundary uncertainty, including
            # cancellation. Never log exception text, request, headers or body.
            status = getattr(exc, 'status_code', None)
            if status is None:
                status = getattr(getattr(exc, 'response', None), 'status_code', None)
            status = status if type(status) is int and 100 <= status <= 599 else None
            import httpx
            from band_rest.core.api_error import ApiError
            definitive = single_attempt and phase == 'RAW_SEND' and (
                isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)) or
                isinstance(exc, (ApiError, httpx.HTTPStatusError)) and status in {400, 403, 404, 413, 422})
            if definitive:
                self.adapter.mailbox.send_not_sent(identifier, self.attempt, 'CONNECT_NOT_SENT' if status is None else 'HTTP_REJECTED_' + str(status))
                raise LocalSendRejected('DEFINITIVE_SEND_NOT_SENT') from None
            diagnostic = self.adapter.guard.sanitize({'outbox_id': identifier,
                'method': method, 'phase': phase,
                'exception_class': type(exc).__name__, 'http_status': status})
            self.adapter.mailbox.observe(self.operation, self.attempt, "DELIVERY_UNKNOWN", diagnostic)
            raise

    async def send_message(self, content, mentions=None):
        # SDK completed-turn proxy reply and internal question export, not the
        # model's explicit business tool. Redact before crossing the boundary.
        body = self.adapter.guard.sanitize({'content': content, 'mentions': mentions})
        try:
            return await self._send('send_message', body)
        except IntegrationError as exc:
            if not isinstance(exc, LocalSendRejected) and str(exc) not in {'GRANT_INACTIVE', 'OUTBOX_SECRET_BLOCKED'}:
                raise
            self.adapter._reply_not_sent = {'effect': 'NOT_SENT', 'code': str(exc)}
            self._local_telemetry('SDK_REPLY_NOT_SENT', self.adapter._reply_not_sent)
            return {'ok': False, **self.adapter._reply_not_sent}

    def _local_telemetry(self, kind, body):
        # SDK reporting is not business delivery. Even local recording failure
        # must not replace the original SDK exception or create an outbox fence.
        try:
            self.adapter.mailbox.observe(self.operation, self.attempt, kind,
                                         self.adapter.guard.sanitize(body))
        except Exception:
            pass
        return {'ok': False}

    async def send_event(self, content, message_type, metadata=None):
        return self._local_telemetry('SDK_TELEMETRY_SUPPRESSED',
                                     {'content': content, 'message_type': message_type, 'metadata': metadata})

    async def send_failure(self, failure):
        try:
            content, metadata = to_failure_event(failure)
            return self._local_telemetry('SDK_FAILURE_LOCAL', {'content': content, 'metadata': metadata})
        except Exception:
            return {'ok': False}

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
                with self.adapter.mailbox.owner.transaction(self.adapter.mailbox.owner.epoch) as db:
                    old_ack = db.execute("SELECT 1 FROM c_git_effect WHERE id=? AND state='ACKED'", (identifier,)).fetchone()
                try:
                    result = method(self.operation, self.attempt, identifier, **arguments)
                except (OSError, ValueError, TypeError):
                    raise IntegrationError('LOCAL_GIT_DIAGNOSTIC_REDACTED') from None
                # Stamp the actual creating seat; canonical receipt/hash is unchanged.
                if not old_ack:
                    VerificationBroker.record_git_origin(self.adapter.mailbox, self.adapter.router, identifier)
                # An ACK replay is not a creating call. Never backfill a legacy
                # run from today's Grant; legacy provenance requires Main ledger.
                self.adapter.guard.require_clean(result)
                self.adapter.mailbox.observe(self.operation, self.attempt, 'LOCAL_GIT_TOOL_RESULT', {'name': name, 'call_id': self.call_id, 'receipt': result})
            elif name == 'dh_verify':
                try:
                    effect, result = await self.adapter.verification.start(self.operation, self.attempt, self.call_id, arguments)
                    self.adapter._verification_effect = effect
                except BaseException:
                    effect = digest(encode([self.operation, self.attempt, self.call_id, 'dh_verify']).encode())
                    with self.adapter.mailbox.owner.transaction(self.adapter.mailbox.owner.epoch) as db:
                        if db.execute('SELECT 1 FROM c_verification_effect WHERE id=? AND operation=? AND attempt=?', (effect, self.operation, self.attempt)).fetchone():
                            self.adapter._verification_effect = effect
                    raise
            elif name == 'dh_verification_read':
                self.adapter.guard.require_clean(arguments)
                if not isinstance(arguments, dict) or set(arguments) != {'effect_id', 'artifact', 'offset', 'limit'}:
                    raise IntegrationError('TYPED_VERIFICATION_ARGUMENTS_REQUIRED')
                result = await asyncio.to_thread(self.adapter.verification.read_page, self.operation, self.attempt, **arguments)
            elif name == "band_send_message":
                result = await self._send('send_message', {'mentions': None, **arguments})
            elif name == "band_send_event":
                result = await self._send('send_event', {'metadata': None, **arguments})
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
            failure = {"error": str(exc)}
            if name == 'dh_verify':
                effect = digest(encode([self.operation, self.attempt, self.call_id, 'dh_verify']).encode())
                failure.update(self.adapter.verification.broker.failure_contract(self.operation, self.attempt, effect))
            return ToolCallOutcome(value=failure, ok=False, error_message=str(exc))

    async def execute_tool_call(self, name, arguments):
        return (await self.execute_tool_call_structured(name, arguments)).value


