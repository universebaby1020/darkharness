"""Pre-registered provider policy + authenticated dispatch receipt, no issuer.

Consumes the existing Grant/owner transaction. No Grant updates after dispatch.
Unknown items, RPCs, business effects and unproved cessation remain fenced.
"""
from copy import deepcopy
from datetime import datetime, timezone
import json
import time

from .codex_capacity import CodexCapacityRecovery, terminal_evidence
from .codex_timeout import prepare_settled_timeout_recovery
from .mailbox import IntegrationError, Mailbox, digest, encode

ALLOWED_ERRORS = frozenset({'serverOverloaded', 'usageLimitExceeded',
    'responseTooManyFailedAttempts', 'httpConnectionFailed', 'internalServerError'})
BACKOFF = [60, 180, 300]


def prepare_settled_provider_recovery(scope, *, run_id, workspace, rooms, seats, dispatch_sender, dispatch_room, dispatch_sha256):
    # Reuse scope/subset/canonical validation; this does not grant timeout recovery.
    checked = prepare_settled_timeout_recovery({k: v for k, v in scope.items() if k != 'settled_timeout_recovery'}, run_id=run_id, workspace=workspace, rooms=rooms, seats=seats)
    if (not isinstance(dispatch_sender, str) or not dispatch_sender.strip() or dispatch_room not in rooms or
            not isinstance(dispatch_sha256, str) or len(dispatch_sha256) != 64 or any(c not in '0123456789abcdef' for c in dispatch_sha256)):
        raise IntegrationError('PROVIDER_DISPATCH_BINDING_REQUIRED')
    cap = {**checked['settled_timeout_recovery'], 'max_attempts': 3, 'backoff_s': list(BACKOFF)}
    binding = {'sender_id': dispatch_sender, 'room_id': dispatch_room, 'content_sha256': dispatch_sha256}
    result = deepcopy(scope)
    for key, value in [('settled_provider_recovery', cap), ('provider_dispatch', binding)]:
        if key in scope and scope[key] != value:
            raise IntegrationError('PROVIDER_PREPARATION_CONFLICT')
        result[key] = value
    return result


def policy(scope, router):
    cap = (scope or {}).get('settled_provider_recovery')
    binding = (scope or {}).get('provider_dispatch')
    if (not isinstance(cap, dict) or set(cap) != {'enabled', 'run_id', 'workspace', 'rooms', 'seats', 'max_attempts', 'backoff_s'} or
            cap['enabled'] is not True or cap['run_id'] != router.run_id or cap['workspace'] != router.workspace or
            type(cap['max_attempts']) is not int or cap['max_attempts'] != 3 or not isinstance(cap['backoff_s'], list) or
            any(type(v) is not int for v in cap['backoff_s']) or cap['backoff_s'] != BACKOFF or
            not isinstance(cap['rooms'], list) or not isinstance(cap['seats'], list) or router.room not in cap['rooms'] or router.seat not in cap['seats'] or
            not set(cap['rooms']) <= set(scope.get('rooms', [])) or not set(cap['seats']) <= set(scope.get('seats', [])) or
            not isinstance(binding, dict) or set(binding) != {'sender_id', 'room_id', 'content_sha256'} or
            binding['room_id'] not in cap['rooms'] or not isinstance(binding['sender_id'], str) or not binding['sender_id'] or
            not isinstance(binding['content_sha256'], str) or len(binding['content_sha256']) != 64 or any(c not in '0123456789abcdef' for c in binding['content_sha256'])):
        return None
    return {'capability': cap, 'dispatch_binding': binding}


def register_policy(box, router):
    with box.owner.transaction(box.owner.epoch) as db:
        db.execute('CREATE TABLE IF NOT EXISTS c_provider_policy(run_id TEXT PRIMARY KEY, body TEXT, hash TEXT, registered_at REAL)')
        db.execute('CREATE TABLE IF NOT EXISTS c_run_dispatch(run_id TEXT PRIMARY KEY, policy_hash TEXT, platform_id TEXT, dispatched_at REAL, evidence_ref TEXT)')
        value = policy(router._scope(db), router)
        if value is None:
            return
        raw = encode(value)
        old = db.execute('SELECT * FROM c_provider_policy WHERE run_id=?', (router.run_id,)).fetchone()
        if old and old['body'] != raw:
            raise IntegrationError('PROVIDER_POLICY_CHANGED')
        db.execute('INSERT OR IGNORE INTO c_provider_policy VALUES(?,?,?,?)', (router.run_id, raw, digest(raw.encode()), time.time()))


