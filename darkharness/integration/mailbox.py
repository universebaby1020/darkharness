"""Owner-transaction extension of the Core store. No separate canonical writer.

Raw UTF-8 artifacts are blobs in the same SQLite transaction as receipts.
The supplied owner must implement transaction(epoch) and expose epoch; production
uses darkharness.core.Store. A test owner may implement that seam without Linux.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone


class IntegrationError(RuntimeError):
    """A stable, safe public reason code (never includes the input)."""


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: bytes):
    return hashlib.sha256(value).hexdigest()


LIFE = ("RECEIVED", "ASSEMBLED", "READY", "STARTED", "RETURNED", "RECONCILED")
TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED", "CLOSED_UNRESOLVED"}


@dataclass(frozen=True)
class HandoffPart:
    handoff_id: str
    index: int  # zero-based
    count: int
    length: int  # UTF-8 bytes, not characters
    sha256: str
    whole_sha256: str


class Mailbox:
    def __init__(self, owner):
        self.owner = owner
        # execute individually: executescript would implicitly commit the fence.
        schema = [
            "CREATE TABLE IF NOT EXISTS c_artifact(hash TEXT PRIMARY KEY, body BLOB NOT NULL)",
            "CREATE TABLE IF NOT EXISTS c_inbox(seq INTEGER PRIMARY KEY AUTOINCREMENT, seat TEXT NOT NULL, room TEXT NOT NULL, sender TEXT NOT NULL, platform_id TEXT NOT NULL, hash TEXT NOT NULL, life TEXT NOT NULL, work TEXT, handoff TEXT, received TEXT NOT NULL, UNIQUE(seat,room,sender,platform_id))",
            "CREATE TABLE IF NOT EXISTS c_handoff(id TEXT PRIMARY KEY, seat TEXT, room TEXT, sender TEXT, count INTEGER, whole_hash TEXT)",
            "CREATE TABLE IF NOT EXISTS c_part(handoff TEXT, idx INTEGER, hash TEXT, length INTEGER, inbox INTEGER, PRIMARY KEY(handoff,idx))",
            "CREATE TABLE IF NOT EXISTS c_work(id TEXT PRIMARY KEY, seat TEXT, room TEXT, input TEXT, attempt TEXT, state TEXT, delivery TEXT, thread TEXT, result TEXT)",
            "CREATE TABLE IF NOT EXISTS c_outbox(id TEXT PRIMARY KEY, operation TEXT, hash TEXT, state TEXT, receipt TEXT)",
            "CREATE TABLE IF NOT EXISTS c_question(id TEXT PRIMARY KEY, operation TEXT, peer TEXT, context TEXT, answer TEXT, continuation TEXT)",
            "CREATE TABLE IF NOT EXISTS c_control(id TEXT PRIMARY KEY, operation TEXT, attempt TEXT, kind TEXT, body TEXT, state TEXT)",
            "CREATE TABLE IF NOT EXISTS c_control_receipt(seat TEXT,room TEXT,sender TEXT,platform_id TEXT,hash TEXT,PRIMARY KEY(seat,room,sender,platform_id))",
            "CREATE TABLE IF NOT EXISTS c_callback(operation TEXT,attempt TEXT,id TEXT,hash TEXT,PRIMARY KEY(operation,attempt,id))",
            "CREATE TABLE IF NOT EXISTS c_thread_tools(thread TEXT PRIMARY KEY, fingerprint TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS c_retry_wait(operation TEXT PRIMARY KEY, failures INTEGER NOT NULL, not_before REAL NOT NULL)",
            "CREATE TABLE IF NOT EXISTS c_event(seq INTEGER PRIMARY KEY AUTOINCREMENT, operation TEXT, kind TEXT, body TEXT, at TEXT)",
        ]
        with owner.transaction(owner.epoch) as db:
            for sql in schema:
                db.execute(sql)

    @staticmethod
    def event(db, operation, kind, body):
        db.execute("INSERT INTO c_event(operation,kind,body,at) VALUES(?,?,?,?)",
                   (operation, kind, encode(body), datetime.now(timezone.utc).isoformat()))

    @staticmethod
    def artifact(db, raw):
        h = digest(raw)
        db.execute("INSERT OR IGNORE INTO c_artifact VALUES(?,?)", (h, raw))
        return h

    def receive(self, seat, room, sender, platform_id, content, *, envelope=None, part=None):
        if not all(isinstance(x, str) and x for x in (seat, room, sender, platform_id)):
            raise IntegrationError("MESSAGE_ID_REQUIRED")
        raw = content.encode("utf-8")
        h = digest(raw)
        with self.owner.transaction(self.owner.epoch) as db:
            row = db.execute("SELECT * FROM c_inbox WHERE seat=? AND room=? AND sender=? AND platform_id=?",
                             (seat, room, sender, platform_id)).fetchone()
            if row:
                if row["hash"] != h:
                    raise IntegrationError("MESSAGE_ID_CONFLICT")
                # Conflicting multipart metadata must not pass as an idempotent receipt.
                if part:
                    hid = digest(encode([seat, room, sender, part.handoff_id]).encode())
                    old = db.execute("SELECT * FROM c_part WHERE handoff=? AND idx=?", (hid, part.index)).fetchone()
                    header = db.execute("SELECT * FROM c_handoff WHERE id=?", (hid,)).fetchone()
                    if not old or old["hash"] != h or old["length"] != part.length or not header or header["count"] != part.count or header["whole_hash"] != part.whole_sha256:
                        raise IntegrationError("HANDOFF_CONFLICT")
                return dict(row)
            self.artifact(db, raw)
            hid = digest(encode([seat, room, sender, part.handoff_id]).encode()) if part else None
            cursor = db.execute("INSERT INTO c_inbox(seat,room,sender,platform_id,hash,life,handoff,received) VALUES(?,?,?,?,?,'RECEIVED',?,?)",
                                (seat, room, sender, platform_id, h, hid, datetime.now(timezone.utc).isoformat()))
            seq = cursor.lastrowid
            self.event(db, None, "RECEIVE_ACK", {"inbox": seq, "hash": h})
            if part:
                if not isinstance(part.count, int) or not isinstance(part.index, int) or part.count < 1 or not 0 <= part.index < part.count or len(raw) != part.length or h != part.sha256:
                    raise IntegrationError("PART_INTEGRITY")
                hid = digest(encode([seat, room, sender, part.handoff_id]).encode())
                header = db.execute("SELECT * FROM c_handoff WHERE id=?", (hid,)).fetchone()
                if header and (header["count"] != part.count or header["whole_hash"] != part.whole_sha256):
                    raise IntegrationError("HANDOFF_CONFLICT")
                db.execute("INSERT OR IGNORE INTO c_handoff VALUES(?,?,?,?,?,?)", (hid, seat, room, sender, part.count, part.whole_sha256))
                old = db.execute("SELECT * FROM c_part WHERE handoff=? AND idx=?", (hid, part.index)).fetchone()
                if old and (old["hash"] != h or old["length"] != part.length):
                    raise IntegrationError("PART_CONFLICT")
                db.execute("INSERT OR IGNORE INTO c_part VALUES(?,?,?,?,?)", (hid, part.index, h, part.length, seq))
                parts = db.execute("SELECT * FROM c_part WHERE handoff=? ORDER BY idx", (hid,)).fetchall()
                if len(parts) != part.count:
                    self.event(db, hid, "PARTS_MISSING", {"indices": [i for i in range(part.count) if i not in {r["idx"] for r in parts}]})
                    return dict(db.execute("SELECT * FROM c_inbox WHERE seq=?", (seq,)).fetchone())
                full = b"".join(db.execute("SELECT body FROM c_artifact WHERE hash=?", (r["hash"],)).fetchone()[0] for r in parts)
                if digest(full) != part.whole_sha256:
                    raise IntegrationError("HANDOFF_INTEGRITY")
                self.artifact(db, full)
                work = hid
                content = full.decode("utf-8")
                # All redundant part receipts attach to the one verified work item.
                for r in db.execute("SELECT seq FROM c_inbox WHERE handoff=? AND work IS NULL", (hid,)).fetchall():
                    db.execute("UPDATE c_inbox SET work=?,life='READY' WHERE seq=?", (work, r[0]))
            else:
                work = digest(encode([seat, room, sender, platform_id]).encode())
            body = dict(envelope or {})
            body["content"] = content
            if part:
                body["handoff_id"] = part.handoff_id
            old_work = db.execute("SELECT id FROM c_work WHERE id=?", (work,)).fetchone()
            if not old_work:
                db.execute("INSERT INTO c_work VALUES(?,?,?,?,NULL,'QUEUED','READY',NULL,NULL)", (work, seat, room, encode(body)))
            db.execute("UPDATE c_inbox SET work=?,life='READY' WHERE seq=?", (work, seq))
            self.event(db, work, "ASSEMBLY_ACK", {"whole_hash": digest(content.encode()), "complete": True})
            self.event(db, work, "READY", {"seat": seat})
            return dict(db.execute("SELECT * FROM c_inbox WHERE seq=?", (seq,)).fetchone())

    def read_work(self, operation):
        with self.owner.transaction(self.owner.epoch) as db:
            row = db.execute("SELECT * FROM c_work WHERE id=?", (operation,)).fetchone()
            if not row:
                raise IntegrationError("WORK_NOT_FOUND")
            return dict(row)

    def next_ready(self, seat):
        with self.owner.transaction(self.owner.epoch) as db:
            # Questions paused on peers do not occupy execution. Unknown effects do.
            if db.execute("SELECT 1 FROM c_outbox o JOIN c_work w ON w.id=o.operation WHERE w.seat=? AND o.state='DELIVERY_UNKNOWN'", (seat,)).fetchone():
                return None
            if db.execute("SELECT 1 FROM c_work WHERE seat=? AND delivery IN ('DISPATCHING','STARTED','DELIVERY_UNKNOWN')", (seat,)).fetchone():
                return None
            row = db.execute("SELECT w.* FROM c_work w LEFT JOIN c_inbox i ON i.work=w.id WHERE w.seat=? AND w.delivery='READY' AND w.state='QUEUED' AND NOT EXISTS (SELECT 1 FROM c_retry_wait r WHERE r.operation=w.id AND r.not_before > CAST(strftime('%s','now') AS REAL)) GROUP BY w.id ORDER BY MIN(i.seq),w.id LIMIT 1", (seat,)).fetchone()
            return dict(row) if row else None

    def claim(self, operation, attempt):
        with self.owner.transaction(self.owner.epoch) as db:
            row = db.execute("SELECT * FROM c_work WHERE id=?", (operation,)).fetchone()
            if not row or row["state"] != "QUEUED" or row["delivery"] != "READY" or not attempt:
                raise IntegrationError("DUPLICATE_EXECUTION_FENCE")
            db.execute("UPDATE c_work SET attempt=?,delivery='DISPATCHING',state='RUNNING' WHERE id=?", (attempt, operation))
            self.event(db, operation, "DISPATCH_INTENT", {"attempt": attempt})

    def observe(self, operation, attempt, kind, body):
        with self.owner.transaction(self.owner.epoch) as db:
            self.event(db, operation, kind, {"attempt": attempt, "data": body})

    def update(self, operation, attempt, *, state=None, delivery=None, thread=None, result=None):
        with self.owner.transaction(self.owner.epoch) as db:
            row = db.execute("SELECT * FROM c_work WHERE id=?", (operation,)).fetchone()
            if not row or row["attempt"] != attempt:
                raise IntegrationError("OWNER_ATTEMPT_FENCE")
            if state is not None and state not in TERMINAL | {"QUEUED", "RUNNING", "PAUSED"}:
                raise IntegrationError("INVALID_STATE")
            self.event(db, operation, "WORK_OBSERVATION", {"attempt": attempt, "state": state, "delivery": delivery, "thread": thread, "result": result})
            if row["state"] in TERMINAL:
                # Message/result acknowledgement is independent of terminal work
                # status; only forward receipt refinement is permitted.
                if delivery == "RECONCILED" and row["delivery"] == "RETURNED" and result is not None:
                    db.execute("UPDATE c_work SET delivery='RECONCILED' WHERE id=?", (operation,))
                    db.execute("UPDATE c_inbox SET life='RECONCILED' WHERE work=?", (operation,))
                    self.event(db, operation, "RECONCILED_ACK", {"attempt": attempt, "receipt": result})
                return dict(db.execute("SELECT * FROM c_work WHERE id=?", (operation,)).fetchone())
            state = state or row["state"]
            delivery = delivery or row["delivery"]
            db.execute("UPDATE c_work SET state=?,delivery=?,thread=COALESCE(?,thread),result=COALESCE(?,result) WHERE id=?", (state, delivery, thread, encode(result) if result is not None else None, operation))
            if delivery in LIFE:
                db.execute("UPDATE c_inbox SET life=? WHERE work=?", (delivery, operation))
                self.event(db, operation, delivery + "_ACK", {"attempt": attempt})
            return dict(db.execute("SELECT * FROM c_work WHERE id=?", (operation,)).fetchone())

    def release_unstarted(self, operation, attempt, code):
        from .unstarted import proven_unstarted
        import time
        with self.owner.transaction(self.owner.epoch) as db:
            work = db.execute('SELECT * FROM c_work WHERE id=?', (operation,)).fetchone()
            if not work or not proven_unstarted(db, work, attempt):
                return None
            prior = db.execute('SELECT failures FROM c_retry_wait WHERE operation=?', (operation,)).fetchone()
            n = (prior[0] if prior else 0) + 1
            terminal = n >= 3
            due = time.time() + 60
            db.execute('INSERT OR REPLACE INTO c_retry_wait VALUES(?,?,?)', (operation, n, due))
            state, delivery = ('FAILED', 'RETURNED') if terminal else ('QUEUED', 'READY')
            result = {'effect': 'NOT_STARTED', 'code': code, 'failures': n}
            db.execute('UPDATE c_work SET state=?,delivery=?,result=? WHERE id=?', (state, delivery, encode(result), operation))
            db.execute('UPDATE c_inbox SET life=? WHERE work=?', (delivery, operation))
            self.event(db, operation, 'NOT_STARTED_FAILED' if terminal else 'NOT_STARTED_RELEASED', {'attempt': attempt, **result, 'not_before': due})
            return {'terminal': terminal, 'backoff_s': 60, **result}

    def notify_peer_blocker(self, operation, attempt, seat, room, run_id):
        """Evidence-only internal work; never a platform message or retry grant."""
        with self.owner.transaction(self.owner.epoch) as db:
            work = db.execute('SELECT * FROM c_work WHERE id=? AND attempt=?', (operation, attempt)).fetchone()
            if not work or work['seat'] == seat:
                return None
            child = digest(encode([run_id, operation, attempt, 'peer-blocker']).encode())
            evidence = {'operation': operation, 'attempt': attempt, 'seat': work['seat'], 'state': work['state'], 'delivery': work['delivery'], 'result_ref': self.artifact(db, (work['result'] or '{}').encode()), 'replay': 'FENCED'}
            body = {'content': encode({'peer_blocker': evidence}), 'internal_evidence': True, 'run_id': run_id}
            db.execute("INSERT OR IGNORE INTO c_work VALUES(?,?,?,?,NULL,'QUEUED','READY',NULL,NULL)", (child, seat, room, encode(body)))
            self.event(db, operation, 'COORDINATOR_BLOCKER_QUEUED', {'attempt': attempt, 'child': child, 'result_ref': evidence['result_ref']})
            return child

    def recover(self):
        """Observe restart, never replay an in-flight effect."""
        with self.owner.transaction(self.owner.epoch) as db:
            rows = db.execute("SELECT id,attempt FROM c_work WHERE delivery IN ('DISPATCHING','STARTED')").fetchall()
            from .unstarted import proven_unstarted
            for row in rows:
                work = db.execute('SELECT * FROM c_work WHERE id=?', (row[0],)).fetchone()
                if proven_unstarted(db, work, row[1]):
                    # Cessation must already be durable; owner restart itself is
                    # NOT evidence that an app-server group stopped.
                    prior = db.execute('SELECT failures FROM c_retry_wait WHERE operation=?', (row[0],)).fetchone()
                    n = (prior[0] if prior else 0) + 1
                    due = datetime.now(timezone.utc).timestamp() + 60
                    db.execute('INSERT OR REPLACE INTO c_retry_wait VALUES(?,?,?)', (row[0], n, due))
                    state, delivery = ('FAILED', 'RETURNED') if n >= 3 else ('QUEUED', 'READY')
                    db.execute('UPDATE c_work SET state=?,delivery=?,result=? WHERE id=?', (state, delivery, encode({'effect': 'NOT_STARTED', 'code': 'OWNER_RESTART', 'failures': n}), row[0]))
                    db.execute('UPDATE c_inbox SET life=? WHERE work=?', (delivery, row[0]))
                    self.event(db, row[0], 'NOT_STARTED_RECOVERED', {'attempt': row[1], 'failures': n, 'not_before': due})
                    continue
                db.execute("UPDATE c_work SET delivery='DELIVERY_UNKNOWN',state='PAUSED' WHERE id=?", (row[0],))
                self.event(db, row[0], "DELIVERY_UNKNOWN", {"attempt": row[1], "cause": "owner_restart"})
            return [r[0] for r in rows]

    @staticmethod
    def _send_readback(row, operation, h):
        if row['hash'] != h or row['operation'] != operation:
            raise IntegrationError('OUTBOX_ID_CONFLICT')
        if row['state'] == 'REJECTED':
            raise IntegrationError('LOCAL_SEND_VALIDATION_REJECTED')
        if row['state'] != 'ACKED':
            raise IntegrationError('DELIVERY_UNKNOWN_FENCE')
        return json.loads(row['receipt'])

    def send_readback(self, identifier, operation, body):
        with self.owner.transaction(self.owner.epoch) as db:
            row = db.execute('SELECT * FROM c_outbox WHERE id=?', (identifier,)).fetchone()
            return self._send_readback(row, operation, digest(encode(body).encode())) if row else None

    def reject_send(self, identifier, operation, attempt, body, code):
        # Only called by the synchronous local validation phase, never an HTTP catch.
        raw = encode(body).encode()
        with self.owner.transaction(self.owner.epoch) as db:
            row = db.execute('SELECT * FROM c_outbox WHERE id=?', (identifier,)).fetchone()
            if row:
                return self._send_readback(row, operation, digest(raw))
            h = self.artifact(db, raw)
            receipt = {'effect': 'NOT_SENT', 'code': code, 'phase': 'SDK4_LOCAL_PREFLIGHT', 'attempt': attempt}
            db.execute("INSERT INTO c_outbox VALUES(?,?,?,'REJECTED',?)", (identifier, operation, h, encode(receipt)))
            self.event(db, operation, 'SEND_REJECTED', {'id': identifier, 'hash': h, **receipt})

    def prepare_send(self, identifier, operation, body):
        raw = encode(body).encode()
        with self.owner.transaction(self.owner.epoch) as db:
            h = digest(raw)
            row = db.execute("SELECT * FROM c_outbox WHERE id=?", (identifier,)).fetchone()
            if row:
                return self._send_readback(row, operation, h)
            self.artifact(db, raw)
            # Before crossing the external boundary, uncertainty is durable.
            db.execute("INSERT INTO c_outbox VALUES(?,?,?,'DELIVERY_UNKNOWN',NULL)", (identifier, operation, h))
            self.event(db, operation, "SEND_INTENT", {"id": identifier, "hash": h})
            return None

    def send_not_sent(self, identifier, attempt, code):
        with self.owner.transaction(self.owner.epoch) as db:
            row = db.execute('SELECT * FROM c_outbox WHERE id=?', (identifier,)).fetchone()
            if not row or row['state'] != 'DELIVERY_UNKNOWN':
                raise IntegrationError('OUTBOX_REJECTION_FENCE')
            receipt = {'effect': 'NOT_SENT', 'attempt': attempt, 'code': code, 'phase': 'DEFINITIVE_TRANSPORT_REJECTION'}
            db.execute("UPDATE c_outbox SET state='REJECTED',receipt=? WHERE id=?", (encode(receipt), identifier))
            self.event(db, row['operation'], 'SEND_REJECTED', {'id': identifier, 'hash': row['hash'], **receipt})

    def sent(self, identifier, receipt):
        with self.owner.transaction(self.owner.epoch) as db:
            row = db.execute("SELECT operation,state FROM c_outbox WHERE id=?", (identifier,)).fetchone()
            if not row:
                raise IntegrationError("OUTBOX_NOT_FOUND")
            if row['state'] == 'REJECTED':
                raise IntegrationError('LOCAL_SEND_VALIDATION_REJECTED')
            db.execute("UPDATE c_outbox SET state='ACKED',receipt=? WHERE id=?", (encode(receipt), identifier))
            self.event(db, row[0], "SEND_ACK", {"id": identifier, "receipt": receipt})

    def question(self, identifier, operation, peer, context):
        with self.owner.transaction(self.owner.epoch) as db:
            row = db.execute("SELECT * FROM c_question WHERE id=?", (identifier,)).fetchone()
            if row:
                if row["operation"] != operation or row["peer"] != peer or row["context"] != encode(context):
                    raise IntegrationError("QUESTION_ID_CONFLICT")
                return
            db.execute("INSERT INTO c_question VALUES(?,?,?,?,NULL,NULL)", (identifier, operation, peer, encode(context)))
            self.event(db, operation, "PEER_QUESTION", {"id": identifier, "peer": peer, "context": context})

    def answer(self, identifier, sender, answer):
        """Authenticated platform sender binding, not a role in the content."""
        with self.owner.transaction(self.owner.epoch) as db:
            q = db.execute("SELECT * FROM c_question WHERE id=?", (identifier,)).fetchone()
            if not q or q["peer"] != sender:
                raise IntegrationError("PEER_BINDING_DENIED")
            if q["answer"] is not None:
                if q["answer"] != answer:
                    raise IntegrationError("ANSWER_CONFLICT")
                return q["continuation"]
            parent = db.execute("SELECT * FROM c_work WHERE id=?", (q["operation"],)).fetchone()
            if parent["delivery"] != "YIELDED" or parent["state"] != "PAUSED":
                raise IntegrationError("QUESTION_NOT_YIELDED")
            cid = digest(encode([identifier, "continuation"]).encode())
            body = json.loads(parent["input"])
            body["content"] = encode({"original_task": body["content"], "question": json.loads(q["context"]), "peer_answer": answer})
            body["parent_operation"] = parent["id"]
            db.execute("INSERT INTO c_work VALUES(?,?,?,?,NULL,'QUEUED','READY',?,NULL)", (cid, parent["seat"], parent["room"], encode(body), parent["thread"]))
            db.execute("UPDATE c_question SET answer=?,continuation=? WHERE id=?", (answer, cid, identifier))
            self.event(db, parent["id"], "PEER_ANSWER", {"id": identifier, "continuation": cid})
            return cid

    def receive_control(self, seat, room, sender, platform_id, content):
        raw = content.encode("utf-8")
        with self.owner.transaction(self.owner.epoch) as db:
            row = db.execute("SELECT hash FROM c_control_receipt WHERE seat=? AND room=? AND sender=? AND platform_id=?", (seat, room, sender, platform_id)).fetchone()
            if row:
                if row[0] != digest(raw):
                    raise IntegrationError("MESSAGE_ID_CONFLICT")
                return False
            h = self.artifact(db, raw)
            db.execute("INSERT INTO c_control_receipt VALUES(?,?,?,?,?)", (seat, room, sender, platform_id, h))
            self.event(db, None, "CONTROL_RECEIVE_ACK", {"seat": seat, "room": room, "sender": sender, "platform_id": platform_id, "hash": h})
            return True

    def callback(self, operation, attempt, identifier, body):
        with self.owner.transaction(self.owner.epoch) as db:
            h = digest(encode(body).encode())
            row = db.execute("SELECT hash FROM c_callback WHERE operation=? AND attempt=? AND id=?", (operation, attempt, identifier)).fetchone()
            if row:
                if row[0] != h:
                    raise IntegrationError("CALLBACK_ID_CONFLICT")
                return False
            db.execute("INSERT INTO c_callback VALUES(?,?,?,?)", (operation, attempt, identifier, h))
            self.event(db, operation, "CALLBACK_INTENT", {"attempt": attempt, "id": identifier, "hash": h})
            return True

    def control(self, identifier, operation, attempt, kind, body):
        if kind not in {"cancel", "permission_reply", "peer_answer"}:
            raise IntegrationError("CONTROL_KIND_DENIED")
        with self.owner.transaction(self.owner.epoch) as db:
            row = db.execute("SELECT * FROM c_work WHERE id=?", (operation,)).fetchone()
            if not row or row["attempt"] != attempt or row["state"] in TERMINAL:
                raise IntegrationError("STALE_CONTROL")
            old = db.execute("SELECT * FROM c_control WHERE id=?", (identifier,)).fetchone()
            if old and (old["operation"] != operation or old["attempt"] != attempt or old["kind"] != kind or old["body"] != encode(body)):
                raise IntegrationError("CONTROL_ID_CONFLICT")
            db.execute("INSERT OR IGNORE INTO c_control VALUES(?,?,?,?,?,'PENDING')", (identifier, operation, attempt, kind, encode(body)))
            self.event(db, operation, "CONTROL_RECEIVED", {"id": identifier, "kind": kind, "attempt": attempt})

    def drain_controls(self, operation, attempt):
        with self.owner.transaction(self.owner.epoch) as db:
            rows = db.execute("SELECT * FROM c_control WHERE operation=? AND attempt=? AND state='PENDING'", (operation, attempt)).fetchall()
            db.execute("UPDATE c_control SET state='DRAINED' WHERE operation=? AND attempt=? AND state='PENDING'", (operation, attempt))
            return [dict(r) for r in rows]
