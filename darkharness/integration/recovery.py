"""Controller-only recovery proof/readback, not seat-supplied 'safe' assertions.

Ordinary new attempt retains the original full input and thread/parent lineage.
No outbox, native UNKNOWN delivery or ambiguous Git intent is refined here.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat

from .mailbox import IntegrationError, Mailbox, digest, encode
from .git_broker import regular_path


class Recovery:
    def __init__(self, mailbox, router, broker, idle_client_pids=None):
        self.box, self.owner, self.router, self.broker = mailbox, mailbox.owner, router, broker
        # Internal registry callback, NEVER a payload boolean or PID allowlist.
        self.idle_client_pids = idle_client_pids or (lambda: set())
        with self.owner.transaction(self.owner.epoch) as db:
            db.execute('CREATE TABLE IF NOT EXISTS c_recovery(id TEXT PRIMARY KEY,operation TEXT,attempt TEXT,proof TEXT,continuation TEXT,UNIQUE(operation,attempt))')

    def _parent(self, db, operation, attempt):
        scope = self.router._scope(db)
        cap = scope.get('continuation') if scope else None
        if not isinstance(cap, dict) or operation not in cap.get('operations', []):
            raise IntegrationError('CONTINUATION_GRANT_REQUIRED')
        parent = db.execute('SELECT * FROM c_work WHERE id=?', (operation,)).fetchone()
        if not parent or parent['attempt'] != attempt or parent['seat'] != self.router.seat or parent['room'] != self.router.room:
            raise IntegrationError('OWNER_ATTEMPT_FENCE')
        if parent['delivery'] not in {'RETURNED', 'YIELDED', 'RECONCILED'} or parent['state'] not in {'SUCCEEDED', 'FAILED', 'CANCELLED', 'PAUSED'} or not parent['thread']:
            raise IntegrationError('CONTINUATION_DELIVERY_UNKNOWN')
        if db.execute("SELECT 1 FROM c_outbox o JOIN c_work w ON w.id=o.operation WHERE w.seat=? AND o.state!='ACKED'", (parent['seat'],)).fetchone() or db.execute("SELECT 1 FROM c_work WHERE seat=? AND delivery IN ('DISPATCHING','STARTED','DELIVERY_UNKNOWN')", (parent['seat'],)).fetchone():
            raise IntegrationError('CONTINUATION_OUTSTANDING_UNKNOWN')
        if db.execute("SELECT 1 FROM c_git_effect WHERE state!='ACKED'").fetchone():
            raise IntegrationError('CONTINUATION_GIT_UNKNOWN')
        if db.execute("SELECT 1 FROM c_question q JOIN c_work w ON w.id=q.continuation WHERE q.operation=? AND w.delivery IN ('READY','DISPATCHING','STARTED','DELIVERY_UNKNOWN')", (operation,)).fetchone():
            raise IntegrationError('CONTINUATION_ALREADY_PENDING')
        return dict(parent)

    def _quiescent(self):
        root = self.broker.root
        allowed = self.idle_client_pids() | {os.getpid()}
        # Validate owned client identities first; don't turn unrelated WSL
        # nondumpable processes into a global /proc approval prerequisite.
        with self.owner.transaction(self.owner.epoch) as db:
            starts = [json.loads(r[0]).get('data', {}) for r in db.execute("SELECT body FROM c_event WHERE kind='PROCESS_STARTED'")]
        for data in starts:
            data = data.get('payload', data)
            identity = data.get('identity')
            if data.get('cwd') != str(root) or not identity or identity[0] in allowed:
                continue
            p = Path('/proc') / str(identity[0])
            try:
                fields = (p / 'stat').read_text().rsplit(')', 1)[1].split()
                boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
                if [identity[0], boot, fields[19]] == identity and fields[0] != 'Z':
                    raise IntegrationError('CONTINUATION_PROCESS_OUTSTANDING')
            except FileNotFoundError:
                continue
            except PermissionError:
                raise IntegrationError('CONTINUATION_OWNED_PROCESS_UNKNOWN') from None
        for p in Path('/proc').iterdir():
            if not p.name.isdigit() or int(p.name) in allowed:
                continue
            try:
                if p.stat().st_uid != os.getuid():
                    continue
                state = (p / 'stat').read_text().rsplit(')', 1)[1].split()[0]
                if state == 'Z':
                    continue
                cwd = Path(os.readlink(p / 'cwd'))
                if cwd == root or cwd.is_relative_to(root):
                    raise IntegrationError('CONTINUATION_PROCESS_OUTSTANDING')
            except FileNotFoundError:
                continue  # exited before observation
            except PermissionError:
                continue  # unrelated inaccessible processes; owned identities checked above

    def fingerprint(self):
        identity = self.broker._identity(str(self.broker.root))
        ref, head = self.broker._head()
        self.broker._tree(head)
        with self.owner.transaction(self.owner.epoch) as db:
            scope = self.router._scope(db)
        review = (scope or {}).get('review_snapshot', {})
        scratch = Path(review['scratch']) if isinstance(review, dict) and review.get('scratch') else None
        files = []
        for folder, dirs, names in os.walk(self.broker.root, followlinks=False):
            folder = Path(folder)
            dirs[:] = sorted(d for d in dirs if d != '.git' and (scratch is None or folder / d != scratch))
            for d in dirs:
                if (folder / d).is_symlink() or (folder / d / '.git').exists():
                    raise IntegrationError('CONTINUATION_SOURCE_UNSAFE')
            for name in sorted(names):
                p = folder / name
                rel = p.relative_to(self.broker.root).as_posix()
                regular_path(self.broker.root, rel)
                before = p.stat()
                raw = p.read_bytes()
                after = p.stat()
                if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                    raise IntegrationError('CONTINUATION_SOURCE_CHANGED')
                files.append([rel, digest(raw), len(raw), stat.S_IMODE(after.st_mode)])
        body = {'identity': identity, 'ref': ref, 'head': head, 'index': digest(self.broker._index_bytes()), 'files': files}
        return {'sha256': digest(encode(body).encode()), **body}

    def observe(self, operation, attempt):
        with self.owner.transaction(self.owner.epoch) as db:
            parent = self._parent(db, operation, attempt)
            commits = [json.loads(r[0]) for r in db.execute("SELECT receipt FROM c_git_effect WHERE operation=? AND state='ACKED'", (operation,)) if r[0] and json.loads(r[0]).get('state') == 'COMMITTED']
        self._quiescent()
        first, second = self.fingerprint(), self.fingerprint()
        if first != second:
            raise IntegrationError('CONTINUATION_SOURCE_CHANGED')
        for receipt in commits:
            # Exact receipt/readback, including selected-index reconciliation.
            if receipt['commit'] != second['head'] or receipt['git_identity'] != second['identity'] or receipt['index_after'] != second['index']:
                raise IntegrationError('CONTINUATION_COMMIT_UNKNOWN')
        proof = {'schema': 'dh-maintenance-proof-v1', 'operation': operation, 'attempt': attempt,
                 'run_id': self.router.run_id, 'grant_id': self.router.grant_id, 'seat': parent['seat'],
                 'room': parent['room'], 'thread': parent['thread'], 'input_sha256': digest(parent['input'].encode()),
                 'delivery': parent['delivery'], 'state': parent['state'], 'source': second,
                 'effect': 'KNOWN_COMMITTED_EFFECT' if commits else 'VERIFIED_NO_OUTSTANDING_EFFECT',
                 'committed_receipts': commits, 'verification': 'settled delivery + ACKed effects + owned/absent processes + two identical canonical source readbacks'}
        raw = encode(proof).encode()
        identifier = digest(raw)
        with self.owner.transaction(self.owner.epoch) as db:
            self._parent(db, operation, attempt)
            old = db.execute('SELECT * FROM c_recovery WHERE operation=? AND attempt=?', (operation, attempt)).fetchone()
            if old and old['id'] != identifier:
                raise IntegrationError('CONTINUATION_SOURCE_CHANGED')
            Mailbox.artifact(db, raw)
            db.execute('INSERT OR IGNORE INTO c_recovery VALUES(?,?,?,?,NULL)', (identifier, operation, attempt, raw.decode()))
            Mailbox.event(db, operation, 'CONTINUATION_EVIDENCE', {'attempt': attempt, 'evidence_id': identifier, 'effect': proof['effect'], 'source_sha256': second['sha256']})
        return {'evidence_id': identifier, 'effect': proof['effect'], 'source_sha256': second['sha256'], 'head': second['head']}

    def resume(self, operation, attempt, evidence_id):
        if not isinstance(evidence_id, str):
            raise IntegrationError('CONTINUATION_EVIDENCE_REQUIRED')
        with self.owner.transaction(self.owner.epoch) as db:
            parent = self._parent(db, operation, attempt)
            row = db.execute('SELECT * FROM c_recovery WHERE id=? AND operation=? AND attempt=?', (evidence_id, operation, attempt)).fetchone()
            artifact = db.execute('SELECT body FROM c_artifact WHERE hash=?', (evidence_id,)).fetchone()
            if not row or not artifact or digest(artifact[0]) != evidence_id or artifact[0].decode() != row['proof']:
                raise IntegrationError('CONTINUATION_EVIDENCE_REQUIRED')
            proof = json.loads(row['proof'])
            if proof['grant_id'] != self.router.grant_id or proof['run_id'] != self.router.run_id or proof['input_sha256'] != digest(parent['input'].encode()) or proof['thread'] != parent['thread']:
                raise IntegrationError('CONTINUATION_LINEAGE_CONFLICT')
            if row['continuation']:
                return {'id': row['continuation'], 'parent_operation': operation, 'evidence_id': evidence_id}
        self._quiescent()
        if self.fingerprint() != proof['source']:
            raise IntegrationError('CONTINUATION_SOURCE_CHANGED')
        cid = digest(encode([operation, attempt, 'maintenance-continuation']).encode())
        with self.owner.transaction(self.owner.epoch) as db:
            parent = self._parent(db, operation, attempt)
            body = json.loads(parent['input'])  # no manually authored replacement task
            body.update(parent_operation=operation, parent_attempt=attempt, recovery_evidence=evidence_id,
                        recovery_receipt={'evidence_id': evidence_id, 'effect': proof['effect'], 'head': proof['source']['head'], 'source_sha256': proof['source']['sha256'], 'committed_receipts': proof['committed_receipts']})
            answers = [{'id': q['id'], 'context': json.loads(q['context']), 'answer': q['answer']} for q in db.execute('SELECT * FROM c_question WHERE operation=? AND answer IS NOT NULL ORDER BY id', (operation,))]
            if answers:
                body['peer_answers'] = answers
            # One child per settled parent attempt, no synthetic c_inbox/human event.
            db.execute("INSERT OR IGNORE INTO c_work VALUES(?,?,?,?,NULL,'QUEUED','READY',?,NULL)", (cid, parent['seat'], parent['room'], encode(body), parent['thread']))
            db.execute('UPDATE c_recovery SET continuation=? WHERE id=?', (cid, evidence_id))
            Mailbox.event(db, operation, 'CONTINUATION_AUTHORIZED', {'attempt': attempt, 'continuation': cid, 'evidence_id': evidence_id, 'input_sha256': proof['input_sha256'], 'thread': parent['thread']})
        return {'id': cid, 'parent_operation': operation, 'evidence_id': evidence_id}
