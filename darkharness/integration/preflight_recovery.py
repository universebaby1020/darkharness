"""Controller-only append-only correction proof and original review continuation."""
import json
from pathlib import Path
from .mailbox import Mailbox, IntegrationError, digest, encode
from .verification import _hash_file
from .checker_preflight import official_output_exists


def reconcile_preflight(bridge, recovery, operation, attempt, effect, *, recognizer=official_output_exists):
    """recognizer is a trusted code seam for fixtures, NEVER a request parameter."""
    try:
        return _reconcile(bridge, recovery, operation, attempt, effect, recognizer)
    except (KeyError, TypeError, ValueError, IndexError, OSError):
        raise IntegrationError('PREFLIGHT_EVIDENCE_MALFORMED') from None


def _reconcile(bridge, recovery, operation, attempt, effect, recognizer):
    box, broker, router = bridge.box, bridge.broker, bridge.router
    recovery._quiescent()
    with box.owner.transaction(box.owner.epoch) as db:
        parent = broker._work(db, operation, attempt)
        row, result, old_ref = bridge._result(db, effect)
        if row['operation'] != operation or row['attempt'] != attempt or row['run_id'] != router.run_id:
            raise IntegrationError('OWNER_ATTEMPT_FENCE')
        scope = router._scope(db)
        request, configuration = json.loads(row['request']), json.loads(row['config'])
        cfg, restriction = configuration['check'], configuration['restriction']
        current = (scope or {}).get('verification', {}).get('checks', {}).get(request['check_id'])
        # Only output ownership layout may change. Historical config digest stays
        # authoritative for the old effect; the current scoped Grant is required.
        authority = lambda c: {k: v for k, v in c.items() if k != 'output_layout'}
        cap = (scope or {}).get('verification', {})
        binding = cap.get('receipts', {}).get(request['checkout_receipt']) if 'receipts' in cap else None
        run = db.execute("SELECT body FROM controls WHERE kind='run' AND id=?", (router.run_id,)).fetchone()
        if (not scope or not isinstance(current, dict) or authority(current) != authority(cfg) or binding != restriction or
                request.get('grant') != router.grant_id or request.get('run') != router.run_id or
                run and json.loads(run[0]).get('state') in {'UNKNOWN','DELIVERY_UNKNOWN','EFFECT_UNKNOWN','CLOSED_UNRESOLVED'}):
            raise IntegrationError('PREFLIGHT_CURRENT_AUTHORITY_REQUIRED')
        if (db.execute("SELECT 1 FROM c_control WHERE operation=? AND (kind='cancel' OR state='PENDING')", (operation,)).fetchone() or
                db.execute("SELECT 1 FROM c_question WHERE operation=? AND answer IS NULL", (operation,)).fetchone() or
                db.execute("SELECT 1 FROM c_outbox o JOIN c_work w ON w.id=o.operation WHERE w.seat=? AND o.state NOT IN ('ACKED','REJECTED')", (router.seat,)).fetchone() or
                db.execute("SELECT 1 FROM c_git_effect WHERE state!='ACKED'").fetchone() or
                db.execute("SELECT 1 FROM c_work WHERE seat=? AND id!=? AND delivery IN ('DISPATCHING','STARTED','DELIVERY_UNKNOWN')", (router.seat, operation)).fetchone() or
                db.execute("SELECT 1 FROM c_verification_effect WHERE run_id=? AND id!=? AND state NOT IN ('SUCCEEDED','FAILED','CANCELLED','TIMED_OUT')", (router.run_id, effect)).fetchone()):
            raise IntegrationError('PREFLIGHT_PENDING_OR_UNKNOWN')
        old = db.execute('SELECT * FROM c_verification_continuation WHERE effect=?', (effect,)).fetchone()
        if old:
            correction = db.execute("SELECT body FROM c_event WHERE operation=? AND kind='VERIFICATION_PREFLIGHT_CORRECTED'", (operation,)).fetchall()
            if old['result_ref'] != old_ref or not any(json.loads(r[0]).get('result_ref') == old_ref and json.loads(r[0]).get('effect') == effect for r in correction):
                raise IntegrationError('PREFLIGHT_CORRECTION_CONFLICT')
            return {'id': old['child'], 'effect_id': effect, 'result_ref': old_ref, 'classification': 'VERIFIED_PREEXECUTION_REFUSAL'}
        if row['state'] != 'UNKNOWN' or result.get('state') != 'UNKNOWN' or parent['state'] != 'PAUSED' or parent['delivery'] != 'DELIVERY_UNKNOWN' or not parent['thread']:
            raise IntegrationError('PREFLIGHT_STATE_CONFLICT')
        source, cwd, exe, _, executable = broker._configuration(cfg)
        if executable != configuration['executable']:
            raise IntegrationError('PREFLIGHT_EXECUTABLE_CHANGED')
        checkout, provenance = broker._receipt(db, operation, attempt, request['checkout_receipt'], request['revision'], restriction, cfg)
        if provenance != configuration['provenance'] or result.get('checkout_receipt') != request['checkout_receipt'] or result.get('revision') != request['revision'] or result.get('source_commit_effect') != provenance['commit_effect'] or result.get('output') != row['output']:
            raise IntegrationError('PREFLIGHT_RESULT_BINDING')
        argv = [str(exe), *[str(checkout) if x == '{checkout}' else row['output'] if x == '{output}' else x for x in cfg['argv']]]
        refs = []
        def one(kind, predicate):
            found = []
            for e in db.execute('SELECT * FROM c_event WHERE operation=? AND kind=? ORDER BY seq', (operation, kind)):
                b = json.loads(e['body'])
                if predicate(b):
                    found.append((e, b))
            if len(found) != 1:
                raise IntegrationError('PREFLIGHT_CANONICAL_EVENT_REQUIRED')
            e, b = found[0]
            h = Mailbox.artifact(db, e['body'].encode())
            refs.append({'seq': e['seq'], 'artifact_id': h})
            return e['seq'], b
        intent = one('VERIFICATION_INTENT', lambda b: b.get('id') == effect)
        if intent[1] != {'attempt': attempt, 'id': effect, 'request': request, 'config_sha256': digest(row['config'].encode()), 'output': row['output'], 'effects': cfg['effects'], 'limit_source': cfg.get('limit_source')}:
            raise IntegrationError('PREFLIGHT_INTENT_MISMATCH')
        spawn = one('VERIFICATION_SPAWN', lambda b: b.get('id') == effect)
        proc = json.loads(row['process'])
        if spawn[1] != {'id': effect, 'process': proc, 'argv': argv, 'cwd': str(cwd), 'source_head': cfg['source_head'], 'source_tree': cfg['source_tree'], 'executable': executable}:
            raise IntegrationError('PREFLIGHT_SPAWN_MISMATCH')
        recorded = one('VERIFICATION_RESULT', lambda b: b.get('id') == effect and b.get('result_ref') == old_ref)
        if recorded[1] != {'attempt': attempt, 'id': effect, 'result_ref': old_ref, 'state': 'UNKNOWN'} or not intent[0] < spawn[0] < recorded[0]:
            raise IntegrationError('PREFLIGHT_RESULT_MISMATCH')
        # No signalling of a persisted PID. Any surviving group or matching
        # leader identity fails; native cessation is additionally proven below.
        from .codex import group_members
        if type(proc.get('pid')) is not int or proc['pid'] != proc.get('pgid') or not proc.get('start_ticks') or not proc.get('owner') or group_members(proc['pgid']):
            raise IntegrationError('PREFLIGHT_PROCESS_OUTSTANDING')
        stat = Path('/proc') / str(proc['pid']) / 'stat'
        if stat.exists() and stat.read_text().rsplit(')', 1)[1].split()[19] == proc['start_ticks']:
            raise IntegrationError('PREFLIGHT_PROCESS_OUTSTANDING')
        yield_event = one('VERIFICATION_YIELD', lambda b: b.get('attempt') == attempt and b.get('data', {}).get('effect') == effect)
        wait = one('VERIFICATION_WAIT', lambda b: b.get('attempt') == attempt and b.get('data', {}).get('effect') == effect)
        accepted = one('TURN_ACCEPTED', lambda b: b.get('attempt') == attempt and b.get('data', {}).get('session') == parent['thread'] and b['data'].get('cwd') == router.workspace)
        turn = accepted[1]['data']['turn']
        interrupted = one('STDOUT_RPC', lambda b: b.get('attempt') == attempt and b.get('data', {}).get('payload', {}).get('method') == 'turn/completed' and b['data']['payload'].get('params', {}).get('threadId') == parent['thread'] and b['data']['payload']['params'].get('turn', {}).get('id') == turn)
        terminal = interrupted[1]['data']['payload']['params']['turn']
        client = interrupted[1]['data']['client_id']
        interrupt = one('STDIN_RPC', lambda b: b.get('attempt') == attempt and b.get('data', {}).get('client_id') == client and b['data'].get('payload', {}).get('method') == 'turn/interrupt' and b['data']['payload'].get('params') == {'threadId': parent['thread'], 'turnId': turn})
        ack = one('STDOUT_RPC', lambda b: b.get('attempt') == attempt and b.get('data', {}).get('client_id') == client and b['data'].get('payload', {}).get('id') == interrupt[1]['data']['payload']['id'] and b['data']['payload'].get('result') == {})
        stopped = one('PROCESS_STOPPED', lambda b: b.get('attempt') == attempt and b.get('data') == {'client_id': client, 'payload': {'members': []}})
        if (terminal.get('status') != 'interrupted' or terminal.get('error') is not None or not client or
                not accepted[0] < intent[0] < spawn[0] < interrupt[0] < ack[0] < wait[0] or
                not interrupt[0] < interrupted[0] < stopped[0] < wait[0] or not yield_event[0] < wait[0] or
                not db.execute('SELECT 1 FROM c_owned_thread WHERE thread=? AND run_id=? AND seat=? AND room=?', (parent['thread'], router.run_id, router.seat, router.room)).fetchone()):
            raise IntegrationError('PREFLIGHT_NATIVE_CESSATION_REQUIRED')
        logs = {}
        for name in ('stdout', 'stderr'):
            item = result['artifacts'][name]
            p = Path(row['output']) / name  # historical broker-owned layout only
            if item.get('path') != str(p) or item.get('bytes', 1048577) > 1048576 or _hash_file(p) != item:
                raise IntegrationError('PREFLIGHT_LOG_HASH_MISMATCH')
            raw = p.read_bytes()
            if len(raw) != item['bytes'] or digest(raw) != item['sha256']:
                raise IntegrationError('PREFLIGHT_LOG_HASH_MISMATCH')
            logs[name] = raw
        recognized = recognizer(cfg, argv, result, logs['stdout'], logs['stderr'])
        if not recognized or recognized.get('external_execution') != 'NOT_EXECUTED':
            raise IntegrationError('PREFLIGHT_NOT_RECOGNIZED')
        proof = {'schema': 'dh-preflight-correction-v1', 'effect': effect, 'operation': operation, 'attempt': attempt,
                 'run_id': router.run_id, 'grant_id': router.grant_id, 'original_result_ref': old_ref,
                 'original_config_sha256': digest(row['config'].encode()), 'current_check_sha256': digest(encode(current).encode()),
                 'evidence_refs': refs, 'process': proc, 'recognition': recognized, 'revision': request['revision'],
                 'checkout_receipt': request['checkout_receipt'], 'input_sha256': digest(parent['input'].encode())}
        proof_ref = Mailbox.artifact(db, encode(proof).encode())
        corrected = {**result, 'state': 'FAILED', 'reason': 'VERIFIED_PREEXECUTION_REFUSAL', 'accepted': False,
                     'external_execution': 'NOT_EXECUTED', 'preflight_proof': proof_ref, 'original_result_ref': old_ref,
                     'output_layout': 'broker-owned-v0'}
        ref = Mailbox.artifact(db, encode(corrected).encode())
        db.execute("UPDATE c_verification_effect SET state='FAILED',result=? WHERE id=?", (encode(corrected), effect))
        Mailbox.event(db, operation, 'VERIFICATION_PREFLIGHT_CORRECTED', {'attempt': attempt, 'effect': effect, 'original_result_ref': old_ref, 'proof_ref': proof_ref, 'result_ref': ref})
        child = bridge._queue(db, parent, corrected, ref, effect, attempt)
        return {'id': child, 'effect_id': effect, 'proof_ref': proof_ref, 'result_ref': ref, 'classification': 'VERIFIED_PREEXECUTION_REFUSAL'}
