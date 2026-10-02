"""Backend-independent durable Runtime contract facade."""
from .artifacts import git_evidence
from .mailbox import IntegrationError

class DurableRuntime:
    def __init__(self, adapter):
        self.adapter = adapter

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
        row = self.adapter.mailbox.read_work(operation)
        settings = getattr(self.adapter, 'effective_settings', None)
        return {**row, "effective_settings": settings.evidence() if settings else None}

    async def collect(self, operation):
        return {"work": await self.status(operation), "git": git_evidence(self.adapter.workspace)}
