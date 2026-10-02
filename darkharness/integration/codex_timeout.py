"""SDK4-specific terminal evidence detection. Never parse a seat's safety claim.

The owner store and its event artifacts are authoritative (not a sandbox against
same-UID tampering). Generic Recovery remains independent of SDK error strings.
"""
from importlib.metadata import version
import json
from pathlib import Path
import re

from .mailbox import IntegrationError, Mailbox, digest, encode
from .recovery import Recovery

SDK_SHA256 = 'b3738db2e31376726e7edbe953c5f3ac5e26f2681066fb20a109a8a819529a4c'


def terminal_evidence(db, parent, router):
    try:
        return _terminal_evidence(db, parent, router)
    except (KeyError, TypeError, ValueError, AttributeError):
        raise IntegrationError('TIMEOUT_CANONICAL_EVIDENCE_MALFORMED') from None


def _terminal_evidence(db, parent, router):
    from band.adapters import codex
    if version('band-sdk') != '4.0.0' or digest(Path(codex.__file__).read_bytes()) != SDK_SHA256:
        raise IntegrationError('TIMEOUT_SDK_PIN_MISMATCH')
    operation, attempt = parent['id'], parent['attempt']
    rows = db.execute('SELECT * FROM c_event WHERE operation=? ORDER BY seq', (operation,)).fetchall()
    events = [(r, json.loads(r['body'])) for r in rows]
    events = [(r, b.get('data', b)) for r, b in events if b.get('attempt') == attempt]
    def one(kind, predicate):
        found = [(r, b) for r, b in events if r['kind'] == kind and predicate(b)]
        if len(found) != 1:
            raise IntegrationError('TIMEOUT_TERMINAL_EVIDENCE_REQUIRED')
        return found[0]
    accepted = one('TURN_ACCEPTED', lambda b: b.get('session') == parent['thread'] and b.get('cwd') == router.workspace)
    thread, turn = accepted[1]['session'], accepted[1]['turn']
    outcome = one('TURN_OUTCOME', lambda b: b.get('thread_id') == thread and b.get('turn_id') == turn and b.get('room_id') == router.room)
    error = outcome[1].get('turn_error') or ''
    match = re.fullmatch(r'Codex turn timed out after ([0-9]+(?:\.[0-9]+)?)s', error)
    if not match or float(match[1]) <= 0 or outcome[1].get('turn_status') != 'failed' or outcome[1].get('settled_reply') is not False:
        raise IntegrationError('NOT_SDK_TURN_TIMEOUT')
    runtime = one('RUNTIME_ERROR', lambda b: b.get('code') == 'TurnResultAlreadyReported' and b.get('diagnostic') == error)
    interrupted = one('STDOUT_RPC', lambda b: b.get('payload', {}).get('method') == 'turn/completed' and b['payload'].get('params', {}).get('threadId') == thread and b['payload']['params'].get('turn', {}).get('id') == turn)
    terminal = interrupted[1]['payload']['params']['turn']
    if terminal.get('status') != 'interrupted' or terminal.get('error') is not None:
        raise IntegrationError('TIMEOUT_NATIVE_TERMINAL_MISMATCH')
    client = interrupted[1].get('client_id')
    if not client:
        raise IntegrationError('TIMEOUT_CLIENT_REQUIRED')
    interrupt = one('STDIN_RPC', lambda b: b.get('client_id') == client and b.get('payload', {}).get('method') == 'turn/interrupt' and b['payload'].get('params') == {'threadId': thread, 'turnId': turn})
    ack = one('STDOUT_RPC', lambda b: b.get('client_id') == client and b.get('payload', {}).get('id') == interrupt[1]['payload']['id'] and b['payload'].get('result') == {})
    stopped = one('PROCESS_STOPPED', lambda b: b.get('client_id') == client and b.get('payload') == {'members': []})
    start = one('STDIN_RPC', lambda b: b.get('client_id') == client and b.get('payload', {}).get('method') == 'turn/start' and b['payload'].get('params', {}).get('threadId') == thread)
    params = start[1]['payload']['params']
    if params.get('cwd') != router.workspace or params.get('approvalPolicy') != 'on-request' or params.get('sandboxPolicy', {}).get('type') != 'workspaceWrite' or params['sandboxPolicy'].get('networkAccess', False) is not False:
        raise IntegrationError('TIMEOUT_NATIVE_SCOPE_UNKNOWN')
    # Readiness may create the client before an attempt is bound. Bind via the
    # unique client id, exact cwd, leader/group identity and later own raw stream.
    starts = []
    for r in db.execute("SELECT * FROM c_event WHERE kind='PROCESS_STARTED'"):
        b = json.loads(r['body'])
        data = b.get('data', {})
        if data.get('client_id') == client:
            p = data.get('payload', {})
            identity = p.get('identity')
            if r['operation'] not in (None, operation) or b.get('attempt') not in (None, attempt) or p.get('cwd') != router.workspace or not isinstance(identity, list) or len(identity) != 3 or identity[0] != p.get('group'):
                raise IntegrationError('TIMEOUT_PROCESS_OWNERSHIP_UNKNOWN')
            starts.append((r, data))
    if len(starts) != 1:
        raise IntegrationError('TIMEOUT_PROCESS_OWNERSHIP_UNKNOWN')
    chain = [starts[0], start, accepted, interrupt, interrupted, outcome, runtime, stopped]
    seq = [r['seq'] for r, _ in chain]
    if seq != sorted(set(seq)) or not interrupt[0]['seq'] < ack[0]['seq'] < outcome[0]['seq']:
        raise IntegrationError('TIMEOUT_EVIDENCE_ORDER')
    owned = db.execute('SELECT 1 FROM c_owned_thread WHERE thread=? AND run_id=? AND seat=? AND room=?', (thread, router.run_id, router.seat, router.room)).fetchone()
    if not owned:
        raise IntegrationError('TIMEOUT_THREAD_NOT_OWNED')
    if parent['state'] == 'CANCELLED' or db.execute("SELECT 1 FROM c_control WHERE operation=? AND (kind='cancel' OR state='PENDING')", (operation,)).fetchone() or db.execute('SELECT 1 FROM c_question WHERE operation=? AND answer IS NULL', (operation,)).fetchone():
        raise IntegrationError('TIMEOUT_CONTROL_PENDING_OR_CANCELLED')
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='c_verification_effect'").fetchone() and db.execute("SELECT 1 FROM c_verification_effect v JOIN c_work w ON w.id=v.operation WHERE w.seat=? AND v.state NOT IN ('SUCCEEDED','FAILED','CANCELLED','TIMED_OUT')", (router.seat,)).fetchone():
        raise IntegrationError('TIMEOUT_CHECKER_OUTSTANDING')
    pending, failures, callbacks = {}, [], set()
    safe_items = {'userMessage', 'agentMessage', 'reasoning', 'commandExecution', 'fileChange', 'dynamicToolCall', 'plan'}
    from .codex import READ_TOOLS, SEND_TOOLS, GIT_TOOLS
    safe_tools = READ_TOOLS | SEND_TOOLS | set(GIT_TOOLS) | {'dh_verification_read'}
    for r, b in events:
        if r['kind'] in {'PROCESS_STOP_UNKNOWN', 'TURN_YIELDED', 'VERIFICATION_WAIT'}:
            raise IntegrationError('TIMEOUT_UNRESOLVED_EFFECT')
        if r['kind'] not in {'STDIN_RPC', 'STDOUT_RPC'}:
            continue
        if b.get('client_id') != client:
            raise IntegrationError('TIMEOUT_CLIENT_MISMATCH')
        frame = b.get('payload', {})
        method = frame.get('method', '')
        if r['kind'] == 'STDOUT_RPC' and method and frame.get('id') is not None:
            callbacks.add(str(frame['id']))
        if r['kind'] == 'STDIN_RPC' and ('result' in frame or 'error' in frame):
            callbacks.discard(str(frame.get('id')))
        if method.endswith('requestApproval') or method == 'item/tool/requestUserInput':
            raise IntegrationError('TIMEOUT_NEW_PERMISSION_OR_INPUT')
        if method == 'item/tool/call' and frame.get('params', {}).get('tool') not in safe_tools:
            raise IntegrationError('TIMEOUT_EXTERNAL_EFFECT_UNKNOWN')
        if method in {'item/started', 'item/completed'}:
            item = frame['params']['item']
            if item.get('type') == 'dynamicToolCall' and item.get('tool') not in safe_tools:
                raise IntegrationError('TIMEOUT_EXTERNAL_EFFECT_UNKNOWN')
            kind = item.get('type')
            if kind not in safe_items:
                raise IntegrationError('TIMEOUT_EXTERNAL_EFFECT_UNKNOWN')
            if kind in {'commandExecution', 'fileChange', 'dynamicToolCall'}:
                if method == 'item/started':
                    pending[item['id']] = item
                else:
                    if item.get('status') not in {'completed', 'failed', 'declined'}:
                        raise IntegrationError('TIMEOUT_ITEM_UNSETTLED')
                    pending.pop(item['id'], None)
                    if item.get('status') != 'completed':
                        failures.append({'item': item['id'], 'type': kind, 'status': item['status'], 'exit_code': item.get('exitCode')})
    if pending or callbacks:
        raise IntegrationError('TIMEOUT_ITEM_UNSETTLED')
    refs = []
    for r, _ in chain + [ack]:
        ref = Mailbox.artifact(db, r['body'].encode())
        refs.append({'seq': r['seq'], 'artifact_id': ref})
    return {'classification': 'SETTLED_SDK_TURN_TIMEOUT', 'thread': thread, 'turn': turn, 'client_id': client,
            'observed_timeout_s': float(match[1]), 'sdk_sha256': SDK_SHA256, 'evidence_refs': refs, 'native_failures': failures}


