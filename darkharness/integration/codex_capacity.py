"""Controller-only recovery of capacity failure with no native/business effects.

This is not a timeout classifier or automatic retry policy. The canonical owner
trace, SDK source pin, existing run settings and Recovery readbacks are required;
the native terminal's unloaded item list is never evidence that no tools ran.
"""
from importlib.metadata import PackageNotFoundError, version
import json
import math
from pathlib import Path

from .codex_timeout import SDK_SHA256
from .mailbox import IntegrationError, Mailbox, digest, encode
from .recovery import Recovery


CLASSIFICATION = 'SETTLED_NATIVE_CAPACITY_FAILURE'
REPORT_TYPES_SHA256 = '7c3d0de3961db983f80aa059a6eecd9f450258cd3df686bffd80dd23dcf15309'
REPORT_PROTOCOLS_SHA256 = '37d21f5941f9c39a72e82be5fc9121871d0b9d82b4da1e4351811dfaffd61fdf'
CAPACITY_ERROR = {'message': 'Selected model is at capacity. Please try a different model.',
                  'codexErrorInfo': 'serverOverloaded', 'additionalDetails': None,
                  'misalignment': None}


def verify_sdk_pin():
    try:
        from band.adapters import codex
        from band.integrations.codex import types
        from band.core import protocols
        matched = (version('band-sdk') == '4.0.0' and digest(Path(codex.__file__).read_bytes()) == SDK_SHA256 and
                   digest(Path(types.__file__).read_bytes()) == REPORT_TYPES_SHA256 and
                   digest(Path(protocols.__file__).read_bytes()) == REPORT_PROTOCOLS_SHA256)
    except (ImportError, PackageNotFoundError, OSError):
        matched = False
    if not matched:
        raise IntegrationError('CAPACITY_SDK_PIN_MISMATCH')


def terminal_evidence(db, parent, router, *, provider=False):
    """Read-only native proof; recover persists its canonical references later."""
    verify_sdk_pin()
    try:
        return _terminal_evidence(db, parent, router, provider=provider)
    except (KeyError, TypeError, ValueError, AttributeError):
        raise IntegrationError('CAPACITY_CANONICAL_EVIDENCE_MALFORMED') from None


