"""Runtime-independent execution contract. No provider names or SDK types."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, Protocol


@dataclass(frozen=True)
class RuntimeBinding:
    runtime: str
    version: str
    workspace: str
    sandbox_policy: str
    approval_policy: str
    protection: str  # core-owned / native-controlled / observed-only
    limitations: tuple[str, ...]
    effective_timeout_s: float
    prompt_sha256: str
    connection: str = "legacy"
    model: str = ""
    effort: str = ""
    settings_sha256: str = ""
    settings_sources: tuple[tuple[str, str], ...] = field(default=(), compare=False)
    result_repo: str | None = None  # explicit repository narrowing, not native cwd


@dataclass(frozen=True)
class SeatSettings:
    """Pinned execution values; origins are evidence, never execution identity.

    Commands and external auth-home references must not be emitted in status.
    No credential contents belong in this object.
    """
    connection: str
    runtime: str
    workspace: str
    model: str
    effort: str
    turn_timeout_s: float
    command: tuple[str, ...] = field(repr=False)
    runtime_env: tuple[tuple[str, str], ...] = field(repr=False)
    sources: tuple[tuple[str, str], ...] = field(compare=False)
    result_repo: str | None = None

    @property
    def fingerprint(self) -> str:
        import hashlib
        import json
        body = {name: getattr(self, name) for name in (
            "connection", "runtime", "workspace", "model", "effort",
            "turn_timeout_s", "command", "runtime_env")}
        if self.result_repo is not None:
            body['result_repo'] = self.result_repo
        return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"),
                                         allow_nan=False).encode()).hexdigest()

    def evidence(self) -> dict[str, Any]:
        return {**({'result_repo': self.result_repo} if self.result_repo is not None else {}),
                "connection": self.connection, "runtime": self.runtime,
                "model": self.model, "effort": self.effort,
                "effective_timeout_s": self.turn_timeout_s,
                "sources": dict(self.sources), "settings_sha256": self.fingerprint}


@dataclass(frozen=True)
class BackendRegistration:
    """Controller-owned code registration, never an import path from JSON.

    A factory must return a protected adapter, Runtime facade and RuntimeBinding.
    preflight is local-only and must reject capability/dependency failures before
    credential loading, Agent creation, native processes or room effects.
    """
    harness: str
    version: str
    environment_names: frozenset[str]
    preflight: Callable[[SeatSettings], None]
    factory: Callable[..., tuple[Any, Any, RuntimeBinding]]


@dataclass(frozen=True)
class TurnInput:
    operation: str
    attempt: str
    workspace: str
    full_input: str
    room: str
    sender: str
    parent_operation: str | None = None


@dataclass(frozen=True)
class RuntimeEvent:
    operation: str
    attempt: str
    kind: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PermissionRequest:
    operation: str
    attempt: str
    request_id: str
    action: str
    workspace: str
    argv: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()
    privilege: bool = False


@dataclass(frozen=True)
class PeerQuestion:
    identifier: str
    operation: str
    attempt: str
    peer: str
    full_context: dict[str, Any]


ApprovalCallback = Callable[[PermissionRequest], Awaitable[bool]]
QuestionCallback = Callable[[PeerQuestion], Awaitable[None]]


class Runtime(Protocol):
    """Dispatch returns on acceptance; events, questions and results arrive later.

    resume must NOT replay unknown external effects; a fresh attempt needs explicit
    lineage and reconciliation. cancel may operate only on this binding's processes.
    """
    async def readiness(self) -> dict[str, Any]: ...
    async def start(self, binding: RuntimeBinding) -> None: ...
    async def attach(self, operation: str, attempt: str, session: str) -> None: ...
    async def dispatch(self, turn: TurnInput) -> None: ...
    def events(self) -> AsyncIterator[RuntimeEvent]: ...
    async def permission_reply(self, request: PermissionRequest) -> bool: ...
    async def peer_answer(self, identifier: str, sender: str, answer: str) -> str: ...
    async def cancel(self, operation: str, attempt: str) -> dict[str, Any]: ...
    async def resume(self, operation: str, attempt: str) -> None: ...
    async def status(self, operation: str) -> dict[str, Any]: ...
    async def collect(self, operation: str) -> dict[str, Any]: ...
