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
        self._repo_pin = self._identity_pin()

    def _identity_pin(self):
        import stat
        pins = []
        for path in (Path(self.result_repo), Path(self.result_repo) / '.git'):
            try:
                st = path.lstat()
            except FileNotFoundError:
                # Legacy non-Git workspace routing remains supported. Pin absence
                # too: it cannot silently become a different repository authority.
                pins.append(None)
                continue
            if not stat.S_ISDIR(st.st_mode):
                raise IntegrationError('DIRECT_REPO_DIRECTORY_REQUIRED')
            pins.append((st.st_dev, st.st_ino))
        return tuple(pins)

    def _reason(self, db, reason):
        from .mailbox import Mailbox, encode
        key = encode([self.grant_id, self.run_id, self.seat, self.room, self.workspace, self.result_repo])
        # Scope checks also run in controller preparation before Mailbox exists.
        # Use the same owner event schema; do not lose or suppress transitions.
        db.execute('CREATE TABLE IF NOT EXISTS c_event(seq INTEGER PRIMARY KEY AUTOINCREMENT, operation TEXT, kind TEXT, body TEXT, at TEXT)')
        db.execute('CREATE TABLE IF NOT EXISTS c_grant_observation(binding TEXT PRIMARY KEY, reason TEXT)')
        old = db.execute('SELECT reason FROM c_grant_observation WHERE binding=?', (key,)).fetchone()
        if not old or old[0] != reason:
            Mailbox.event(db, None, 'GRANT_SCOPE_CHANGED', {'grant_id': self.grant_id, 'run_id': self.run_id, 'seat': self.seat, 'reason': reason})
            db.execute('INSERT OR REPLACE INTO c_grant_observation VALUES(?,?)', (key, reason))
        return None

    def _scope(self, db):
        row = db.execute("SELECT body FROM controls WHERE kind='grant' AND id=?", (self.grant_id,)).fetchone()
        if not row:
            return self._reason(db, 'GRANT_MISSING')
        grant = json.loads(row[0])
        if grant.get("revoked") or not grant.get("source") or not grant.get("end_condition"):
            return self._reason(db, 'GRANT_REVOKED_OR_INVALID')
        scope = grant.get("scope", {})
        # Run completion/revoke is an explicit end condition, not an invented
        # numeric time or retry budget. Validate an expiry only when supplied.
        if "expires_at" in scope:
            try:
                expiry = datetime.fromisoformat(scope["expires_at"].replace("Z", "+00:00"))
                if expiry <= datetime.now(timezone.utc):
                    return self._reason(db, 'GRANT_EXPIRED')
            except (TypeError, ValueError):
                return self._reason(db, 'GRANT_EXPIRY_INVALID')
        run = db.execute("SELECT body FROM controls WHERE kind='run' AND id=?", (self.run_id,)).fetchone()
        if run and json.loads(run[0]).get("state") in {"STOPPED", "COMPLETED", "REVOKED"}:
            return self._reason(db, 'RUN_ENDED')
        if scope.get("run_id") != self.run_id or self.seat not in scope.get("seats", []) or self.room not in scope.get("rooms", []) or str(Path(scope.get("workspace", "")).resolve()) != self.workspace:
            return self._reason(db, 'SCOPE_BINDING_MISMATCH')
        # Repository narrowing is authenticated alongside the workspace ceiling.
        # A runtime config cannot choose a different repo using the same Grant.
        if scope.get('result_repo') != self.configured_result_repo:
            return self._reason(db, 'REPO_BINDING_MISMATCH')
        try:
            if self._identity_pin() != self._repo_pin:
                return self._reason(db, 'REPO_IDENTITY_CHANGED')
        except (IntegrationError, OSError, ValueError):
            return self._reason(db, 'REPO_IDENTITY_CHANGED')
        self._reason(db, 'ACTIVE')
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
