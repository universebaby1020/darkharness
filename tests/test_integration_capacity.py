"""Anonymous capacity trace; component tests do not call providers or Band."""
import json
from pathlib import Path
import tempfile
import unittest
import sys
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

from test_integration_mailbox import Owner
from darkharness.integration.mailbox import Mailbox, IntegrationError, encode, digest
from darkharness.integration.policy import ApprovalRouter
from darkharness.integration.git_broker import LocalGitBroker
from darkharness.integration.thread_ownership import ThreadOwnership
from darkharness.integration.codex_capacity import CodexCapacityRecovery


ERROR = {'message': 'Selected model is at capacity. Please try a different model.',
         'codexErrorInfo': 'serverOverloaded', 'additionalDetails': None, 'misalignment': None}


class CapacityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.owner = Owner()
        self.addCleanup(self.owner.db.close)
        self.box = Mailbox(self.owner)
        self.op = self.box.receive('s', 'r', 'peer', 'message', 'original full task', envelope={'metadata': {'keep': True}})['work']
        self.box.claim(self.op, 'a')
        self.box.update(self.op, 'a', state='PAUSED', delivery='DELIVERY_UNKNOWN', thread='thread')
        self.owner.grant(self.root, continuation={'operations': [self.op]})
        self.prepare_repo()
        self.router = ApprovalRouter(self.owner, 'g', 's', 'r', 'run', self.root)
        self.broker = LocalGitBroker(self.box, self.router, 'fixture', 'fixture@actors.invalid', self.root)
        ThreadOwnership(self.box, self.router, binding='fixture-settings').bind('thread', 'compatibility')
        self.owner.db.execute('CREATE TABLE c_run_settings(run_id TEXT PRIMARY KEY, body TEXT, hash TEXT)')
        body = encode({'room': 'r', 'seats': [{'alias': 's', 'settings_sha256': 'fixture-settings'}]})
        self.owner.db.execute('INSERT INTO c_run_settings VALUES(?,?,?)', ('run', body, digest(body.encode())))
        with self.owner.transaction(self.owner.epoch) as db:
            Mailbox.event(db, None, 'RUNTIME_BINDINGS', {'run_id': 'run', 'bindings': [{
                'settings_sha256': 'fixture-settings', 'runtime': 'codex', 'workspace': str(self.root),
                'model': 'fixture-model', 'effort': 'high'}]})
        self.recovery = CodexCapacityRecovery(self.box, self.router, self.broker)
        # Component only: full Linux integration below runs actual pin/source/cessation.
        self.addCleanup(patch.stopall)
        patch('darkharness.integration.codex_capacity.verify_sdk_pin').start()
        patch.object(self.recovery, '_quiescent').start()
        patch.object(self.recovery, 'fingerprint', return_value={
            'sha256': 'source', 'head': 'head', 'identity': 'identity', 'ref': 'ref',
            'indexed_entries': [], 'files': []}).start()
        self.trace()

    def prepare_repo(self):
        pass

    def record(self, kind, body):
        self.box.observe(self.op, 'a', kind, body)

    def rpc(self, kind, payload):
        self.record(kind, {'client_id': 'client', 'payload': payload})

    def trace(self):
        self.rpc('PROCESS_STARTED', {'cwd': str(self.root), 'group': 99999999, 'identity': [99999999, 'boot', '1']})
        if getattr(self, 'telemetry', False):
            self.telemetry_counter = 0
            self.rpc('STDIN_RPC', {'id': 10, 'method': 'thread/resume', 'params': {'threadId': 'thread'}})
            self.rpc('STDOUT_RPC', {'id': 10, 'result': {'thread': {'id': 'thread'}}})
            self.report('UUID: thread\nTask: Codex thread\nStatus: resumed\nSummary: Room: r', 'task',
                        {'codex_resumed': True, 'codex_room_id': 'r', 'codex_thread_id': 'thread'})
            self.usage_event()
        self.rpc('STDIN_RPC', {'id': 1, 'method': 'turn/start', 'params': {
            'threadId': 'thread', 'cwd': str(self.root), 'model': 'fixture-model', 'effort': 'high',
            'approvalPolicy': 'on-request', 'sandboxPolicy': {'type': 'workspaceWrite'},
            'input': [{'type': 'text', 'text': 'original full task'}]}})
        self.rpc('STDOUT_RPC', {'id': 1, 'result': {'turn': {'id': 'turn', 'status': 'inProgress'}}})
        self.record('TURN_ACCEPTED', {'session': 'thread', 'turn': 'turn', 'cwd': str(self.root)})
        if getattr(self, 'telemetry', False):
            self.report('UUID: turn\nTask: Codex turn lifecycle\nStatus: started\nSummary: Thread: thread', 'task',
                        {'codex_event_type': 'turn_lifecycle', 'codex_room_id': 'r', 'codex_thread_id': 'thread',
                         'codex_turn_id': 'turn', 'codex_turn_status': 'started', 'codex_input_summary': 'original full task'})
        for method in ('item/started', 'item/completed'):
            self.rpc('STDOUT_RPC', {'method': method, 'params': {'threadId': 'thread', 'turnId': 'turn',
                     'item': {'id': 'user', 'type': 'userMessage', 'content': []}}})
        if getattr(self, 'telemetry', False):
            self.report_usage()
            self.usage_event()
            self.report_usage()
        self.rpc('STDOUT_RPC', {'method': 'error', 'params': {'threadId': 'thread', 'turnId': 'turn', 'error': ERROR, 'willRetry': False}})
        self.rpc('STDOUT_RPC', {'method': 'turn/completed', 'params': {'threadId': 'thread', 'turn': {
            'id': 'turn', 'status': 'failed', 'error': ERROR, 'items': [], 'itemsView': 'notLoaded'}}})
        if getattr(self, 'telemetry', False):
            self.report(ERROR['message'], 'error', {'failure': {'provider': 'codex', 'code': None,
                'message': ERROR['message'], 'detail': {'codex_room_id': 'r', 'codex_thread_id': 'thread', 'codex_turn_id': 'turn'}}})
        self.record('TURN_OUTCOME', {'thread_id': 'thread', 'turn_id': 'turn', 'room_id': 'r',
                    'turn_status': 'failed', 'turn_error': ERROR['message'], 'settled_reply': False,
                    'include_reply': False, 'final_text': '', 'duration_s': 7.635})
        if getattr(self, 'telemetry', False):
            self.report('UUID: turn\nTask: Codex turn lifecycle\nStatus: failed\nSummary: Duration: 7.6s | Thread: thread', 'task',
                        {**self.usage_metadata(), 'codex_turn_id': 'turn', 'codex_turn_status': 'failed',
                         'codex_error': ERROR['message'], 'codex_duration_s': round(7.635, 2)})
        self.record('RUNTIME_ERROR', {'code': 'TurnResultAlreadyReported', 'diagnostic': ERROR['message'], 'replay': 'FENCED'})
        self.rpc('PROCESS_STOPPED', {'members': []})

    def usage_metadata(self):
        return {'codex_event_type': 'token_usage', 'codex_room_id': 'r', 'codex_thread_id': 'thread',
                'codex_input_tokens': 1200, 'codex_output_tokens': 30, 'codex_reasoning_tokens': 10, 'codex_total_tokens': 1230}

    def usage_event(self):
        self.rpc('STDOUT_RPC', {'method': 'thread/tokenUsage/updated', 'params': {'threadId': 'thread',
                 'tokenUsage': {'total': {'inputTokens': 1200, 'outputTokens': 30, 'reasoningOutputTokens': 10, 'totalTokens': 1230}}}})

    def report_usage(self):
        self.report('Token usage — input: 1,200, output: 30, reasoning: 10, total: 1,230', 'task', self.usage_metadata())

    def report(self, content, message_type, metadata):
        self.telemetry_counter += 1
        identifier = digest(encode([self.op, 'a', 'adapter', 'send_event', self.telemetry_counter]).encode())
        self.box.prepare_send(identifier, self.op, {'content': content, 'message_type': message_type, 'metadata': metadata})
        self.box.sent(identifier, {'id': 'platform-receipt-' + str(self.telemetry_counter), 'message_type': message_type, 'success': True})

    def with_telemetry(self):
        self.owner.db.execute('DELETE FROM c_event WHERE operation=?', (self.op,))
        self.telemetry = True
        self.trace()

    def test_six_acked_sdk_reports_are_preserved_without_business_effects(self):
        self.with_telemetry()
        result = self.recovery.recover(self.op, 'a')
        proof = json.loads(self.owner.db.execute('SELECT proof FROM c_recovery WHERE id=?', (result['evidence_id'],)).fetchone()[0])
        self.assertEqual(len(proof['settled_outbox']), 6)
        self.assertTrue(all(o['state'] == 'ACKED' for o in proof['settled_outbox']))
        receipt = json.loads(self.box.read_work(self.op)['result'])
        self.assertEqual(len(receipt['acknowledged_sdk_reports']), 6)
        self.assertEqual(result, self.recovery.recover(self.op, 'a'))

    def test_sdk_report_body_binding_receipt_and_hash_mismatches_rejected(self):
        self.with_telemetry()
        old_rows = list(self.owner.db.execute('SELECT * FROM c_outbox'))
        old_events = list(self.owner.db.execute('SELECT seq,body FROM c_event'))
        cases = [
            (0, lambda b: b['metadata'].update(codex_room_id='other-room')),
            (1, lambda b: b['metadata'].update(codex_turn_id='other-turn')),
            (1, lambda b: b['metadata'].update(codex_input_summary='replacement task')),
            (2, lambda b: b['metadata'].update(codex_total_tokens=999999)),
            (4, lambda b: b['metadata']['failure'].update(code='different')),
            (5, lambda b: b['metadata'].update(codex_duration_s=999)),
            (1, lambda b: b.update(content='business reply', metadata={})),
        ]
        for index, change in cases:
            with self.subTest(index=index):
                row = old_rows[index]
                body = json.loads(self.owner.db.execute('SELECT body FROM c_artifact WHERE hash=?', (row['hash'],)).fetchone()[0])
                change(body)
                with self.owner.transaction(self.owner.epoch) as db:
                    h = Mailbox.artifact(db, encode(body).encode())
                self.owner.db.execute('UPDATE c_outbox SET hash=? WHERE id=?', (h, row['id']))
                for event in old_events:
                    data = json.loads(event['body'])
                    if data == {'id': row['id'], 'hash': row['hash']}:
                        data['hash'] = h
                        self.owner.db.execute('UPDATE c_event SET body=? WHERE seq=?', (encode(data), event['seq']))
                self.assert_blocked('REPORT_NOT_SDK_TELEMETRY')
                self.owner.db.executemany('UPDATE c_outbox SET hash=? WHERE id=?', [(r['hash'], r['id']) for r in old_rows])
                self.owner.db.executemany('UPDATE c_event SET body=? WHERE seq=?', [(r['body'], r['seq']) for r in old_events])
        self.owner.db.execute("UPDATE c_outbox SET receipt=? WHERE id=?", (encode({'id': 'ack', 'message_type': 'task', 'success': False}), old_rows[0]['id']))
        self.assert_blocked('REPORT_EVIDENCE_MISMATCH')
        self.owner.db.execute('UPDATE c_outbox SET receipt=? WHERE id=?', (old_rows[0]['receipt'], old_rows[0]['id']))
        self.owner.db.execute("UPDATE c_outbox SET state='DELIVERY_UNKNOWN' WHERE id=?", (old_rows[0]['id'],))
        self.assert_blocked('OUTSTANDING_UNKNOWN')
        self.owner.db.execute("UPDATE c_outbox SET state='ACKED'")
        self.owner.db.execute("DELETE FROM c_event WHERE kind='SEND_ACK' AND seq=(SELECT MIN(seq) FROM c_event WHERE kind='SEND_ACK')")
        self.assert_blocked('REPORT_EVIDENCE_MISMATCH')

    def test_sdk_reporting_does_not_hide_native_effect_or_token_delta(self):
        self.with_telemetry()
        self.rpc('STDOUT_RPC', {'method': 'item/completed', 'params': {'threadId': 'thread', 'turnId': 'turn',
                 'item': {'id': 'effect', 'type': 'commandExecution', 'status': 'completed'}}})
        self.assert_blocked('EFFECT_OBSERVED')
        self.owner.db.execute('DELETE FROM c_event WHERE seq=(SELECT MAX(seq) FROM c_event)')
        row = self.owner.db.execute("SELECT seq,body FROM c_event WHERE kind='STDOUT_RPC' AND body LIKE '%tokenUsage%' ORDER BY seq DESC LIMIT 1").fetchone()
        body = json.loads(row['body'])
        body['data']['payload']['params']['tokenUsage']['total']['totalTokens'] += 1
        self.owner.db.execute('UPDATE c_event SET body=? WHERE seq=?', (encode(body), row['seq']))
        self.assert_blocked('REPORT_TOKEN_DELTA')

    def mutate(self, kind, change, method=None):
        rows = self.owner.db.execute('SELECT seq,body FROM c_event WHERE operation=? AND kind=?', (self.op, kind)).fetchall()
        for row in rows:
            body = json.loads(row['body'])
            if method and body['data'].get('payload', {}).get('method') != method:
                continue
            change(body['data'])
            self.owner.db.execute('UPDATE c_event SET body=? WHERE seq=?', (encode(body), row['seq']))

    def test_manual_capacity_recovery_preserves_input_thread_single_child(self):
        result = self.recovery.recover(self.op, 'a')
        self.assertEqual(result, self.recovery.recover(self.op, 'a'))
        parent, child = self.box.read_work(self.op), self.box.read_work(result['id'])
        self.assertEqual((parent['state'], parent['delivery']), ('FAILED', 'RECONCILED'))
        self.assertEqual(child['thread'], 'thread')
        self.assertEqual(json.loads(child['input'])['content'], 'original full task')
        self.assertEqual(json.loads(child['input'])['metadata'], {'keep': True})
        receipt = json.loads(parent['result'])
        self.assertEqual(receipt['classification'], 'SETTLED_NATIVE_CAPACITY_FAILURE')
        self.assertEqual(receipt['model'], 'fixture-model')
        self.assertEqual(receipt['acceptance'], 'NOT_EVALUATED')
        self.assertEqual(self.owner.db.execute('SELECT COUNT(*) FROM c_work').fetchone()[0], 2)
        self.assertEqual(self.owner.db.execute('SELECT COUNT(*) FROM c_inbox').fetchone()[0], 1)
        self.assertEqual(self.owner.db.execute("SELECT COUNT(*) FROM c_event WHERE kind='SETTLED_NATIVE_CAPACITY'").fetchone()[0], 1)
        for ref in receipt['evidence_refs']:
            raw = self.owner.db.execute('SELECT body FROM c_artifact WHERE hash=?', (ref['artifact_id'],)).fetchone()[0]
            event = self.owner.db.execute('SELECT body FROM c_event WHERE seq=?', (ref['seq'],)).fetchone()[0]
            self.assertEqual(raw, event.encode())

    def test_terminal_preflight_is_read_only(self):
        from darkharness.integration.codex_capacity import terminal_evidence
        before = self.owner.db.total_changes
        proof = terminal_evidence(self.owner.db, self.box.read_work(self.op), self.router)
        self.assertEqual(proof['classification'], 'SETTLED_NATIVE_CAPACITY_FAILURE')
        self.assertEqual(self.owner.db.total_changes, before)

    def test_timeout_capability_is_not_capacity_permission(self):
        self.owner.grant(self.root, settled_timeout_recovery={'enabled': True, 'run_id': 'run', 'workspace': str(self.root), 'rooms': ['r'], 'seats': ['s']})
        with self.assertRaisesRegex(IntegrationError, 'CONTINUATION_GRANT_REQUIRED'):
            self.recovery.recover(self.op, 'a')
        self.owner.grant(self.root, continuation={'operations': self.op})
        self.assert_blocked('CONTINUATION_GRANT_REQUIRED')

    def test_capacity_permission_does_not_inherit_to_child(self):
        child = self.recovery.recover(self.op, 'a')['id']
        with self.owner.transaction(self.owner.epoch) as db:
            self.assertFalse(self.recovery._authorized(db, child))

    def assert_blocked(self, pattern):
        with self.assertRaisesRegex(IntegrationError, pattern):
            self.recovery.recover(self.op, 'a')
        self.assertEqual(self.box.read_work(self.op)['delivery'], 'DELIVERY_UNKNOWN')
        self.assertEqual(self.owner.db.execute('SELECT COUNT(*) FROM c_work').fetchone()[0], 1)

    def test_exact_native_outcome_client_and_cessation_fences(self):
        original = list(self.owner.db.execute('SELECT seq,body FROM c_event'))
        cases = [
            ('STDOUT_RPC', 'turn/completed', lambda b: b['payload']['params']['turn'].update(status='interrupted'), 'NOT_NATIVE_CAPACITY'),
            ('STDOUT_RPC', 'turn/completed', lambda b: b['payload']['params']['turn']['error'].update(codexErrorInfo='other'), 'NOT_NATIVE_CAPACITY'),
            ('STDOUT_RPC', 'error', lambda b: b['payload']['params'].update(willRetry=True), 'NATIVE_ERROR'),
            ('STDOUT_RPC', 'error', lambda b: b['payload']['params'].update(turnId='other'), 'NATIVE_ERROR'),
            ('TURN_OUTCOME', None, lambda b: b.update(turn_error='generic capacity word'), 'SDK_OUTCOME'),
            ('TURN_OUTCOME', None, lambda b: b.update(settled_reply=True), 'SDK_OUTCOME'),
            ('TURN_OUTCOME', None, lambda b: b.update(include_reply=True), 'SDK_OUTCOME'),
            ('TURN_OUTCOME', None, lambda b: b.update(final_text='already replied'), 'SDK_OUTCOME'),
            ('TURN_OUTCOME', None, lambda b: b.update(thread_id='other'), 'SDK_OUTCOME'),
            ('RUNTIME_ERROR', None, lambda b: b.update(replay='RETRY'), 'RUNTIME_MISMATCH'),
            ('PROCESS_STOPPED', None, lambda b: b.update(payload={'members': [[100, 'boot', '2']]}), 'CESSATION'),
            ('PROCESS_STARTED', None, lambda b: b['payload'].update(group=42), 'OWNERSHIP'),
            ('STDIN_RPC', 'turn/start', lambda b: b.update(client_id='other'), 'CLIENT_MISMATCH'),
            ('STDIN_RPC', 'turn/start', lambda b: b['payload']['params'].update(model='different-model'), 'BINDING_MISMATCH'),
            ('STDIN_RPC', 'turn/start', lambda b: b['payload']['params']['sandboxPolicy'].update(networkAccess=True), 'SCOPE_UNKNOWN'),
        ]
        for kind, method, change, pattern in cases:
            with self.subTest(kind=kind, method=method, pattern=pattern):
                self.mutate(kind, change, method)
                self.assert_blocked(pattern)
                self.owner.db.executemany('UPDATE c_event SET body=? WHERE seq=?', [(r['body'], r['seq']) for r in original])

    def test_native_effects_callbacks_unknown_notifications_and_missing_replies(self):
        for frame in [
            {'method': 'item/started', 'params': {'threadId': 'thread', 'turnId': 'turn', 'item': {'id': 'x', 'type': 'commandExecution'}}},
            {'method': 'item/completed', 'params': {'threadId': 'thread', 'turnId': 'turn', 'item': {'id': 'x', 'type': 'fileChange', 'status': 'completed'}}},
            {'method': 'item/completed', 'params': {'threadId': 'thread', 'turnId': 'turn', 'item': {'id': 'x', 'type': 'dynamicToolCall', 'status': 'completed'}}},
            {'id': 77, 'method': 'item/tool/call', 'params': {'tool': 'band_read_messages'}},
            {'method': 'item/commandExecution/requestApproval', 'params': {}},
            {'method': 'future/native/effect', 'params': {}},
        ]:
            with self.subTest(frame=frame):
                self.rpc('STDOUT_RPC', frame)
                self.assert_blocked('CAPACITY_')
                self.owner.db.execute('DELETE FROM c_event WHERE seq=(SELECT MAX(seq) FROM c_event)')
        self.owner.db.execute("DELETE FROM c_event WHERE kind='STDOUT_RPC' AND body LIKE '%inProgress%'")
        self.assert_blocked('RPC_UNSETTLED')

    def test_missing_duplicate_misordered_and_malformed_terminal(self):
        row = self.owner.db.execute("SELECT * FROM c_event WHERE kind='TURN_OUTCOME'").fetchone()
        self.owner.db.execute('DELETE FROM c_event WHERE seq=?', (row['seq'],))
        self.assert_blocked('TERMINAL_EVIDENCE_REQUIRED')
        self.owner.db.execute('INSERT INTO c_event VALUES(?,?,?,?,?)', tuple(row))
        self.record('TURN_OUTCOME', json.loads(row['body'])['data'])
        self.assert_blocked('TERMINAL_EVIDENCE_REQUIRED')
        self.owner.db.execute('DELETE FROM c_event WHERE seq=(SELECT MAX(seq) FROM c_event)')
        self.owner.db.execute('UPDATE c_event SET seq=1000 WHERE seq=?', (row['seq'],))
        self.assert_blocked('EVIDENCE_ORDER')
        self.owner.db.execute('UPDATE c_event SET seq=? WHERE seq=1000', (row['seq'],))
        self.mutate('STDOUT_RPC', lambda b: b['payload']['params'].update(turn=None), 'turn/completed')
        self.assert_blocked('EVIDENCE_MALFORMED')

    def test_outbox_git_other_work_and_checker_unknown_remain_fenced(self):
        other = self.box.receive('s', 'r', 'peer', 'other', 'other task')['work']
        self.box.prepare_send('send', other, {'content': 'unknown'})
        with self.assertRaisesRegex(IntegrationError, 'OUTSTANDING_UNKNOWN'):
            self.recovery.recover(self.op, 'a')
        self.box.sent('send', {'id': 'receipt'})
        self.owner.db.execute("INSERT INTO c_git_effect VALUES('effect',?,'a','{}','UNKNOWN',NULL,NULL)", (other,))
        with self.assertRaisesRegex(IntegrationError, 'GIT_UNKNOWN'):
            self.recovery.recover(self.op, 'a')
        self.owner.db.execute("DELETE FROM c_git_effect")
        self.box.claim(other, 'b')
        self.box.update(other, 'b', state='RUNNING', delivery='STARTED')
        with self.assertRaisesRegex(IntegrationError, 'OUTSTANDING_UNKNOWN'):
            self.recovery.recover(self.op, 'a')
        self.box.update(other, 'b', state='SUCCEEDED', delivery='RETURNED')
        self.owner.db.execute('CREATE TABLE c_verification_effect(id TEXT,operation TEXT,state TEXT)')
        self.owner.db.execute("INSERT INTO c_verification_effect VALUES('checker',?,'UNKNOWN')", (other,))
        with self.assertRaisesRegex(IntegrationError, 'CHECKER_OUTSTANDING'):
            self.recovery.recover(self.op, 'a')
        self.owner.db.execute("UPDATE c_verification_effect SET state='SUCCEEDED'")
        self.assertTrue(self.recovery.recover(self.op, 'a')['id'])

    def test_own_acked_effect_still_outside_narrow_capacity_path(self):
        self.box.prepare_send('own', self.op, {'content': 'already sent'})
        self.box.sent('own', {'id': 'receipt'})
        self.assert_blocked('REPORT_EVIDENCE_MISMATCH')

    def test_cancel_stop_revoke_unknown_run_stale_attempt_and_binding_fences(self):
        for state in ('STOPPED', 'COMPLETED', 'REVOKED', 'UNKNOWN', 'EFFECT_UNKNOWN', 'DELIVERY_UNKNOWN', 'CLOSED_UNRESOLVED'):
            with self.subTest(state=state):
                self.owner.db.execute("INSERT OR REPLACE INTO controls VALUES('run','run',?,1)", (encode({'state': state}),))
                self.assert_blocked('GRANT_REQUIRED|RUN_UNRESOLVED')
        self.owner.db.execute("DELETE FROM controls WHERE kind='run'")
        with self.assertRaisesRegex(IntegrationError, 'OWNER_ATTEMPT_FENCE'):
            self.recovery.recover(self.op, 'stale')
        self.box.control('cancel', self.op, 'a', 'cancel', {})
        self.assert_blocked('CONTROL_PENDING_OR_CANCELLED')
        self.owner.db.execute('DELETE FROM c_control')
        self.owner.db.execute("UPDATE c_owned_thread SET binding='changed'")
        self.assert_blocked('BINDING_MISMATCH')
        self.owner.db.execute("UPDATE c_owned_thread SET binding='fixture-settings'")
        self.owner.db.execute("UPDATE controls SET body=? WHERE kind='grant'", (encode({'revoked': True}),))
        self.assert_blocked('GRANT_REQUIRED')

    def test_source_or_process_uncertainty_does_not_reconcile(self):
        with patch.object(self.recovery, '_quiescent', side_effect=IntegrationError('CONTINUATION_PROCESS_OUTSTANDING')):
            self.assert_blocked('PROCESS_OUTSTANDING')
        with patch.object(self.recovery, 'fingerprint', side_effect=[{'sha256': 'one'}, {'sha256': 'two'}]):
            self.assert_blocked('SOURCE_CHANGED')

    def test_sdk_pin_failure_does_not_reconcile(self):
        with patch('darkharness.integration.codex_capacity.verify_sdk_pin', side_effect=IntegrationError('CAPACITY_SDK_PIN_MISMATCH')):
            self.assert_blocked('SDK_PIN_MISMATCH')

    def test_pending_control_question_and_proof_tampering_are_rejected(self):
        self.box.control('control', self.op, 'a', 'permission_reply', {})
        self.assert_blocked('CONTROL_PENDING_OR_CANCELLED')
        self.owner.db.execute('DELETE FROM c_control')
        self.box.question('question', self.op, 'peer', {'question': 'unresolved'})
        self.assert_blocked('CONTROL_PENDING_OR_CANCELLED')
        self.owner.db.execute('DELETE FROM c_question')
        result = self.recovery.recover(self.op, 'a')
        self.owner.db.execute('UPDATE c_artifact SET body=? WHERE hash=?', (b'forged', result['evidence_id']))
        with self.assertRaisesRegex(IntegrationError, 'CONTINUATION_EVIDENCE_REQUIRED'):
            self.recovery.recover(self.op, 'a')

    def test_gateway_is_controller_only_and_rejects_payload_authority(self):
        from darkharness.integration.gateway import IntegrationSession
        from darkharness.core import Rejected
        session = IntegrationSession.__new__(IntegrationSession)
        session.hello, session.binding = True, {'seat': 's'}
        session.service = SimpleNamespace(environment_id='environment', store=SimpleNamespace(owner=True))
        req = {'action': 'integration.capacity.recover', 'environment_id': 'environment',
               'operation_id': self.op, 'payload': {'attempt': 'a', 'grant_id': 'g'}}
        with self.assertRaisesRegex(Rejected, 'SEAT_ACTION_DENIED'):
            session._dispatch_integration(req)
        session.binding = None
        session.service.manager = SimpleNamespace(agents=[])
        req['payload']['safe'] = True
        with self.assertRaisesRegex(Rejected, 'INVALID_RECOVERY_PAYLOAD'):
            session._dispatch_integration(req)
        del req['payload']['safe']
        session.service.recovery = lambda *_: self.recovery
        session.response = lambda req, data: data
        with patch('darkharness.integration.codex_capacity.CodexCapacityRecovery', return_value=self.recovery):
            result = session._dispatch_integration(req)
        self.assertTrue(result['id'])


@unittest.skipUnless(sys.platform == 'linux', 'Linux actual SDK source, Git and process readbacks')
class CapacityLinuxTests(CapacityTests):
    def setUp(self):
        super().setUp()
        patch.stopall()

    def prepare_repo(self):
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        (self.root / 'base').write_text('fixture source')
        subprocess.run(['git', '-C', str(self.root), 'add', 'base'], check=True)
        subprocess.run(['git', '-C', str(self.root), '-c', 'user.name=fixture', '-c', 'user.email=fixture@actors.invalid', 'commit', '-qm', 'base'], check=True)

    def test_capacity_failure_is_still_not_a_timeout(self):
        from darkharness.integration.codex_timeout import CodexTimeoutRecovery
        timeout = CodexTimeoutRecovery(self.box, self.router, self.broker)
        with self.assertRaisesRegex(IntegrationError, 'NOT_SDK_TURN_TIMEOUT'):
            timeout.recover(self.op, 'a')
        self.assertEqual(self.box.read_work(self.op)['delivery'], 'DELIVERY_UNKNOWN')


if __name__ == '__main__':
    unittest.main()
