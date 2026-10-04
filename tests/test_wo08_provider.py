"""Synthetic canonical provider proof + fixed pre-dispatch policy tests."""
from datetime import datetime, timezone
import json
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_integration_capacity as capacity
ERROR = capacity.ERROR
from darkharness.integration.mailbox import IntegrationError, encode, digest
from darkharness.integration.provider_recovery import (CodexProviderRecovery,
    prepare_settled_provider_recovery, register_policy, bind_dispatch, ALLOWED_ERRORS)


class ProviderTests(unittest.TestCase):
    # Reuse only fixture/proof helpers, not legacy operator-grant acceptance tests.
    prepare_repo = capacity.CapacityTests.prepare_repo
    trace = capacity.CapacityTests.trace
    record = capacity.CapacityTests.record
    def rpc(self, kind, payload):
        self.record(kind, {'client_id': getattr(self, 'client_id', 'client'), 'payload': payload})

    def setUp(self):
        capacity.CapacityTests.setUp(self)
        scope = {'workspace': str(self.root), 'run_id': 'run', 'seats': ['s'], 'rooms': ['r']}
        prepared = prepare_settled_provider_recovery(scope, run_id='run', workspace=str(self.root), rooms=['r'], seats=['s'], dispatch_sender='human', dispatch_room='r', dispatch_sha256=digest(b'first directive'))
        self.owner.grant(self.root, **{k: v for k, v in prepared.items() if k != 'workspace'})
        register_policy(self.box, self.router)
        self.dispatch = SimpleNamespace(sender_type='user', sender_id='human', room_id='r', id='dispatch-id', content='first directive', created_at=datetime.now(timezone.utc))
        bind_dispatch(self.box, self.router, self.dispatch)
        self.recovery = CodexProviderRecovery(self.box, self.router, self.broker)
        patch.object(self.recovery, '_quiescent').start()
        patch.object(self.recovery, 'fingerprint', return_value={'sha256': 'source', 'head': 'head', 'identity': 'identity', 'ref': 'ref', 'indexed_entries': [], 'files': []}).start()

    def change_error(self, info):
        error = {**ERROR, 'codexErrorInfo': info}
        for row in self.owner.db.execute("SELECT seq,body FROM c_event WHERE operation=?", (self.op,)).fetchall():
            body = json.loads(row['body'])
            data = body.get('data', {})
            frame = data.get('payload', {})
            if frame.get('method') == 'error':
                frame['params']['error'] = error
            elif frame.get('method') == 'turn/completed':
                frame['params']['turn']['error'] = error
            self.owner.db.execute('UPDATE c_event SET body=? WHERE seq=?', (encode(body), row['seq']))

    def test_policy_retry_zero_effect_and_persistent_backoff(self):
        before = self.owner.db.execute("SELECT body,revision FROM controls WHERE kind='grant'").fetchone()
        result = self.recovery.recover(self.op, 'a')
        self.assertEqual(result['ordinal'], 1)
        self.assertGreater(result['backoff_s'], 59)
        self.assertIsNone(self.box.next_ready('s'))
        self.assertEqual(json.loads(self.box.read_work(result['id'])['input'])['content'], 'original full task')
        again = self.recovery.recover(self.op, 'a')
        self.assertEqual(again['id'], result['id'])
        self.assertEqual(self.owner.db.execute('SELECT COUNT(*) FROM c_provider_retry').fetchone()[0], 1)
        self.assertEqual(tuple(before), tuple(self.owner.db.execute("SELECT body,revision FROM controls WHERE kind='grant'").fetchone()))

    def test_exact_error_allowlist_and_items_transport_fence(self):
        for info in sorted(ALLOWED_ERRORS - {'usageLimitExceeded'}):
            self.change_error(info)
            with self.owner.transaction(self.owner.epoch) as db:
                proof = self.recovery._terminal(db, dict(db.execute('SELECT * FROM c_work WHERE id=?', (self.op,)).fetchone()))
            self.assertEqual(proof['provider_error'], info)
        for info in ['transportClosed', 'serverOverloaded extra', {'serverOverloaded': {}}]:
            self.change_error(info)
            with self.assertRaises(IntegrationError):
                self.recovery.recover(self.op, 'a')
        self.change_error('serverOverloaded')
        self.rpc('STDOUT_RPC', {'method': 'item/started', 'params': {'threadId': 'thread', 'turnId': 'turn', 'item': {'type': 'commandExecution', 'id': 'tool'}}})
        with self.assertRaisesRegex(IntegrationError, 'EFFECT_OBSERVED'):
            self.recovery.recover(self.op, 'a')

    def test_actual_closed_transport_and_business_acks_never_retry(self):
        self.rpc('STDOUT_RPC', {'method': 'transport/closed', 'params': {}})
        with self.assertRaisesRegex(IntegrationError, 'RPC_UNKNOWN'):
            self.recovery.recover(self.op, 'a')
        self.owner.db.execute("DELETE FROM c_event WHERE operation=? AND body LIKE '%transport/closed%'", (self.op,))
        for n in range(3):
            key = 'business-' + str(n)
            self.box.prepare_send(key, self.op, {'content': 'settled business ' + str(n)})
            self.box.sent(key, {'id': key})
        with self.assertRaisesRegex(IntegrationError, 'BUSINESS_EFFECT_OBSERVED'):
            self.recovery.recover(self.op, 'a')
        self.assertEqual(self.owner.db.execute('SELECT COUNT(*) FROM c_work').fetchone()[0], 1)
        self.assertEqual(self.box.read_work(self.op)['delivery'], 'DELIVERY_UNKNOWN')

    def test_initial_thread_scope_and_owned_response_are_required(self):
        seq = self.owner.db.execute("SELECT MIN(seq) FROM c_event WHERE operation=? AND kind='STDIN_RPC'", (self.op,)).fetchone()[0]
        for row in self.owner.db.execute('SELECT seq FROM c_event WHERE seq>=? ORDER BY seq DESC', (seq,)).fetchall():
            self.owner.db.execute('UPDATE c_event SET seq=? WHERE seq=?', (row[0] + 2, row[0]))
        params = {'cwd': str(self.root), 'model': 'fixture-model', 'approvalPolicy': 'on-request', 'sandbox': 'workspace-write'}
        def write_request():
            body = encode({'attempt': 'a', 'data': {'client_id': 'client', 'payload': {'id': 20, 'method': 'thread/start', 'params': params}}})
            self.owner.db.execute('INSERT OR REPLACE INTO c_event VALUES(?,?,?,?,?)', (seq, self.op, 'STDIN_RPC', body, 'fixture'))
        write_request()
        response = {'id': 20, 'result': {'thread': {'id': 'thread'}}}
        def write_response():
            body = encode({'attempt': 'a', 'data': {'client_id': 'client', 'payload': response}})
            self.owner.db.execute('INSERT OR REPLACE INTO c_event VALUES(?,?,?,?,?)', (seq + 1, self.op, 'STDOUT_RPC', body, 'fixture'))
        write_response()
        with self.owner.transaction(self.owner.epoch) as db:
            parent = dict(db.execute('SELECT * FROM c_work WHERE id=?', (self.op,)).fetchone())
            self.recovery._terminal(db, parent)
            for key, value in [('cwd', '/foreign'), ('model', 'other-model'), ('approvalPolicy', 'never'), ('sandbox', 'danger-full-access')]:
                prior = params[key]
                params[key] = value
                write_request()
                with self.assertRaisesRegex(IntegrationError, 'SCOPE_UNKNOWN'):
                    self.recovery._terminal(db, parent)
                params[key] = prior
            write_request()
            response['result']['thread']['id'] = 'foreign-thread'
            write_response()
            with self.assertRaisesRegex(IntegrationError, 'SCOPE_UNKNOWN'):
                self.recovery._terminal(db, parent)

    def test_resets_at_wait_and_eight_hour_deadline_no_grant_rewrite(self):
        self.change_error('usageLimitExceeded')
        resets = time.time() + 600
        self.rpc('STDOUT_RPC', {'method': 'account/rateLimits/updated', 'params': {'rateLimits': {'primary': {'usedPercent': 100, 'resetsAt': resets}}}})
        result = self.recovery.recover(self.op, 'a')
        self.assertGreater(result['backoff_s'], 599)
        self.assertEqual(self.owner.db.execute('SELECT not_before FROM c_provider_retry').fetchone()[0], resets)

    def test_reset_after_deadline_terminal_failure(self):
        self.change_error('usageLimitExceeded')
        self.rpc('STDOUT_RPC', {'method': 'account/rateLimits/updated', 'params': {'rateLimits': {'primary': {'usedPercent': 100, 'resetsAt': time.time() + 9 * 3600}}}})
        result = self.recovery.recover(self.op, 'a')
        self.assertTrue(result['terminal'])
        self.assertEqual(result['code'], 'PROVIDER_RUN_DEADLINE_EXCEEDED')
        self.assertEqual(self.box.read_work(self.op)['delivery'], 'RETURNED')
        self.assertEqual(self.owner.db.execute('SELECT COUNT(*) FROM c_work').fetchone()[0], 1)

    def test_maximum_three_and_exact_backoffs(self):
        with self.owner.transaction(self.owner.epoch) as db:
            parent = dict(db.execute('SELECT * FROM c_work WHERE id=?', (self.op,)).fetchone())
            terminal = self.recovery._terminal(db, parent)
            db.execute('INSERT INTO c_provider_retry VALUES(?,?,?,?,?,?)', ('ancestor', 'old', self.op, 'root', 2, 0))
            root, ordinal, due, failure = self.recovery._schedule(db, parent, terminal)
            self.assertEqual((root, ordinal, failure), ('root', 3, None))
            self.assertGreater(due - time.time(), 299)
            db.execute('UPDATE c_provider_retry SET ordinal=3')
            self.assertEqual(self.recovery._schedule(db, parent, terminal)[3], 'PROVIDER_ATTEMPTS_EXHAUSTED')

    def test_three_complete_lineage_retries_then_clean_terminal(self):
        before = tuple(self.owner.db.execute("SELECT body,revision FROM controls WHERE kind='grant'").fetchone())
        for ordinal, backoff in enumerate((60, 180, 300), 1):
            result = self.recovery.recover(self.op, 'a')
            self.assertEqual(result['ordinal'], ordinal)
            self.assertGreater(result['backoff_s'], backoff - 2)
            self.assertLessEqual(result['backoff_s'], backoff)
            self.assertIsNone(self.box.next_ready('s'))
            self.op = result['id']
            self.owner.db.execute('UPDATE c_retry_wait SET not_before=0 WHERE operation=?', (self.op,))
            self.box.claim(self.op, 'a')
            self.box.update(self.op, 'a', state='PAUSED', delivery='DELIVERY_UNKNOWN', thread='thread')
            self.client_id = 'client-' + str(ordinal)
            self.trace()
        result = self.recovery.recover(self.op, 'a')
        self.assertEqual(result['code'], 'PROVIDER_ATTEMPTS_EXHAUSTED')
        self.assertEqual(self.box.read_work(self.op)['state'], 'FAILED')
        self.assertEqual(self.box.read_work(self.op)['delivery'], 'RETURNED')
        self.assertEqual(self.owner.db.execute('SELECT COUNT(*) FROM c_provider_retry').fetchone()[0], 3)
        self.assertEqual(self.owner.db.execute('SELECT COUNT(*) FROM c_work').fetchone()[0], 4)
        self.assertEqual(before, tuple(self.owner.db.execute("SELECT body,revision FROM controls WHERE kind='grant'").fetchone()))

    def test_missing_or_malformed_usage_reset_does_not_create_child(self):
        self.change_error('usageLimitExceeded')
        with self.assertRaisesRegex(IntegrationError, 'RATE_RESET_REQUIRED'):
            self.recovery.recover(self.op, 'a')
        self.rpc('STDOUT_RPC', {'method': 'account/rateLimits/updated', 'params': {'rateLimits': {'primary': {'usedPercent': 100, 'resetsAt': 'unknown'}}}})
        with self.assertRaisesRegex(IntegrationError, 'RATE_RESET_REQUIRED'):
            self.recovery.recover(self.op, 'a')
        self.assertEqual(self.owner.db.execute('SELECT COUNT(*) FROM c_work').fetchone()[0], 1)
        self.assertEqual(self.box.read_work(self.op)['delivery'], 'DELIVERY_UNKNOWN')

    def test_changed_policy_and_missing_dispatch_never_fallback(self):
        self.owner.db.execute('DELETE FROM c_run_dispatch')
        with self.assertRaisesRegex(IntegrationError, 'DISPATCH_PROOF_REQUIRED'):
            self.recovery.recover(self.op, 'a')
        grant = json.loads(self.owner.db.execute("SELECT body FROM controls WHERE kind='grant'").fetchone()[0])
        grant['scope']['settled_provider_recovery']['max_attempts'] = 4
        self.owner.db.execute("UPDATE controls SET body=? WHERE kind='grant'", (encode(grant),))
        with self.assertRaisesRegex(IntegrationError, 'GRANT_REQUIRED'):
            self.recovery.recover(self.op, 'a')

    def test_dispatch_sender_hash_time_and_immutable_receipt(self):
        self.owner.db.execute('DELETE FROM c_run_dispatch')
        self.dispatch.sender_id = 'agent'
        bind_dispatch(self.box, self.router, self.dispatch)
        self.assertEqual(self.owner.db.execute('SELECT COUNT(*) FROM c_run_dispatch').fetchone()[0], 0)
        self.dispatch.sender_id = 'human'
        self.dispatch.created_at = datetime.fromtimestamp(1, timezone.utc)
        with self.assertRaisesRegex(IntegrationError, 'NOT_PREREGISTERED'):
            bind_dispatch(self.box, self.router, self.dispatch)
        self.dispatch.created_at = datetime.now(timezone.utc)
        bind_dispatch(self.box, self.router, self.dispatch)
        bind_dispatch(self.box, self.router, self.dispatch)
        self.dispatch.id = 'different-id'
        with self.assertRaisesRegex(IntegrationError, 'CONFLICT'):
            bind_dispatch(self.box, self.router, self.dispatch)

    def test_exact_supported_human_sender_representations(self):
        for kind in ('User', 'user', 'human', 'Agent', 'agent', 'USER', 'Human', 'system', ''):
            with self.subTest(sender_type=kind):
                self.owner.db.execute('DELETE FROM c_run_dispatch')
                self.dispatch.sender_type = kind
                bind_dispatch(self.box, self.router, self.dispatch)
                self.assertEqual(self.owner.db.execute('SELECT COUNT(*) FROM c_run_dispatch').fetchone()[0], int(kind in ('User', 'user', 'human')))

    def test_no_telemetry_native_token_delta_is_still_fenced(self):
        for n in (1, 2):
            self.rpc('STDOUT_RPC', {'method': 'thread/tokenUsage/updated', 'params': {'threadId': 'thread', 'turnId': 'turn', 'tokenUsage': {'total': {'inputTokens': n, 'outputTokens': 0, 'reasoningOutputTokens': 0, 'totalTokens': n}}}})
        with self.assertRaisesRegex(IntegrationError, 'TOKEN_DELTA'):
            self.recovery.recover(self.op, 'a')

    def test_dispatch_artifact_and_exact_integer_policy(self):
        row = self.owner.db.execute('SELECT evidence_ref FROM c_run_dispatch').fetchone()
        self.owner.db.execute('UPDATE c_artifact SET body=? WHERE hash=?', (b'forged', row[0]))
        with self.assertRaisesRegex(IntegrationError, 'DISPATCH_PROOF_REQUIRED'):
            self.recovery.recover(self.op, 'a')
        grant = json.loads(self.owner.db.execute("SELECT body FROM controls WHERE kind='grant'").fetchone()[0])
        grant['scope']['settled_provider_recovery']['backoff_s'] = [60.0, 180.0, 300.0]
        self.owner.db.execute("UPDATE controls SET body=? WHERE kind='grant'", (encode(grant),))
        with self.assertRaisesRegex(IntegrationError, 'GRANT_REQUIRED'):
            self.recovery.recover(self.op, 'a')


