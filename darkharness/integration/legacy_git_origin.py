"""Read-only legacy run proof from the historical authenticated execution ledger.

No current-Grant backfill, model run claims, receipt registration or new approval.
Human-prepared commits without a native creating turn are deliberately unproven.
"""
from __future__ import annotations

import json
from .mailbox import encode


def execution_ledger_run(db, effect):
    if effect.get('state') != 'ACKED' or not effect.get('receipt'):
        return None
    operation, attempt = effect['operation'], effect['attempt']
    request, receipt = json.loads(effect['request']), json.loads(effect['receipt'])
    source = request.get('identity', [None])[0]
    intents, acks, turns = [], [], []
    for row in db.execute("SELECT seq,kind,body FROM c_event WHERE operation=? AND kind IN ('LOCAL_GIT_INTENT','LOCAL_GIT_RECEIPT','TURN_ACCEPTED') ORDER BY seq", (operation,)):
        body = json.loads(row['body'])
        if body.get('attempt') != attempt:
            continue
        if row['kind'] == 'LOCAL_GIT_INTENT' and body.get('id') == effect['id'] and encode(body.get('request')) == effect['request']:
            intents.append(row['seq'])
        elif row['kind'] == 'LOCAL_GIT_RECEIPT' and body.get('id') == effect['id'] and encode(body.get('receipt')) == effect['receipt']:
            acks.append(row['seq'])
        elif row['kind'] == 'TURN_ACCEPTED':
            data = body.get('data', {})
            if data.get('cwd') == source and data.get('session') and data.get('turn'):
                turns.append(row['seq'])
    if len(intents) != 1 or len(acks) != 1:
        return None
    turn = max((seq for seq in turns if seq < intents[0]), default=None)
    if turn is None or acks[0] <= intents[0]:
        return None
    # Historical manager binding must precede the creating native turn. A
    # conflicting restart/run marker inside the effect interval fails closed.
    marker = db.execute("SELECT seq,body FROM c_event WHERE kind='RUNTIME_BINDINGS' AND seq<? ORDER BY seq DESC LIMIT 1", (turn,)).fetchone()
    if not marker:
        return None
    body = json.loads(marker['body'])
    run = body.get('run_id')
    bindings = body.get('bindings')
    if not isinstance(run, str) or not run or not isinstance(bindings, list) or not bindings or not all(b.get('workspace') == source for b in bindings):
        return None
    for newer in db.execute("SELECT body FROM c_event WHERE kind='RUNTIME_BINDINGS' AND seq>? AND seq<=?", (marker['seq'], acks[0])):
        if json.loads(newer[0]).get('run_id') != run:
            return None
    return run
