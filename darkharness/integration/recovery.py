"""Controller-only recovery proof/readback, not seat-supplied 'safe' assertions.

Ordinary new attempt retains the original full input and thread/parent lineage.
Only the evidence-linked SDK4 local mention rejection can refine an outbox.
Native UNKNOWN delivery and ambiguous Git intent remain fenced.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat

from .mailbox import IntegrationError, Mailbox, digest, encode
from .git_broker import regular_path
from .index_evidence import indexed_entries, tree_entries, same_source


class Recovery:
    def __init__(self, mailbox, router, broker, idle_client_pids=None):
        self.box, self.owner, self.router, self.broker = mailbox, mailbox.owner, router, broker
        # Internal registry callback, NEVER a payload boolean or PID allowlist.
        self.idle_client_pids = idle_client_pids or (lambda: set())
        with self.owner.transaction(self.owner.epoch) as db:
            db.execute('CREATE TABLE IF NOT EXISTS c_recovery(id TEXT PRIMARY KEY,operation TEXT,attempt TEXT,proof TEXT,continuation TEXT,UNIQUE(operation,attempt))')

    def reject_local_send(self, operation, attempt, outbox_id, body_sha256, evidence_refs):
        """Refine only the pinned SDK4 empty-cache unknown-mention rejection.

        Controller supplies canonical event references, never a safe/success claim.
        All validation and the single-row transition share the owner transaction.
        Does not resume a parent, manufacture inbox work, or replay the rejected ID.
        """
        from importlib.metadata import version
        from band.runtime.tools import agent as sdk_agent
        pinned = '44364d1704f66b4ad38ad83fa8cfb97fd8666053e00d4d893a39adf8558d7180'
        if version('band-sdk') != '4.0.0' or digest(Path(sdk_agent.__file__).read_bytes()) != pinned:
            raise IntegrationError('LOCAL_REJECTION_SDK_PIN_MISMATCH')
        names = {'intent', 'request', 'callback', 'unknown', 'result', 'completed'}
        if not all(isinstance(v, str) and v for v in (operation, attempt, outbox_id, body_sha256)) or not isinstance(evidence_refs, dict) or set(evidence_refs) != names:
            raise IntegrationError('LOCAL_REJECTION_EVIDENCE_REQUIRED')
        with self.owner.transaction(self.owner.epoch) as db:
            scope = self.router._scope(db)
            cap = (scope or {}).get('continuation')
            if not scope or not isinstance(cap, dict) or operation not in cap.get('operations', []):
                raise IntegrationError('CONTINUATION_GRANT_REQUIRED')
            parent = db.execute('SELECT * FROM c_work WHERE id=?', (operation,)).fetchone()
            row = db.execute('SELECT * FROM c_outbox WHERE id=?', (outbox_id,)).fetchone()
            if not parent or parent['attempt'] != attempt or parent['seat'] != self.router.seat or parent['room'] != self.router.room or not row or row['operation'] != operation:
                raise IntegrationError('OWNER_ATTEMPT_FENCE')
            if parent['delivery'] not in {'RETURNED', 'YIELDED', 'RECONCILED'} or db.execute("SELECT 1 FROM c_work WHERE seat=? AND delivery IN ('STARTED','DISPATCHING')", (parent['seat'],)).fetchone():
                raise IntegrationError('LOCAL_REJECTION_WORK_NOT_SETTLED')
            artifact = db.execute('SELECT body FROM c_artifact WHERE hash=?', (body_sha256,)).fetchone()
            if row['hash'] != body_sha256 or not artifact or digest(artifact[0]) != body_sha256:
                raise IntegrationError('LOCAL_REJECTION_BODY_MISMATCH')
            try:
                body = json.loads(artifact[0])
                if encode(body).encode() != artifact[0] or set(body) != {'content', 'mentions'} or not isinstance(body['content'], str) or not isinstance(body['mentions'], list) or not body['mentions'] or not all(isinstance(m, str) and m.lstrip('@') for m in body['mentions']):
                    raise ValueError()
                events = {}
                kinds = {'intent': 'SEND_INTENT', 'request': 'STDOUT_RPC', 'callback': 'CALLBACK_INTENT', 'unknown': 'DELIVERY_UNKNOWN', 'result': 'STDIN_RPC', 'completed': 'STDOUT_RPC'}
                for name, ref in evidence_refs.items():
                    if not isinstance(ref, dict) or set(ref) != {'seq', 'artifact_id'} or type(ref['seq']) is not int or not isinstance(ref['artifact_id'], str):
                        raise ValueError()
                    event = db.execute('SELECT * FROM c_event WHERE seq=?', (ref['seq'],)).fetchone()
                    raw = db.execute('SELECT body FROM c_artifact WHERE hash=?', (ref['artifact_id'],)).fetchone()
                    if not event or event['operation'] != operation or event['kind'] != kinds[name] or not raw or digest(raw[0]) != ref['artifact_id'] or raw[0] != event['body'].encode():
                        raise ValueError()
                    events[name] = json.loads(event['body'])
                seq = {k: v['seq'] for k, v in evidence_refs.items()}
                if not seq['request'] < seq['callback'] < seq['intent'] < seq['unknown'] < seq['result'] < seq['completed']:
                    raise ValueError()
                if events['intent'] != {'id': outbox_id, 'hash': body_sha256} or events['unknown'] != {'attempt': attempt, 'data': {'outbox_id': outbox_id}}:
                    raise ValueError()
                frames = {}
                clients = set()
                for name in ('request', 'result', 'completed'):
                    event = events[name]
                    if event['attempt'] != attempt:
                        raise ValueError()
                    clients.add(event['data']['client_id'])
                    frames[name] = event['data']['payload']
                if len(clients) != 1 or not next(iter(clients)):
                    raise ValueError()
                request, result, completed = (frames[n] for n in ('request', 'result', 'completed'))
                params = request['params']
                if request['method'] != 'item/tool/call' or params['tool'] != 'band_send_message' or params['arguments'] != body or request['id'] != result['id'] or not params['callId']:
                    raise ValueError()
                callback_hash = digest(encode({'method': request['method'], 'params': params}).encode())
                cb = events['callback']
                if cb != {'attempt': attempt, 'id': str(request['id']), 'hash': callback_hash}:
                    raise ValueError()
                callback = db.execute('SELECT hash FROM c_callback WHERE operation=? AND attempt=? AND id=?', (operation, attempt, str(request['id']))).fetchone()
                if not callback or callback[0] != callback_hash:
                    raise ValueError()
                item = completed['params']['item']
                if completed['method'] != 'item/completed' or item['type'] != 'dynamicToolCall' or item['tool'] != params['tool'] or item['id'] != params['callId'] or item['arguments'] != body or item['status'] != 'failed' or item['success'] is not False or completed['params']['threadId'] != params['threadId'] or completed['params']['turnId'] != params['turnId'] or params['threadId'] != parent['thread']:
                    raise ValueError()
                # Exact SDK error reconstruction. No loose HTTP/network ValueError match.
                probe = sdk_agent.AgentTools(parent['room'], None, participants=[])
                try:
                    probe._resolve_required_mentions(body['mentions'])
                except ValueError as exc:
                    expected = {'success': False, 'contentItems': [{'type': 'inputText', 'text': 'Error: ' + str(exc)}]}
                else:
                    raise ValueError()
                if result['result'] != expected or item['contentItems'] != expected['contentItems']:
                    raise ValueError()
                # A unique matching intent between this request and failed result
                # is required; room absence and another operation's error are not proof.
                intents = [json.loads(r[0]) for r in db.execute("SELECT body FROM c_event WHERE operation=? AND kind='SEND_INTENT' AND seq>? AND seq<?", (operation, seq['request'], seq['result']))]
                if sum(i.get('hash') == body_sha256 for i in intents) != 1:
                    raise ValueError()
            except (ValueError, TypeError, KeyError, AttributeError):
                raise IntegrationError('LOCAL_REJECTION_EVIDENCE_MISMATCH') from None
            receipt = {'effect': 'NOT_SENT', 'code': 'LOCAL_SEND_VALIDATION_REJECTED', 'class': 'SDK4_EMPTY_CACHE_UNKNOWN_MENTION', 'attempt': attempt, 'body_sha256': body_sha256, 'sdk_sha256': pinned, 'evidence_refs': evidence_refs}
            if row['state'] == 'REJECTED' and row['receipt'] == encode(receipt):
                return {'id': outbox_id, 'state': 'REJECTED', **receipt}
            if row['state'] != 'DELIVERY_UNKNOWN':
                raise IntegrationError('LOCAL_REJECTION_STATE_CONFLICT')
            db.execute("UPDATE c_outbox SET state='REJECTED',receipt=? WHERE id=? AND state='DELIVERY_UNKNOWN'", (encode(receipt), outbox_id))
            Mailbox.event(db, operation, 'SEND_REJECTION_RECONCILED', {'id': outbox_id, **receipt})
            return {'id': outbox_id, 'state': 'REJECTED', **receipt}

    def _authorized(self, db, operation):
        scope = self.router._scope(db)
        cap = scope.get('continuation') if scope else None
        if not isinstance(cap, dict):
            return False
        seen = set()
        while operation not in seen:
            seen.add(operation)
            if operation in cap.get('operations', []):
                return True
            row = db.execute('SELECT * FROM c_recovery WHERE continuation=?', (operation,)).fetchone()
            if not row:
                return False
            proof = json.loads(row['proof'])
            if proof['run_id'] != self.router.run_id or proof['grant_id'] != self.router.grant_id or proof['seat'] != self.router.seat or proof['room'] != self.router.room:
                return False
            operation = row['operation']
        return False

    def _parent(self, db, operation, attempt, *, settled_interruption=False):
        if not self._authorized(db, operation):
            raise IntegrationError('CONTINUATION_GRANT_REQUIRED')
        parent = db.execute('SELECT * FROM c_work WHERE id=?', (operation,)).fetchone()
        if not parent or parent['attempt'] != attempt or parent['seat'] != self.router.seat or parent['room'] != self.router.room:
            raise IntegrationError('OWNER_ATTEMPT_FENCE')
        deliveries = {'RETURNED', 'YIELDED', 'RECONCILED'} | ({'DELIVERY_UNKNOWN'} if settled_interruption else set())
        if parent['delivery'] not in deliveries or parent['state'] not in {'SUCCEEDED', 'FAILED', 'CANCELLED', 'PAUSED'} or not parent['thread']:
            raise IntegrationError('CONTINUATION_DELIVERY_UNKNOWN')
        if db.execute("SELECT 1 FROM c_outbox o JOIN c_work w ON w.id=o.operation WHERE w.seat=? AND o.state NOT IN ('ACKED','REJECTED')", (parent['seat'],)).fetchone() or db.execute("SELECT 1 FROM c_work WHERE seat=? AND delivery IN ('DISPATCHING','STARTED','DELIVERY_UNKNOWN') AND id!=?", (parent['seat'], operation if settled_interruption else '')).fetchone():
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
        raw_index = self.broker._index_bytes()
        indexed = indexed_entries(raw_index)
        if indexed != tree_entries(self.broker, head):
            raise IntegrationError('CONTINUATION_INDEX_TREE_MISMATCH')
        body = {'identity': identity, 'ref': ref, 'head': head, 'indexed_entries': indexed, 'files': files}
        return {'sha256': digest(encode(body).encode()), 'index': digest(raw_index), **body}

    def _predecessors(self, db, operation):
        return db.execute('SELECT * FROM c_recovery WHERE continuation=?', (operation,)).fetchone(), []

    def observe(self, operation, attempt):
        with self.owner.transaction(self.owner.epoch) as db:
            parent = self._parent(db, operation, attempt)
            prior, ancestors = self._predecessors(db, operation)
            effects, settled_outbox = [], []
            for op in list(reversed(ancestors)) + [operation]:
                effects.extend(dict(r) for r in db.execute("SELECT * FROM c_git_effect WHERE operation=? AND state='ACKED' ORDER BY rowid", (op,)))
                settled_outbox.extend(dict(r) for r in db.execute("SELECT * FROM c_outbox WHERE operation=? AND state IN ('ACKED','REJECTED') ORDER BY rowid", (op,)))
            commits = []
            for effect in effects:
                receipt = json.loads(effect['receipt']) if effect['receipt'] else {}
                if receipt.get('state') != 'COMMITTED':
                    continue
                creator = db.execute('SELECT attempt FROM c_work WHERE id=?', (effect['operation'],)).fetchone()
                if not creator or effect['attempt'] != creator['attempt'] or not effect['candidate'] or encode(receipt) != encode(json.loads(effect['candidate'])):
                    raise IntegrationError('CONTINUATION_COMMIT_UNKNOWN')
                index = db.execute('SELECT body FROM c_artifact WHERE hash=?', (receipt['index_after'],)).fetchone()
                if not index or digest(index[0]) != receipt['index_after']:
                    raise IntegrationError('CONTINUATION_COMMIT_UNKNOWN')
                commits.append(receipt)
        # A timeout child keeps its acknowledged ancestor receipts; an unrelated
        # new HEAD must not look like source progress merely because this slice
        # made no Git call. Validate the exact proof/lineage, not input claims.
        if prior:
            prior_id = prior['id']
            with self.owner.transaction(self.owner.epoch) as db:
                artifact = db.execute('SELECT body FROM c_artifact WHERE hash=?', (prior_id,)).fetchone()
            if not prior or not artifact or digest(artifact[0]) != prior_id or artifact[0].decode() != prior['proof']:
                raise IntegrationError('CONTINUATION_LINEAGE_CONFLICT')
            proof = json.loads(prior['proof'])
            if (proof['grant_id'], proof['run_id'], proof['seat'], proof['room']) != (self.router.grant_id, self.router.run_id, parent['seat'], parent['room']):
                raise IntegrationError('CONTINUATION_LINEAGE_CONFLICT')
            commits = proof['committed_receipts'] + commits
            settled_outbox = proof.get('settled_outbox', []) + settled_outbox
        self._quiescent()
        first, second = self.fingerprint(), self.fingerprint()
        if not same_source(first, second):
            raise IntegrationError('CONTINUATION_SOURCE_CHANGED')
        previous = None
        for receipt in commits:
            # Immutable object and receipt readback for every acknowledged commit.
            # Older commits must form the exact chain, not equal the latest HEAD.
            raw = self.broker._git('cat-file', '-p', receipt['commit']).decode()
            headers = raw.split('\n\n', 1)[0].splitlines()
            if receipt['git_identity'] != second['identity'] or receipt['ref'] != second['ref'] or [h[7:] for h in headers if h.startswith('parent ')] != [receipt['parent']] or [h[5:] for h in headers if h.startswith('tree ')] != [receipt['tree']]:
                raise IntegrationError('CONTINUATION_COMMIT_UNKNOWN')
            with self.owner.transaction(self.owner.epoch) as db:
                for key, tree in (('index_before', receipt['parent']), ('index_after', receipt['tree'])):
                    artifact = db.execute('SELECT body FROM c_artifact WHERE hash=?', (receipt[key],)).fetchone()
                    # Legacy broker preserved only the before hash. Its trusted
                    # receipt + exact parent chain and every after index/tree
                    # still bind committed contents; never infer missing bytes.
                    if key == 'index_before' and artifact is None:
                        continue
                    if not artifact or digest(artifact[0]) != receipt[key] or indexed_entries(artifact[0]) != tree_entries(self.broker, tree):
                        raise IntegrationError('CONTINUATION_COMMIT_UNKNOWN')
            if previous and receipt['parent'] != previous['commit']:
                raise IntegrationError('CONTINUATION_COMMIT_UNKNOWN')
            previous = receipt
        if previous and (previous['commit'] != second['head'] or tree_entries(self.broker, previous['tree']) != second['indexed_entries']):
            raise IntegrationError('CONTINUATION_COMMIT_UNKNOWN')
        proof = {'schema': 'dh-maintenance-proof-v1', 'operation': operation, 'attempt': attempt,
                 'run_id': self.router.run_id, 'grant_id': self.router.grant_id, 'seat': parent['seat'],
                 'room': parent['room'], 'thread': parent['thread'], 'input_sha256': digest(parent['input'].encode()),
                 'delivery': parent['delivery'], 'state': parent['state'], 'source': second,
                 'effect': 'KNOWN_COMMITTED_EFFECT' if commits else 'VERIFIED_NO_OUTSTANDING_EFFECT',
                 'committed_receipts': commits, 'settled_outbox': settled_outbox, 'verification': 'settled delivery + ACKed effects + owned/absent processes + two identical canonical source readbacks'}
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
        if not same_source(self.fingerprint(), proof['source']):
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
