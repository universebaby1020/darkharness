"""Durable room intake and Core controls, independent of native runtime."""
from __future__ import annotations
import asyncio
from dataclasses import asdict
import json
import re
from band.adapters.codex import strip_leading_mentions
from .contract import RuntimeEvent
from .mailbox import HandoffPart, IntegrationError, digest, encode

class DurableSeatMixin:
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

    async def on_message(self, msg, tools, history, participants_msg, contacts_msg, *, is_session_bootstrap, room_id):
        if room_id != self.allowed_room or self.stopping:
            raise IntegrationError("ROOM_BINDING_DENIED")
        self.raw_tools = tools
        self.history = history
        envelope = asdict(msg)
        envelope["created_at"] = msg.created_at.isoformat()
        envelope["participants_msg"] = participants_msg
        envelope["contacts_msg"] = contacts_msg
        envelope["session_thread"] = getattr(history, 'thread_id', getattr(history, 'session_id', None))
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

