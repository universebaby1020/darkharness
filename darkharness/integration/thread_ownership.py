"""Run/room/seat ownership, independent of untrusted SDK history metadata."""
from __future__ import annotations

import json
from .mailbox import IntegrationError, Mailbox, digest, encode


class ThreadOwnership:
    def __init__(self, box, router, binding=None):
        self.box, self.router, self.binding = box, router, binding
        with box.owner.transaction(box.owner.epoch) as db:
            db.execute('CREATE TABLE IF NOT EXISTS c_owned_thread(thread TEXT PRIMARY KEY, run_id TEXT, room TEXT, seat TEXT, compatibility TEXT, prompt_hash TEXT, history_ref TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS c_latest_thread(run_id TEXT, room TEXT, seat TEXT, thread TEXT, PRIMARY KEY(run_id,room,seat))')
            if 'binding' not in {r[1] for r in db.execute('PRAGMA table_info(c_owned_thread)')}:
                db.execute('ALTER TABLE c_owned_thread ADD COLUMN binding TEXT')

    def owned(self, thread):
        with self.box.owner.transaction(self.box.owner.epoch) as db:
            row = db.execute('SELECT * FROM c_owned_thread WHERE thread=? AND run_id=? AND room=? AND seat=?', (thread, self.router.run_id, self.router.room, self.router.seat)).fetchone()
            if row and (self.binding is None or row['binding'] == self.binding):
                return dict(row)
            return None

    def latest(self, parent_thread=None):
        with self.box.owner.transaction(self.box.owner.epoch) as db:
            # Authoritative latest precedes a stale continuation parent.
            latest = db.execute('SELECT thread FROM c_latest_thread WHERE run_id=? AND room=? AND seat=?', (self.router.run_id, self.router.room, self.router.seat)).fetchone()
            thread = latest[0] if latest else parent_thread
            row = db.execute('SELECT * FROM c_owned_thread WHERE thread=? AND run_id=? AND room=? AND seat=?', (thread, self.router.run_id, self.router.room, self.router.seat)).fetchone()
            # Unscoped legacy sessions migrate to a fresh thread, retaining own
            # durable tasks but never probing history under a different login.
            return dict(row) if row and (self.binding is None or row['binding'] == self.binding) else None

    def bind(self, thread, compatibility):
        with self.box.owner.transaction(self.box.owner.epoch) as db:
            old = db.execute('SELECT * FROM c_owned_thread WHERE thread=?', (thread,)).fetchone()
            if old and (old['run_id'], old['room'], old['seat']) != (self.router.run_id, self.router.room, self.router.seat):
                raise IntegrationError('CROSS_SEAT_THREAD_DENIED')
            if old and self.binding is not None and old['binding'] != self.binding:
                raise IntegrationError('CROSS_BINDING_THREAD_DENIED')
            db.execute('INSERT OR IGNORE INTO c_owned_thread(thread,run_id,room,seat,compatibility,prompt_hash,history_ref,binding) VALUES(?,?,?,?,?,NULL,NULL,?)', (thread, self.router.run_id, self.router.room, self.router.seat, compatibility, self.binding))
            db.execute('INSERT OR REPLACE INTO c_latest_thread VALUES(?,?,?,?)', (self.router.run_id, self.router.room, self.router.seat, thread))

    def prompted(self, thread, prompt_hash):
        with self.box.owner.transaction(self.box.owner.epoch) as db:
            db.execute('UPDATE c_owned_thread SET prompt_hash=? WHERE thread=? AND run_id=? AND room=? AND seat=?', (prompt_hash, thread, self.router.run_id, self.router.room, self.router.seat))

    def history(self, operation, native_history=None):
        """Full durable own task chain + owned native history, as data not replay."""
        with self.box.owner.transaction(self.box.owner.epoch) as db:
            rows = db.execute('SELECT w.* FROM c_work w LEFT JOIN c_owned_thread t ON t.thread=w.thread WHERE w.seat=? AND w.room=? AND w.rowid < (SELECT rowid FROM c_work WHERE id=?) AND (t.run_id IS NULL OR t.run_id=?) ORDER BY w.rowid', (self.router.seat, self.router.room, operation, self.router.run_id)).fetchall()
            # The shared run store's own inputs are durable, not sender metadata.
            messages = [{'operation': r['id'], 'input': json.loads(r['input']), 'state': r['state'],
                         'result': json.loads(r['result']) if r['result'] else None} for r in rows if r['id'] != operation]
            body = {'prior_own_tasks': messages, 'owned_native_history': native_history,
                    'use': 'Context/evidence only. Do not replay historical tool calls or effects. Continue the original assignment and report actual accepted revision/evidence to its original requester.'}
            ref = Mailbox.artifact(db, encode(body).encode())
            Mailbox.event(db, operation, 'OWNED_HISTORY_LINK', {'history_ref': ref})
            return body, ref