def bind_dispatch(box, router, msg):
    """Called only from authenticated SDK room intake, never a model tool."""
    if msg.sender_type not in {'user', 'human'}:
        return
    with box.owner.transaction(box.owner.epoch) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='c_provider_policy'").fetchone():
            return
        pinned = db.execute('SELECT * FROM c_provider_policy WHERE run_id=?', (router.run_id,)).fetchone()
        value = policy(router._scope(db), router)
        if not pinned or value is None or encode(value) != pinned['body']:
            return
        binding = value['dispatch_binding']
        if (msg.sender_id, msg.room_id, digest(msg.content.encode())) != (binding['sender_id'], binding['room_id'], binding['content_sha256']):
            return
        if msg.created_at.tzinfo is None:
            raise IntegrationError('DISPATCH_UTC_TIMESTAMP_REQUIRED')
        at = msg.created_at.timestamp()
        old = db.execute('SELECT * FROM c_run_dispatch WHERE run_id=?', (router.run_id,)).fetchone()
        if old:
            if old['platform_id'] != msg.id or old['dispatched_at'] != at or old['policy_hash'] != pinned['hash']:
                raise IntegrationError('DISPATCH_RECEIPT_CONFLICT')
            return
        if at < pinned['registered_at'] or at > time.time():
            raise IntegrationError('DISPATCH_POLICY_NOT_PREREGISTERED')
        raw = encode({'run_id': router.run_id, 'policy_hash': pinned['hash'], 'platform_id': msg.id, 'sender_id': msg.sender_id, 'room_id': msg.room_id, 'content_sha256': binding['content_sha256'], 'dispatched_at': at})
        ref = Mailbox.artifact(db, raw.encode())
        db.execute('INSERT INTO c_run_dispatch VALUES(?,?,?,?,?)', (router.run_id, pinned['hash'], msg.id, at, ref))
        Mailbox.event(db, None, 'PROVIDER_DISPATCH_BOUND', {'run_id': router.run_id, 'evidence_ref': ref, 'deadline': at + 8 * 3600})