class ProviderActualReadbackTests(unittest.TestCase):
    prepare_repo = capacity.CapacityLinuxTests.prepare_repo
    trace = capacity.CapacityTests.trace
    rpc = capacity.CapacityTests.rpc
    record = capacity.CapacityTests.record

    def setUp(self):
        ProviderTests.setUp(self)
        patch.stopall()  # actual SDK source pin, /proc and Git/index readbacks

    def test_fixed_grant_actual_readback_retry_is_idempotent(self):
        before = tuple(self.owner.db.execute("SELECT body,revision FROM controls WHERE kind='grant'").fetchone())
        result = self.recovery.recover(self.op, 'a')
        self.assertTrue(result['id'])
        self.assertEqual(result['ordinal'], 1)
        self.assertEqual(before, tuple(self.owner.db.execute("SELECT body,revision FROM controls WHERE kind='grant'").fetchone()))
        self.assertEqual(result['id'], self.recovery.recover(self.op, 'a')['id'])

    def test_source_change_before_first_child_is_fenced(self):
        self.recovery.observe(self.op, 'a')
        (self.root / 'base').write_text('source changed after proof')
        with self.assertRaisesRegex(IntegrationError, 'SOURCE_CHANGED'):
            self.recovery.recover(self.op, 'a')
        self.assertEqual(self.owner.db.execute('SELECT COUNT(*) FROM c_work').fetchone()[0], 1)