class CodexTimeoutRecovery(Recovery):
    def automatic_allowed(self, db):
        scope = self.router._scope(db)
        cap = (scope or {}).get('settled_timeout_recovery')
        return (isinstance(cap, dict) and cap.get('enabled') is True and
                cap.get('run_id') == self.router.run_id and cap.get('workspace') == self.router.workspace and
                self.router.room in cap.get('rooms', []) and self.router.seat in cap.get('seats', []))

    def _lineage_parent(self, db, operation):
        row = db.execute('SELECT * FROM c_recovery WHERE continuation=?', (operation,)).fetchone()
        if row:
            artifact = db.execute('SELECT body FROM c_artifact WHERE hash=?', (row['id'],)).fetchone()
            proof = json.loads(row['proof'])
            if not artifact or digest(artifact[0]) != row['id'] or artifact[0].decode() != row['proof'] or (proof['run_id'], proof['grant_id'], proof['seat'], proof['room']) != (self.router.run_id, self.router.grant_id, self.router.seat, self.router.room):
                raise IntegrationError('CONTINUATION_LINEAGE_CONFLICT')
            return row['operation']
        q = db.execute('SELECT * FROM c_question WHERE continuation=?', (operation,)).fetchone()
        if q:
            if q['answer'] is None or operation != digest(encode([q['id'], 'continuation']).encode()):
                raise IntegrationError('CONTINUATION_LINEAGE_CONFLICT')
            return q['operation']
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='c_verification_continuation'").fetchone():
            link = db.execute('SELECT * FROM c_verification_continuation WHERE child=?', (operation,)).fetchone()
            if link:
                effect = db.execute('SELECT * FROM c_verification_effect WHERE id=?', (link['effect'],)).fetchone()
                artifact = db.execute('SELECT body FROM c_artifact WHERE hash=?', (link['result_ref'],)).fetchone()
                if not effect or effect['run_id'] != self.router.run_id or effect['state'] not in {'SUCCEEDED', 'FAILED'} or not effect['result'] or not artifact or artifact[0] != effect['result'].encode() or digest(artifact[0]) != link['result_ref'] or operation != digest(encode([link['effect'], link['result_ref'], 'verification-continuation']).encode()):
                    raise IntegrationError('CONTINUATION_LINEAGE_CONFLICT')
                result = json.loads(effect['result'])
                parent = db.execute('SELECT attempt FROM c_work WHERE id=?', (effect['operation'],)).fetchone()
                if result.get('run_id') != self.router.run_id or result.get('state') != effect['state'] or not parent or parent['attempt'] != effect['attempt']:
                    raise IntegrationError('CONTINUATION_LINEAGE_CONFLICT')
                return effect['operation']
        return None

    def _authorized(self, db, operation):
        if self.automatic_allowed(db):
            seen = set()
            while operation and operation not in seen:
                seen.add(operation)
                work = db.execute('SELECT * FROM c_work WHERE id=?', (operation,)).fetchone()
                if not work or work['seat'] != self.router.seat or work['room'] != self.router.room or not db.execute('SELECT 1 FROM c_owned_thread WHERE thread=? AND run_id=? AND seat=? AND room=?', (work['thread'], self.router.run_id, self.router.seat, self.router.room)).fetchone():
                    raise IntegrationError('CONTINUATION_LINEAGE_CONFLICT')
                if db.execute("SELECT 1 FROM c_control WHERE operation=? AND kind='cancel'", (operation,)).fetchone():
                    raise IntegrationError('TIMEOUT_CONTROL_PENDING_OR_CANCELLED')
                if db.execute('SELECT 1 FROM c_inbox WHERE work=? AND seat=? AND room=?', (operation, self.router.seat, self.router.room)).fetchone():
                    return True
                operation = self._lineage_parent(db, operation)
        return super()._authorized(db, operation)

    def _predecessors(self, db, operation):
        seen, ancestors = set(), []
        while operation and operation not in seen:
            seen.add(operation)
            row = db.execute('SELECT * FROM c_recovery WHERE continuation=?', (operation,)).fetchone()
            if row:
                self._lineage_parent(db, operation)  # exact artifact/run/Grant proof
                return row, ancestors
            operation = self._lineage_parent(db, operation)
            if operation:
                ancestors.append(operation)
        return None, ancestors

    def _parent(self, db, operation, attempt):
        run = db.execute("SELECT body FROM controls WHERE kind='run' AND id=?", (self.router.run_id,)).fetchone()
        if run and json.loads(run[0]).get('state') in {'UNKNOWN', 'DELIVERY_UNKNOWN', 'EFFECT_UNKNOWN', 'CLOSED_UNRESOLVED'}:
            raise IntegrationError('TIMEOUT_RUN_UNRESOLVED')
        parent = db.execute('SELECT * FROM c_work WHERE id=?', (operation,)).fetchone()
        if not parent or parent['attempt'] != attempt or parent['seat'] != self.router.seat or parent['room'] != self.router.room:
            raise IntegrationError('OWNER_ATTEMPT_FENCE')
        terminal_evidence(db, parent, self.router)
        return super()._parent(db, operation, attempt, settled_interruption=True)

    def reconcile(self, operation, attempt):
        with self.owner.transaction(self.owner.epoch) as db:
            old = db.execute('SELECT * FROM c_recovery WHERE operation=? AND attempt=?', (operation, attempt)).fetchone()
            if old:
                self._parent(db, operation, attempt)
                if old['continuation']:
                    return {'evidence_id': old['id'], 'classification': 'SETTLED_SDK_TURN_TIMEOUT', 'continuation': old['continuation']}
                result = db.execute('SELECT result FROM c_work WHERE id=?', (operation,)).fetchone()[0]
                result = json.loads(result) if result else {}
                if result.get('evidence_id') == old['id'] and result.get('classification') == 'SETTLED_SDK_TURN_TIMEOUT':
                    return result
        observed = self.observe(operation, attempt)
        with self.owner.transaction(self.owner.epoch) as db:
            parent = self._parent(db, operation, attempt)
            terminal = terminal_evidence(db, parent, self.router)
            receipt = {**observed, **terminal, 'acceptance': 'NOT_EVALUATED', 'replay': 'DO_NOT_REPEAT_ACKED_EFFECTS'}
            if parent['delivery'] != 'RECONCILED':
                db.execute("UPDATE c_work SET state='FAILED',delivery='RECONCILED',result=? WHERE id=? AND attempt=?", (encode(receipt), operation, attempt))
                db.execute("UPDATE c_inbox SET life='RECONCILED' WHERE work=?", (operation,))
                Mailbox.event(db, operation, 'SETTLED_TURN_TIMEOUT', {'attempt': attempt, **receipt})
        return receipt

    def recover(self, operation, attempt):
        receipt = self.reconcile(operation, attempt)
        if receipt.get('continuation'):
            return {'id': receipt['continuation'], 'parent_operation': operation, 'evidence_id': receipt['evidence_id']}
        with self.owner.transaction(self.owner.epoch) as db:
            predecessor, _ = self._predecessors(db, operation)
            previous = predecessor['id'] if predecessor else None
            current = json.loads(db.execute('SELECT proof FROM c_recovery WHERE id=?', (receipt['evidence_id'],)).fetchone()[0])
            if previous:
                prior = db.execute('SELECT proof FROM c_recovery WHERE id=?', (previous,)).fetchone()
                if not prior:
                    raise IntegrationError('CONTINUATION_LINEAGE_CONFLICT')
                # A fresh SDK error notification is not task progress. Only changed
                # canonical source or completed business tool calls qualify.
                prior_proof = json.loads(prior[0])
                previous_hashes = {o['hash'] for o in prior_proof.get('settled_outbox', [])}
                progress = db.execute("SELECT body FROM c_event WHERE operation=? AND kind='STDOUT_RPC'", (operation,)).fetchall()
                business = False
                for event in progress:
                    frame = json.loads(event[0]).get('data', {}).get('payload', {})
                    item = frame.get('params', {}).get('item', {})
                    if frame.get('method') == 'item/completed' and item.get('tool') == 'band_send_message' and item.get('success') is True:
                        h = digest(encode(item.get('arguments')).encode())
                        business |= h not in previous_hashes and bool(db.execute("SELECT 1 FROM c_outbox WHERE operation=? AND hash=? AND state='ACKED'", (operation, h)).fetchone())
                _, ancestors = self._predecessors(db, operation)
                for node in [operation] + ancestors:
                    business |= bool(db.execute('SELECT 1 FROM c_question WHERE continuation=? AND answer IS NOT NULL', (node,)).fetchone())
                    if db.execute("SELECT 1 FROM sqlite_master WHERE name='c_verification_continuation'").fetchone():
                        business |= bool(db.execute('SELECT 1 FROM c_verification_continuation WHERE child=?', (node,)).fetchone())
                if prior_proof['source']['sha256'] == current['source']['sha256'] and not business:
                    raise IntegrationError('TIMEOUT_NO_PROGRESS')
        return self.resume(operation, attempt, receipt['evidence_id'])
