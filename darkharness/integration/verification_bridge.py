"""SDK projection and authenticated, single completion continuation.

The checker outlives the native turn, but not its actively owned work attempt.
Private controller results are never serialized directly to SDK or Band.
"""
from __future__ import annotations

import json
from pathlib import Path
from .mailbox import Mailbox, IntegrationError, digest, encode
from .verification import VerificationBroker, _hash_file
from .report_criteria import JsonReportCriteria


class VerificationYield(BaseException):
    def __init__(self, effect_id):
        self.effect_id = effect_id


class VerificationBridge:
    def __init__(self, mailbox, router, guard, *, report_parsers=None, receipt_run_resolver=None):
        self.box, self.router, self.guard = mailbox, router, guard
        parsers = {'json-criteria-v1': JsonReportCriteria()}
        parsers.update(report_parsers or {})
        self.broker = VerificationBroker(mailbox, router, report_parsers=parsers,
                                         receipt_run_resolver=receipt_run_resolver)
        with mailbox.owner.transaction(mailbox.owner.epoch) as db:
            db.execute('CREATE TABLE IF NOT EXISTS c_verification_continuation(effect TEXT PRIMARY KEY, result_ref TEXT NOT NULL, child TEXT NOT NULL)')

    def schemas(self):
        with self.box.owner.transaction(self.box.owner.epoch) as db:
            scope = self.router._scope(db)
            checks = (scope or {}).get('verification', {}).get('checks', {})
        if not checks:
            return []
        return [
            {'name': 'dh_verify', 'description': 'Start a configured trusted check of an exact peer revision and snapshot. Ends this native turn; an authenticated completion continues the full task. Starting is NOT success. Never poll or replay UNKNOWN.',
             'inputSchema': {'type': 'object', 'properties': {'check_id': {'type': 'string', 'enum': sorted(checks)}, 'checkout_receipt': {'anyOf': [{'type': 'string'}, {'type': 'object'}]}, 'revision': {'type': 'string', 'pattern': '^[0-9a-f]{40}$'}}, 'required': ['check_id', 'checkout_receipt', 'revision'], 'additionalProperties': False}},
            {'name': 'dh_verification_read', 'description': 'Read a guarded page of a hash-verified completed same-run checker log/report. artifact is a declared report name or stdout/stderr, not a path.',
             'inputSchema': {'type': 'object', 'properties': {'effect_id': {'type': 'string'}, 'artifact': {'type': 'string'}, 'offset': {'type': 'integer', 'minimum': 0}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 16384}}, 'required': ['effect_id', 'artifact', 'offset', 'limit'], 'additionalProperties': False}}
        ]

    async def start(self, operation, attempt, call_id, arguments):
        self.guard.require_clean(arguments)
        if not call_id or not isinstance(arguments, dict) or set(arguments) != {'check_id', 'checkout_receipt', 'revision'}:
            raise IntegrationError('TYPED_VERIFICATION_ARGUMENTS_REQUIRED')
        effect = digest(encode([operation, attempt, call_id, 'dh_verify']).encode())
        await self.broker.start_async(operation, attempt, effect, **arguments)
        public = self.broker.public_result(operation, attempt, effect)
        self.guard.require_clean(public)
        return effect, public

    def _result(self, db, effect):
        row = db.execute('SELECT * FROM c_verification_effect WHERE id=?', (effect,)).fetchone()
        if not row or row['run_id'] != self.router.run_id or not row['result']:
            raise IntegrationError('SAME_RUN_RESULT_REQUIRED')
        raw = row['result'].encode()
        ref = digest(raw)
        stored = db.execute('SELECT body FROM c_artifact WHERE hash=?', (ref,)).fetchone()
        if not stored or bytes(stored[0]) != raw or digest(bytes(stored[0])) != ref:
            raise IntegrationError('RESULT_HASH_MISMATCH')
        result = json.loads(raw)
        if result.get('run_id') != self.router.run_id or result.get('state') != row['state']:
            raise IntegrationError('RESULT_BINDING_MISMATCH')
        return row, result, ref

    def complete(self, operation, attempt, effect):
        """One atomic continuation only for actual known success/failure, never STOP."""
        with self.box.owner.transaction(self.box.owner.epoch) as db:
            row, result, ref = self._result(db, effect)
            if row['operation'] != operation or row['attempt'] != attempt:
                raise IntegrationError('OWNER_ATTEMPT_FENCE')
            old = db.execute('SELECT * FROM c_verification_continuation WHERE effect=?', (effect,)).fetchone()
            if old:
                if old['result_ref'] != ref:
                    raise IntegrationError('RESULT_HASH_MISMATCH')
                return old['child']
            parent = db.execute('SELECT * FROM c_work WHERE id=?', (operation,)).fetchone()
            scope = self.router._scope(db)
            run = db.execute("SELECT body FROM controls WHERE kind='run' AND id=?", (self.router.run_id,)).fetchone()
            uncertain = run and json.loads(run[0]).get('state') in {'UNKNOWN', 'DELIVERY_UNKNOWN', 'EFFECT_UNKNOWN', 'CLOSED_UNRESOLVED'}
            request, configuration = json.loads(row['request']), json.loads(row['config'])
            still_authorized = self.broker._live(db, operation, attempt, request, configuration['check'], configuration['restriction'])
            pending_stop = db.execute("SELECT 1 FROM c_control WHERE operation=? AND attempt=? AND kind='cancel'", (operation, attempt)).fetchone()
            if not scope or not still_authorized or uncertain or pending_stop or not parent or parent['attempt'] != attempt or parent['seat'] != self.router.seat or parent['room'] != self.router.room or parent['state'] != 'RUNNING' or parent['delivery'] not in {'STARTED', 'DISPATCHING'} or result['state'] not in {'SUCCEEDED', 'FAILED'}:
                Mailbox.event(db, operation, 'VERIFICATION_COMPLETION_EVIDENCE_ONLY', {'attempt': attempt, 'effect': effect, 'result_ref': ref})
                return None
            public = {k: result.get(k) for k in ('state', 'accepted', 'exit_code', 'reason', 'revision')}
            public.update(id=effect, result_ref=ref, artifact_hashes={k: v['sha256'] for k, v in result['artifacts'].items()})
            self.guard.require_clean(public)
            body = json.loads(parent['input'])
            body['content'] = encode({'original_task': body['content'], 'verification_result': public})
            body['parent_operation'] = operation
            child = digest(encode([effect, ref, 'verification-continuation']).encode())
            db.execute("INSERT INTO c_work VALUES(?,?,?,?,NULL,'QUEUED','READY',?,NULL)", (child, parent['seat'], parent['room'], encode(body), parent['thread']))
            db.execute('INSERT INTO c_verification_continuation VALUES(?,?,?)', (effect, ref, child))
            db.execute("UPDATE c_work SET state='PAUSED',delivery='YIELDED' WHERE id=? AND attempt=?", (operation, attempt))
            Mailbox.event(db, operation, 'VERIFICATION_CONTINUATION', {'attempt': attempt, 'effect': effect, 'result_ref': ref, 'child': child})
            return child

    def read_page(self, operation, attempt, *, effect_id, artifact, offset, limit):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 16384:
            raise IntegrationError('BOUNDED_PAGE_REQUIRED')
        with self.box.owner.transaction(self.box.owner.epoch) as db:
            work = self.broker._work(db, operation, attempt)
            scope = self.router._scope(db)
            if not scope or work['state'] != 'RUNNING':
                raise IntegrationError('GRANT_INACTIVE')
            row, result, ref = self._result(db, effect_id)
            creator = db.execute('SELECT seat,room FROM c_work WHERE id=?', (row['operation'],)).fetchone()
            cfg = json.loads(row['config'])['check']
            if not creator or creator['seat'] not in scope.get('seats', []) or creator['room'] != self.router.room or artifact not in ['stdout', 'stderr', *cfg['report_files']]:
                raise IntegrationError('SAME_RUN_RESULT_REQUIRED')
            item = result['artifacts'].get(artifact)
            expected = Path(row['output']) / artifact
            if not item or item['path'] != str(expected) or not expected.is_relative_to(Path(row['output'])):
                raise IntegrationError('ARTIFACT_BINDING_MISMATCH')
        # Full-file guarding BEFORE paging avoids splitting a secret across pages.
        # This is an export cap, not a checker runtime/disk budget.
        if item['bytes'] > 1048576:
            raise IntegrationError('ARTIFACT_EXPORT_TOO_LARGE')
        if _hash_file(expected) != item:
            raise IntegrationError('ARTIFACT_HASH_MISMATCH')
        with expected.open('rb') as f:
            raw = f.read(1048577)
        if len(raw) != item['bytes'] or digest(raw) != item['sha256']:
            raise IntegrationError('ARTIFACT_HASH_MISMATCH')
        text = self.guard.redact(raw.decode('utf-8', 'replace'))
        public = {'id': effect_id, 'result_ref': ref, 'artifact': artifact, 'sha256': item['sha256'], 'offset': offset, 'text': text[offset:offset + limit], 'next_offset': offset + limit if offset + limit < len(text) else None}
        self.guard.require_clean(public)
        return public