def _sdk_reports(db, parent, router, rows, events, start, accepted, completed, outcome, runtime, stopped):
    """Exact ACKed SDK reporting, never arbitrary ACKed business sends.

    Reconstruct SDK4 report envelopes from independent native/owner evidence.
    Zero token delta is the observed narrow capacity case; historical cumulative
    counters are retained as reporting, never called newly executed model work.
    """
    operation, attempt = parent['id'], parent['attempt']
    thread, turn = accepted[1]['session'], accepted[1]['turn']
    room = router.room
    outbox = db.execute('SELECT * FROM c_outbox WHERE operation=? ORDER BY rowid', (operation,)).fetchall()
    whole = [(r, json.loads(r['body'])) for r in rows]
    intents = [(r, b) for r, b in whole if r['kind'] == 'SEND_INTENT']
    acks = [(r, b) for r, b in whole if r['kind'] == 'SEND_ACK']
    if len(intents) != len(outbox) or len(acks) != len(outbox):
        raise IntegrationError('CAPACITY_REPORT_EVIDENCE_MISMATCH')
    if not outbox:
        return [], []
    envelope = json.loads(parent['input'])
    original_message = (encode({'original_task': envelope['content'], 'peer_answers': envelope.get('peer_answers', []),
                                'recovery_receipt': envelope.get('recovery_receipt')})
                        if envelope.get('peer_answers') or envelope.get('recovery_receipt') else envelope['content'])
    base = {'codex_room_id': room, 'codex_thread_id': thread}
    turn_base = {**base, 'codex_turn_id': turn}
    duration = outcome[1]['duration_s']
    if type(duration) not in (int, float) or not math.isfinite(duration) or duration <= 0:
        raise IntegrationError('CAPACITY_REPORT_EVIDENCE_MISMATCH')
    candidates = []
    def candidate(name, content, message_type, metadata, after, before):
        candidates.append((name, {'content': content, 'message_type': message_type, 'metadata': metadata}, after, before))
    for r, b in events:
        frame = b.get('payload', {})
        if r['kind'] != 'STDIN_RPC' or frame.get('method') != 'thread/resume':
            continue
        replies = [(rr, bb['payload']) for rr, bb in events if rr['kind'] == 'STDOUT_RPC' and
                   bb.get('payload', {}).get('id') == frame['id'] and 'result' in bb['payload']]
        if len(replies) != 1 or replies[0][1]['result']['thread']['id'] != thread:
            raise IntegrationError('CAPACITY_REPORT_EVIDENCE_MISMATCH')
        candidate('thread_resumed', f'UUID: {thread}\nTask: Codex thread\nStatus: resumed\nSummary: Room: {room}',
                  'task', {**base, 'codex_resumed': True}, replies[0][0]['seq'], start[0]['seq'])
    candidate('turn_started', f'UUID: {turn}\nTask: Codex turn lifecycle\nStatus: started\nSummary: Thread: {thread}',
              'task', {**turn_base, 'codex_event_type': 'turn_lifecycle', 'codex_turn_status': 'started',
                       'codex_input_summary': original_message[:200]}, accepted[0]['seq'], outcome[0]['seq'])
    usages = []
    names = ('inputTokens', 'outputTokens', 'reasoningOutputTokens', 'totalTokens')
    for r, b in events:
        frame = b.get('payload', {})
        if r['kind'] != 'STDOUT_RPC' or frame.get('method') != 'thread/tokenUsage/updated':
            continue
        p = frame['params']
        if p['threadId'] != thread or (r['seq'] > start[0]['seq'] and p.get('turnId', turn) != turn):
            raise IntegrationError('CAPACITY_REPORT_EVIDENCE_MISMATCH')
        counters = tuple(p['tokenUsage']['total'][key] for key in names)
        if any(type(v) is not int or v < 0 for v in counters) or counters[-1] <= 0:
            raise IntegrationError('CAPACITY_REPORT_EVIDENCE_MISMATCH')
        usages.append((r['seq'], counters))
    usage_meta = {}
    if usages:
        if any(c != usages[0][1] for _, c in usages):
            raise IntegrationError('CAPACITY_REPORT_TOKEN_DELTA')
        inp, out, reasoning, total = usages[0][1]
        usage_meta = {'codex_event_type': 'token_usage', 'codex_input_tokens': inp,
                      'codex_output_tokens': out, 'codex_reasoning_tokens': reasoning, 'codex_total_tokens': total}
        for seq, _ in usages:
            candidate('token_usage', f'Token usage — input: {inp:,}, output: {out:,}, reasoning: {reasoning:,}, total: {total:,}',
                      'task', {**base, **usage_meta}, seq, outcome[0]['seq'])
    candidate('capacity_error', CAPACITY_ERROR['message'], 'error',
              {'failure': {'provider': 'codex', 'code': None, 'message': CAPACITY_ERROR['message'], 'detail': turn_base}},
              completed[0]['seq'], outcome[0]['seq'])
    candidate('turn_failed', f'UUID: {turn}\nTask: Codex turn lifecycle\nStatus: failed\nSummary: Duration: {duration:.1f}s | Thread: {thread}',
              'task', {**turn_base, 'codex_event_type': 'turn_lifecycle', 'codex_turn_status': 'failed',
                       'codex_duration_s': round(duration, 2), 'codex_error': CAPACITY_ERROR['message'], **usage_meta},
              outcome[0]['seq'], runtime[0]['seq'])
    reports, refs = [], []
    for counter, row in enumerate(outbox, 1):
        expected_id = digest(encode([operation, attempt, 'adapter', 'send_event', counter]).encode())
        raw = db.execute('SELECT body FROM c_artifact WHERE hash=?', (row['hash'],)).fetchone()
        if row['id'] != expected_id or row['state'] != 'ACKED' or not raw or digest(raw[0]) != row['hash']:
            raise IntegrationError('CAPACITY_REPORT_EVIDENCE_MISMATCH')
        body, receipt = json.loads(raw[0]), json.loads(row['receipt'])
        if (encode(body).encode() != raw[0] or set(receipt) != {'id', 'message_type', 'success'} or
                not isinstance(receipt['id'], str) or not receipt['id'] or receipt['success'] is not True or
                receipt['message_type'] != body.get('message_type')):
            raise IntegrationError('CAPACITY_REPORT_EVIDENCE_MISMATCH')
        intent = [(r, b) for r, b in intents if b == {'id': row['id'], 'hash': row['hash']}]
        ack = [(r, b) for r, b in acks if b == {'id': row['id'], 'receipt': receipt}]
        if len(intent) != 1 or len(ack) != 1 or not intent[0][0]['seq'] < ack[0][0]['seq'] < stopped[0]['seq']:
            raise IntegrationError('CAPACITY_REPORT_EVIDENCE_MISMATCH')
        match = next((i for i, (_, expected, after, before) in enumerate(candidates)
                      if body == expected and after < intent[0][0]['seq'] < before), None)
        if match is None:
            raise IntegrationError('CAPACITY_REPORT_NOT_SDK_TELEMETRY')
        name, _, _, _ = candidates.pop(match)
        reports.append({'outbox_id': row['id'], 'body_sha256': row['hash'], 'kind': name, 'effect': 'ACKNOWLEDGED_SDK_REPORT'})
        refs.extend({'seq': r['seq'], 'artifact_id': digest(r['body'].encode())} for r, _ in intent + ack)
    return reports, refs


