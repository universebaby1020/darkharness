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
                effect, result = await self.adapter.verification.start(self.operation, self.attempt, self.call_id, arguments)
                self.adapter._verification_effect = effect
            elif name == 'dh_verification_read':
                self.adapter.guard.require_clean(arguments)
                if not isinstance(arguments, dict) or set(arguments) != {'effect_id', 'artifact', 'offset', 'limit'}:
                    raise IntegrationError('TYPED_VERIFICATION_ARGUMENTS_REQUIRED')
                result = await asyncio.to_thread(self.adapter.verification.read_page, self.operation, self.attempt, **arguments)
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


