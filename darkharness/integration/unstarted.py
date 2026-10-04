"""Transactional NOT_STARTED proof. Absence of a reply is never a rejection."""
import json


def proven_unstarted(db, work, attempt):
    if work['attempt'] != attempt or work['delivery'] != 'DISPATCHING' or work['state'] != 'RUNNING':
        return False
    rows = db.execute('SELECT kind,body FROM c_event WHERE operation=? ORDER BY seq', (work['id'],)).fetchall()
    events = [(r['kind'], json.loads(r['body'])) for r in rows]
    events = [(k, b.get('data', b)) for k, b in events if b.get('attempt') == attempt]
    if not any(k == 'ATTEMPT_PROCESS_CLEAN' for k, b in events):
        return False
    if any(k in {'PROCESS_STOP_UNKNOWN', 'TURN_ACCEPTED'} for k, b in events):
        return False
    intents = [(i, b) for i, (k, b) in enumerate(events) if k == 'TURN_START_INTENT']
    starts = [(i, b) for i, (k, b) in enumerate(events) if k == 'STDIN_RPC' and b.get('payload', {}).get('method') == 'turn/start']
    if intents or starts:
        if len(intents) != 1 or len(starts) != 1:
            return False
        start_index, start = starts[0]
        frame = start['payload']
        if intents[0][0] >= start_index or type(frame.get('id')) not in (int, str) or not start.get('client_id'):
            return False
        replies = [(i, b['payload']) for i, (k, b) in enumerate(events) if k == 'STDOUT_RPC' and b.get('client_id') == start['client_id'] and type(b.get('payload', {}).get('id')) is type(frame['id']) and b.get('payload', {}).get('id') == frame['id']]
        if len(replies) != 1 or replies[0][0] <= start_index:
            return False
        reply = replies[0][1]
        error = reply.get('error')
        if not isinstance(error, dict) or type(error.get('code')) is not int or not isinstance(error.get('message'), str) or 'result' in reply:
            return False
    for table in ('c_callback', 'c_git_effect', 'c_verification_effect'):
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            if db.execute(f'SELECT 1 FROM {table} WHERE operation=? AND attempt=?', (work['id'], attempt)).fetchone():
                return False
    # Questions and outbox have no attempt column; operation-wide is conservative.
    if db.execute('SELECT 1 FROM c_question WHERE operation=?', (work['id'],)).fetchone():
        return False
    if db.execute("SELECT 1 FROM c_outbox WHERE operation=? AND state!='REJECTED'", (work['id'],)).fetchone():
        return False
    if db.execute("SELECT 1 FROM c_control WHERE operation=? AND attempt=? AND kind='cancel'", (work['id'], attempt)).fetchone():
        return False
    return True