def _terminal_evidence(db, parent, router, *, provider=False):
    operation, attempt = parent['id'], parent['attempt']
    rows = db.execute('SELECT * FROM c_event WHERE operation=? ORDER BY seq', (operation,)).fetchall()
    events = [(r, json.loads(r['body'])) for r in rows]
    events = [(r, b.get('data', b)) for r, b in events if b.get('attempt') == attempt]

    def one(kind, predicate=lambda b: True):
        found = [(r, b) for r, b in events if r['kind'] == kind and predicate(b)]
        if len(found) != 1:
            raise IntegrationError('CAPACITY_TERMINAL_EVIDENCE_REQUIRED')
        return found[0]

    accepted = one('TURN_ACCEPTED')
    thread, turn = accepted[1]['session'], accepted[1]['turn']
    if not turn or thread != parent['thread'] or accepted[1]['cwd'] != router.workspace:
        raise IntegrationError('CAPACITY_NATIVE_SCOPE_UNKNOWN')
    completed = one('STDOUT_RPC', lambda b: b.get('payload', {}).get('method') == 'turn/completed')
    client = completed[1]['client_id']
    params = completed[1]['payload']['params']
    native = params['turn']
    expected_error = native.get('error') if provider else CAPACITY_ERROR
    if provider:
        from .provider_recovery import provider_error_variant
        if not isinstance(expected_error, dict) or not isinstance(expected_error.get('message'), str) or not expected_error['message']:
            raise IntegrationError('NOT_ALLOWED_PROVIDER_ERROR')
        variant = provider_error_variant(expected_error.get('codexErrorInfo'))
        if variant is None:
            raise IntegrationError('NOT_ALLOWED_PROVIDER_ERROR')
    if (not client or params['threadId'] != thread or native['id'] != turn or
            native['status'] != 'failed' or native['error'] != expected_error):
        raise IntegrationError('NOT_NATIVE_CAPACITY_FAILURE')
    if not isinstance(native.get('items'), list) or any(i.get('type') != 'userMessage' for i in native['items']):
        raise IntegrationError('CAPACITY_EFFECT_OBSERVED')
    error = one('STDOUT_RPC', lambda b: b.get('payload', {}).get('method') == 'error')
    params = error[1]['payload']['params']
    if (params['threadId'] != thread or params['turnId'] != turn or
            params['error'] != expected_error or params['willRetry'] is not False):
        raise IntegrationError('CAPACITY_NATIVE_ERROR_MISMATCH')
    outcome = one('TURN_OUTCOME')
    b = outcome[1]
    if (b['thread_id'] != thread or b['turn_id'] != turn or b['room_id'] != router.room or
            b['turn_status'] != 'failed' or b['turn_error'] != expected_error['message'] or
            b['settled_reply'] is not False or b['include_reply'] is not False or b['final_text'] != ''):
        raise IntegrationError('CAPACITY_SDK_OUTCOME_MISMATCH')
    runtime = one('RUNTIME_ERROR')
    if runtime[1] != {'code': 'TurnResultAlreadyReported', 'diagnostic': expected_error['message'], 'replay': 'FENCED'}:
        raise IntegrationError('CAPACITY_RUNTIME_MISMATCH')
    stopped = one('PROCESS_STOPPED')
    if stopped[1] != {'client_id': client, 'payload': {'members': []}}:
        raise IntegrationError('CAPACITY_PROCESS_CESSATION_REQUIRED')
    start = one('STDIN_RPC', lambda b: b.get('payload', {}).get('method') == 'turn/start')
    params = start[1]['payload']['params']
    if (params['threadId'] != thread or params['cwd'] != router.workspace or
            params['approvalPolicy'] != 'on-request' or
            params['sandboxPolicy'].get('type') != 'workspaceWrite' or
            params['sandboxPolicy'].get('networkAccess', False) is not False or
            not isinstance(params.get('model'), str) or not params['model'] or
            not isinstance(params.get('effort'), str) or not params['effort'] or
            not isinstance(params.get('input'), list) or not params['input']):
        raise IntegrationError('CAPACITY_NATIVE_SCOPE_UNKNOWN')

    # Readiness can create a client before an operation is bound. Use the exact
    # native client/group identity, never a controller-supplied PID assertion.
    starts = []
    for r in db.execute("SELECT * FROM c_event WHERE kind='PROCESS_STARTED'"):
        body = json.loads(r['body'])
        data = body.get('data', {})
        if data.get('client_id') != client:
            continue
        p = data['payload']
        identity = p['identity']
        if (r['operation'] not in (None, operation) or body.get('attempt') not in (None, attempt) or
                p['cwd'] != router.workspace or not isinstance(identity, list) or len(identity) != 3 or
                type(identity[0]) is not int or identity[0] <= 0 or identity[0] != p['group'] or
                not all(isinstance(v, str) and v for v in identity[1:])):
            raise IntegrationError('CAPACITY_PROCESS_OWNERSHIP_UNKNOWN')
        starts.append((r, data))
    if len(starts) != 1:
        raise IntegrationError('CAPACITY_PROCESS_OWNERSHIP_UNKNOWN')
    chain = [starts[0], start, accepted, error, completed, outcome, runtime, stopped]
    seq = [r['seq'] for r, _ in chain]
    if seq != sorted(set(seq)):
        raise IntegrationError('CAPACITY_EVIDENCE_ORDER')
    owned = db.execute('SELECT * FROM c_owned_thread WHERE thread=? AND run_id=? AND seat=? AND room=?',
                       (thread, router.run_id, router.seat, router.room)).fetchone()
    if not owned:
        raise IntegrationError('CAPACITY_THREAD_NOT_OWNED')
    # Startup already refuses any change to this run's model/effort/connection.
    # Keep that immutable execution binding in the receipt; no replacement
    # model or config can be supplied to this recovery API.
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='c_run_settings'").fetchone():
        raise IntegrationError('CAPACITY_RUN_SETTINGS_REQUIRED')
    settings = db.execute('SELECT body,hash FROM c_run_settings WHERE run_id=?', (router.run_id,)).fetchone()
    if not settings or digest(settings['body'].encode()) != settings['hash']:
        raise IntegrationError('CAPACITY_RUN_SETTINGS_REQUIRED')
    pinned = json.loads(settings['body'])
    seats = [s for s in pinned['seats'] if s['alias'] == router.seat]
    if pinned['room'] != router.room or len(seats) != 1 or not seats[0]['settings_sha256']:
        raise IntegrationError('CAPACITY_RUN_SETTINGS_REQUIRED')
    binding = seats[0]['settings_sha256']
    if owned['binding'] != binding:
        raise IntegrationError('CAPACITY_BINDING_MISMATCH')
    binding_events = []
    for r in db.execute("SELECT * FROM c_event WHERE kind='RUNTIME_BINDINGS' ORDER BY seq"):
        body = json.loads(r['body'])
        if body.get('run_id') == router.run_id:
            binding_events.append((r, body))
    if not binding_events:
        raise IntegrationError('CAPACITY_BINDING_MISMATCH')
    runtime_binding = binding_events[-1]
    matches = [b for b in runtime_binding[1]['bindings'] if b.get('settings_sha256') == binding]
    if not matches or any((b.get('runtime'), b.get('workspace'), b.get('model'), b.get('effort')) !=
                          ('codex', router.workspace, params['model'], params['effort']) for b in matches):
        raise IntegrationError('CAPACITY_BINDING_MISMATCH')
    if (parent['state'] == 'CANCELLED' or
            db.execute("SELECT 1 FROM c_control WHERE operation=? AND (kind='cancel' OR state='PENDING')", (operation,)).fetchone() or
            db.execute('SELECT 1 FROM c_question WHERE operation=? AND answer IS NULL', (operation,)).fetchone()):
        raise IntegrationError('CAPACITY_CONTROL_PENDING_OR_CANCELLED')
    if provider:
        if db.execute('SELECT 1 FROM c_outbox WHERE operation=?', (operation,)).fetchone():
            raise IntegrationError('PROVIDER_BUSINESS_EFFECT_OBSERVED')
        reports, report_refs = [], []
    else:
        reports, report_refs = _sdk_reports(db, parent, router, rows, events, start, accepted, completed, outcome, runtime, stopped)
    for table in ('c_git_effect', 'c_callback', 'c_question'):
        if db.execute(f'SELECT 1 FROM {table} WHERE operation=?', (operation,)).fetchone():
            raise IntegrationError('CAPACITY_EFFECT_OBSERVED')
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='c_verification_effect'").fetchone():
        if db.execute("SELECT 1 FROM c_verification_effect v JOIN c_work w ON w.id=v.operation WHERE v.operation=? OR (w.seat=? AND v.state NOT IN ('SUCCEEDED','FAILED','CANCELLED','TIMED_OUT'))", (operation, router.seat)).fetchone():
            raise IntegrationError('CAPACITY_CHECKER_OUTSTANDING_OR_OBSERVED')

    notifications = {'configWarning', 'remoteControl/status/changed', 'deprecationNotice',
                     'mcpServer/startupStatus/updated', 'thread/status/changed',
                     'thread/tokenUsage/updated', 'thread/goal/cleared', 'thread/settings/updated',
                     'turn/started', 'account/updated', 'account/rateLimits/updated',
                     'item/started', 'item/completed', 'error', 'turn/completed'}
    pending_requests = {}
    for r, b in events:
        if r['kind'] in {'PROCESS_STOP_UNKNOWN', 'TURN_YIELDED', 'VERIFICATION_WAIT', 'PERMISSION_DECISION', 'CALLBACK_INTENT', 'DELIVERY_UNKNOWN'}:
            raise IntegrationError('CAPACITY_UNRESOLVED_EFFECT')
        if r['kind'] not in {'STDIN_RPC', 'STDOUT_RPC'}:
            continue
        if b['client_id'] != client:
            raise IntegrationError('CAPACITY_CLIENT_MISMATCH')
        frame = b['payload']
        method = frame.get('method', '')
        if r['kind'] == 'STDOUT_RPC' and method and frame.get('id') is not None:
            raise IntegrationError('CAPACITY_CALLBACK_OBSERVED')
        if method.endswith('requestApproval') or method == 'item/tool/requestUserInput':
            raise IntegrationError('CAPACITY_NEW_PERMISSION_OR_INPUT')
        if method.startswith('item/'):
            if method not in {'item/started', 'item/completed'}:
                raise IntegrationError('CAPACITY_EFFECT_OBSERVED')
            p = frame['params']
            if p['threadId'] != thread or p['turnId'] != turn or p['item']['type'] != 'userMessage':
                raise IntegrationError('CAPACITY_EFFECT_OBSERVED')
        if r['kind'] == 'STDIN_RPC':
            allowed_requests = {'initialize', 'initialized', 'thread/resume', 'turn/start'}
            if provider:
                allowed_requests.add('thread/start')
            if method not in allowed_requests:
                raise IntegrationError('CAPACITY_RPC_UNKNOWN')
            if method == 'thread/start':
                thread_params = frame['params']
                if (thread_params.get('cwd') != router.workspace or thread_params.get('model') != params['model'] or
                        thread_params.get('approvalPolicy') != 'on-request' or thread_params.get('sandbox') != 'workspace-write'):
                    raise IntegrationError('CAPACITY_NATIVE_SCOPE_UNKNOWN')
            if method != 'initialized':
                request_id = str(frame['id'])
                if request_id in pending_requests:
                    raise IntegrationError('CAPACITY_RPC_UNKNOWN')
                pending_requests[request_id] = method
            if method != 'turn/start' and r['seq'] >= start[0]['seq']:
                raise IntegrationError('CAPACITY_EVIDENCE_ORDER')
            if method == 'thread/resume' and frame['params']['threadId'] != thread:
                raise IntegrationError('CAPACITY_NATIVE_SCOPE_UNKNOWN')
        elif method:
            if method not in notifications:
                raise IntegrationError('CAPACITY_RPC_UNKNOWN')
        else:
            request = pending_requests.pop(str(frame.get('id')), None)
            if request is None or 'result' not in frame or 'error' in frame:
                raise IntegrationError('CAPACITY_RPC_UNKNOWN')
            if request == 'turn/start' and frame['result']['turn']['id'] != turn:
                raise IntegrationError('CAPACITY_NATIVE_SCOPE_UNKNOWN')
            if request in {'thread/start', 'thread/resume'} and frame['result']['thread']['id'] != thread:
                raise IntegrationError('CAPACITY_NATIVE_SCOPE_UNKNOWN')
    if pending_requests:
        raise IntegrationError('CAPACITY_RPC_UNSETTLED')

    # Native usage remains independently checked with SDK telemetry disabled.
    counters = []
    for r, b in events:
        frame = b.get('payload', {})
        if r['kind'] == 'STDOUT_RPC' and frame.get('method') == 'thread/tokenUsage/updated':
            usage = frame['params']
            if usage['threadId'] != thread or usage.get('turnId', turn) != turn:
                raise IntegrationError('CAPACITY_NATIVE_SCOPE_UNKNOWN')
            values = tuple(usage['tokenUsage']['total'][k] for k in ('inputTokens', 'outputTokens', 'reasoningOutputTokens', 'totalTokens'))
            if any(type(v) is not int or v < 0 for v in values):
                raise IntegrationError('CAPACITY_REPORT_TOKEN_DELTA')
            counters.append(values)
    if counters and any(v != counters[0] for v in counters):
        raise IntegrationError('CAPACITY_REPORT_TOKEN_DELTA')

    # Include all observed attempt events, not only the short terminal chain:
    # the full stream is what excludes callbacks and native tool execution.
    refs = [{'seq': r['seq'], 'artifact_id': digest(r['body'].encode())} for r, _ in events]
    if starts[0][0]['seq'] not in {ref['seq'] for ref in refs}:
        r = starts[0][0]
        refs.insert(0, {'seq': r['seq'], 'artifact_id': digest(r['body'].encode())})
    r = runtime_binding[0]
    refs.append({'seq': r['seq'], 'artifact_id': digest(r['body'].encode())})
    refs.extend(report_refs)
    return {'classification': 'SETTLED_NATIVE_PROVIDER_FAILURE' if provider else CLASSIFICATION, 'thread': thread, 'turn': turn, 'client_id': client,
            'model': params['model'], 'effort': params['effort'], 'run_settings_sha256': settings['hash'],
            'sdk_sha256': SDK_SHA256, 'report_types_sha256': REPORT_TYPES_SHA256,
            'report_protocols_sha256': REPORT_PROTOCOLS_SHA256, 'acknowledged_sdk_reports': reports, 'evidence_refs': refs, **({'provider_error': variant} if provider else {})}