class CodexProviderRecovery(CodexCapacityRecovery):
    classification = 'SETTLED_NATIVE_PROVIDER_FAILURE'
    settled_event = 'SETTLED_NATIVE_PROVIDER'
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        with self.owner.transaction(self.owner.epoch) as db:
            db.execute('CREATE TABLE IF NOT EXISTS c_provider_retry(operation TEXT PRIMARY KEY, attempt TEXT, child TEXT UNIQUE, root TEXT, ordinal INTEGER, not_before REAL)')

    def _authorized(self, db, operation):
        value = policy(self.router._scope(db), self.router)
        if value is None or not db.execute("SELECT 1 FROM sqlite_master WHERE name='c_provider_policy'").fetchone():
            return False
        pinned = db.execute('SELECT * FROM c_provider_policy WHERE run_id=?', (self.router.run_id,)).fetchone()
        return bool(pinned and pinned['body'] == encode(value) and pinned['hash'] == digest(pinned['body'].encode()))

    def _terminal(self, db, parent):
        return terminal_evidence(db, parent, self.router, provider=True)

    def _schedule(self, db, parent, terminal):
        dispatch = db.execute('SELECT * FROM c_run_dispatch WHERE run_id=?', (self.router.run_id,)).fetchone()
        pinned = db.execute('SELECT hash,body FROM c_provider_policy WHERE run_id=?', (self.router.run_id,)).fetchone()
        if not dispatch or not pinned or dispatch['policy_hash'] != pinned[0]:
            raise IntegrationError('PROVIDER_DISPATCH_PROOF_REQUIRED')
        artifact = db.execute('SELECT body FROM c_artifact WHERE hash=?', (dispatch['evidence_ref'],)).fetchone()
        if not artifact or digest(artifact[0]) != dispatch['evidence_ref']:
            raise IntegrationError('PROVIDER_DISPATCH_PROOF_REQUIRED')
        proof = json.loads(artifact[0])
        if proof.get('dispatched_at') != dispatch['dispatched_at'] or proof.get('policy_hash') != dispatch['policy_hash'] or proof.get('run_id') != self.router.run_id or proof.get('platform_id') != dispatch['platform_id']:
            raise IntegrationError('PROVIDER_DISPATCH_PROOF_REQUIRED')
        binding = json.loads(pinned['body'])['dispatch_binding']
        if any(proof.get(key) != value for key, value in binding.items()):
            raise IntegrationError('PROVIDER_DISPATCH_PROOF_REQUIRED')
        old = db.execute('SELECT * FROM c_provider_retry WHERE operation=?', (parent['id'],)).fetchone()
        if old:
            if old['attempt'] != parent['attempt']:
                raise IntegrationError('PROVIDER_LINEAGE_CONFLICT')
            return old['root'], old['ordinal'], old['not_before'], None
        # Traverse canonical recovery edges, including intervening timeout slices.
        root, ordinal, operation, seen = parent['id'], 0, parent['id'], set()
        while operation not in seen:
            seen.add(operation)
            prior = db.execute('SELECT * FROM c_provider_retry WHERE child=?', (operation,)).fetchone()
            if prior:
                root, ordinal = prior['root'], prior['ordinal']
                break
            link = db.execute('SELECT * FROM c_recovery WHERE continuation=?', (operation,)).fetchone()
            if not link:
                break
            raw = db.execute('SELECT body FROM c_artifact WHERE hash=?', (link['id'],)).fetchone()
            if not raw or digest(raw[0]) != link['id'] or raw[0].decode() != link['proof']:
                raise IntegrationError('PROVIDER_LINEAGE_CONFLICT')
            operation = link['operation']
        else:
            raise IntegrationError('PROVIDER_LINEAGE_CONFLICT')
        if ordinal >= 3:
            return root, ordinal, None, 'PROVIDER_ATTEMPTS_EXHAUSTED'
        due = time.time() + BACKOFF[ordinal]
        if terminal['provider_error'] == 'usageLimitExceeded':
            rates = []
            for row in db.execute("SELECT body FROM c_event WHERE operation=? AND kind='STDOUT_RPC' ORDER BY seq", (parent['id'],)):
                body = json.loads(row[0])
                data = body.get('data', {})
                frame = data.get('payload', {})
                if body.get('attempt') == parent['attempt'] and data.get('client_id') == terminal['client_id'] and frame.get('method') == 'account/rateLimits/updated':
                    rates.append(frame.get('params', {}).get('rateLimits'))
            if not rates or not isinstance(rates[-1], dict):
                raise IntegrationError('PROVIDER_RATE_RESET_REQUIRED')
            resets = [v.get('resetsAt') for v in rates[-1].values() if isinstance(v, dict) and type(v.get('usedPercent')) in (int, float) and v['usedPercent'] >= 100]
            if not resets or any(type(v) not in (int, float) or not 0 < v < float('inf') for v in resets):
                raise IntegrationError('PROVIDER_RATE_RESET_REQUIRED')
            due = max(due, *resets)
        if due > dispatch['dispatched_at'] + 8 * 3600:
            return root, ordinal, None, 'PROVIDER_RUN_DEADLINE_EXCEEDED'
        return root, ordinal + 1, due, None

    def recover(self, operation, attempt):
        # Validate dispatch/reset/scheduling evidence before the base class can
        # refine DELIVERY_UNKNOWN to RECONCILED. Missing reset data is not a
        # settled retry decision and must not silently release the seat fence.
        with self.owner.transaction(self.owner.epoch) as db:
            parent = self._parent(db, operation, attempt)
            self._schedule(db, parent, self._terminal(db, parent))
        return super().recover(operation, attempt)

    def resume(self, operation, attempt, evidence_id):
        # Reserve readiness before generic resume publishes the child. All existing
        # source, ACK, quiescence and hash readbacks still run in the base path.
        with self.owner.transaction(self.owner.epoch) as db:
            parent = self._parent(db, operation, attempt)
            terminal = self._terminal(db, parent)
            root, ordinal, due, failure = self._schedule(db, parent, terminal)
            if failure:
                result = {'classification': 'SETTLED_PROVIDER_FAILURE', 'effect': 'ZERO_EFFECTS_PROVEN', 'code': failure, 'evidence_id': evidence_id, 'replay': 'FENCED'}
                db.execute("UPDATE c_work SET state='FAILED',delivery='RETURNED',result=? WHERE id=? AND attempt=?", (encode(result), operation, attempt))
                Mailbox.event(db, operation, 'PROVIDER_TERMINAL_FAILURE', {'attempt': attempt, **result})
                return {'terminal': True, 'code': failure}
            child = digest(encode([operation, attempt, 'maintenance-continuation']).encode())
            db.execute('INSERT OR IGNORE INTO c_provider_retry VALUES(?,?,?,?,?,?)', (operation, attempt, child, root, ordinal, due))
            db.execute('INSERT OR REPLACE INTO c_retry_wait VALUES(?,0,?)', (child, due))
        result = super().resume(operation, attempt, evidence_id)
        with self.owner.transaction(self.owner.epoch) as db:
            Mailbox.event(db, operation, 'PROVIDER_RETRY_SCHEDULED', {'attempt': attempt, 'child': child, 'ordinal': ordinal, 'not_before': due, 'evidence_id': evidence_id})
        return {**result, 'backoff_s': max(0, due - time.time()), 'ordinal': ordinal}
