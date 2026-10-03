"""Controller Grant consumer. A command matcher is NOT sandbox enforcement."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .contract import PermissionRequest
from .mailbox import IntegrationError


class ApprovalRouter:
    """Reads only Core controls produced by authenticated controller grant.record.

    Seat content never supplies Grant objects. Same-UID tampering is not isolated.
    Arbitrary shell/interpreter scripts and unknown privilege requests are denied.
    Controller scope schema: workspace, seats, rooms, approved_commands (exact argv),
    file_write (bool), expires_at (UTC ISO timestamp), run_id. No wildcard grants.
    """
    def __init__(self, owner, grant_id, seat, room, run_id, workspace, *, result_repo=None):
        from .git_broker import result_repo_boundary
        self.owner, self.grant_id = owner, grant_id
        self.seat, self.room, self.run_id = seat, room, run_id
        self.workspace = str(Path(workspace).resolve())
        self.configured_result_repo = result_repo
        self.result_repo = str(result_repo_boundary(self.workspace, result_repo))

    def _scope(self, db):
        row = db.execute("SELECT body FROM controls WHERE kind='grant' AND id=?", (self.grant_id,)).fetchone()
        if not row:
            return None
        grant = json.loads(row[0])
        if grant.get("revoked") or not grant.get("source") or not grant.get("end_condition"):
            return None
        scope = grant.get("scope", {})
        # Run completion/revoke is an explicit end condition, not an invented
        # numeric time or retry budget. Validate an expiry only when supplied.
        if "expires_at" in scope:
            try:
                expiry = datetime.fromisoformat(scope["expires_at"].replace("Z", "+00:00"))
                if expiry <= datetime.now(timezone.utc):
                    return None
            except (TypeError, ValueError):
                return None
        run = db.execute("SELECT body FROM controls WHERE kind='run' AND id=?", (self.run_id,)).fetchone()
        if run and json.loads(run[0]).get("state") in {"STOPPED", "COMPLETED", "REVOKED"}:
            return None
        if scope.get("run_id") != self.run_id or self.seat not in scope.get("seats", []) or self.room not in scope.get("rooms", []) or str(Path(scope.get("workspace", "")).resolve()) != self.workspace:
            return None
        # Repository narrowing is authenticated alongside the workspace ceiling.
        # A runtime config cannot choose a different repo using the same Grant.
        if scope.get('result_repo') != self.configured_result_repo:
            return None
        from .git_broker import result_repo_boundary
        try:
            if str(result_repo_boundary(self.workspace, self.configured_result_repo)) != self.result_repo:
                return None
        except (IntegrationError, OSError, ValueError):
            return None
        return scope

    def active(self):
        with self.owner.transaction(self.owner.epoch) as db:
            return self._scope(db) is not None

    async def decide(self, request: PermissionRequest):
        accepted = False
        with self.owner.transaction(self.owner.epoch) as db:
            scope = self._scope(db)
            if scope and not request.privilege and str(Path(request.workspace).resolve()) == self.workspace:
                if request.action == "file_read" and request.paths:
                    accepted = all(Path(p).is_absolute() and Path(p).resolve().is_relative_to(Path(self.workspace)) for p in request.paths)
                elif request.action == "file_write" and request.paths and scope.get("file_write") is True:
                    accepted = all(Path(p).is_absolute() and Path(p).resolve().is_relative_to(Path(self.workspace)) for p in request.paths)
                elif request.action == "command" and request.argv:
                    # Exact controller-reviewed command, not a regex or binary allowlist.
                    # Even exact matching is not a guarantee about subprocess effects.
                    binary = Path(request.argv[0]).name
                    forbidden = {"sh", "bash", "zsh", "fish", "sudo", "su", "doas", "python", "python3", "node", "perl", "ruby", "env", "eval"}
                    accepted = binary not in forbidden and list(request.argv) in scope.get("approved_commands", [])
            db.execute("INSERT INTO c_event(operation,kind,body,at) VALUES(?,?,?,?)", (request.operation, "PERMISSION_DECISION", json.dumps({"attempt": request.attempt, "request_id": request.request_id, "action": request.action, "accepted": accepted, "authority": "controller_grant", "protection": "native-controlled"}), datetime.now(timezone.utc).isoformat()))
        return accepted