class CodexCapacityRecovery(Recovery):
    classification = CLASSIFICATION
    settled_event = 'SETTLED_NATIVE_CAPACITY'
    def _terminal(self, db, parent):
        return terminal_evidence(db, parent, self.router)

    def _authorized(self, db, operation):
        scope = self.router._scope(db)
        cap = (scope or {}).get('continuation')
        # Every failed operation needs its own explicit controller binding.
        # Neither automatic timeout authority nor a parent's grant is inherited.
        operations = cap.get('operations') if isinstance(cap, dict) else None
        return isinstance(operations, list) and operation in operations

    def _parent(self, db, operation, attempt):
        if not self._authorized(db, operation):
            raise IntegrationError('CONTINUATION_GRANT_REQUIRED')
        run = db.execute("SELECT body FROM controls WHERE kind='run' AND id=?", (self.router.run_id,)).fetchone()
        if run and json.loads(run[0]).get('state') in {'UNKNOWN', 'DELIVERY_UNKNOWN', 'EFFECT_UNKNOWN', 'CLOSED_UNRESOLVED'}:
            raise IntegrationError('CAPACITY_RUN_UNRESOLVED')
        parent = super()._parent(db, operation, attempt, settled_interruption=True)
        self._terminal(db, parent)
        return parent

    def recover(self, operation, attempt):
        with self.owner.transaction(self.owner.epoch) as db:
            parent = self._parent(db, operation, attempt)
            old = db.execute('SELECT * FROM c_recovery WHERE operation=? AND attempt=?', (operation, attempt)).fetchone()
            prior_result = json.loads(parent['result']) if parent['result'] else {}
            if old and prior_result.get('classification') == self.classification and prior_result.get('evidence_id') == old['id']:
                observed = {'evidence_id': old['id']}
            else:
                observed = None
        if observed is None:
            observed = self.observe(operation, attempt)
            with self.owner.transaction(self.owner.epoch) as db:
                parent = self._parent(db, operation, attempt)
                terminal = self._terminal(db, parent)
                for ref in terminal['evidence_refs']:
                    raw = db.execute('SELECT body FROM c_event WHERE seq=?', (ref['seq'],)).fetchone()[0].encode()
                    if Mailbox.artifact(db, raw) != ref['artifact_id']:
                        raise IntegrationError('CAPACITY_CANONICAL_EVIDENCE_MALFORMED')
                receipt = {**observed, **terminal, 'acceptance': 'NOT_EVALUATED', 'replay': 'DO_NOT_REPEAT_ACKED_EFFECTS'}
                db.execute("UPDATE c_work SET state='FAILED',delivery='RECONCILED',result=? WHERE id=? AND attempt=?", (encode(receipt), operation, attempt))
                db.execute("UPDATE c_inbox SET life='RECONCILED' WHERE work=?", (operation,))
                Mailbox.event(db, operation, self.settled_event, {'attempt': attempt, **receipt})
        return self.resume(operation, attempt, observed['evidence_id'])
