"""WO08 synthetic boundary regressions. No model or platform traffic."""
import json
from pathlib import Path
import tempfile
import unittest

from test_integration_mailbox import Owner
from darkharness.integration.mailbox import Mailbox
from darkharness.integration.policy import ApprovalRouter


class GrantPinTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name).resolve()
        self.repo = self.ws / 'result'
        self.repo.mkdir()
        (self.repo / '.git').mkdir()
        self.owner = Owner()
        self.box = Mailbox(self.owner)
        self.owner.grant(self.ws, result_repo=str(self.repo))
        self.router = ApprovalRouter(self.owner, 'g', 's', 'r', 'run', self.ws, result_repo=str(self.repo))

    def tearDown(self):
        self.owner.db.close()
        self.tmp.cleanup()

    def test_parent_placeholder_does_not_flicker(self):
        self.assertTrue(self.router.active())
        (self.ws / '.git').mkdir()
        self.assertTrue(self.router.active())

    def test_identity_swap_fences_and_reason_is_transition_only(self):
        self.assertTrue(self.router.active())
        self.repo.rename(self.ws / 'old')
        self.repo.mkdir()
        (self.repo / '.git').mkdir()
        self.assertFalse(self.router.active())
        self.assertFalse(self.router.active())
        rows = self.owner.db.execute("SELECT body FROM c_event WHERE kind='GRANT_SCOPE_CHANGED'").fetchall()
        reasons = [json.loads(r[0])['reason'] for r in rows]
        self.assertEqual(reasons.count('REPO_IDENTITY_CHANGED'), 1)

    def test_git_directory_identity_and_scope_bindings_remain_fenced(self):
        (self.repo / '.git').rename(self.repo / 'old-git')
        (self.repo / '.git').mkdir()
        self.assertFalse(self.router.active())
        (self.repo / '.git').rmdir()
        (self.repo / 'old-git').rename(self.repo / '.git')
        self.assertTrue(self.router.active())
        original = json.loads(self.owner.db.execute("SELECT body FROM controls WHERE kind='grant'").fetchone()[0])
        for key, value in [('seats', []), ('rooms', []), ('run_id', 'different'), ('workspace', str(self.ws / 'different')), ('expires_at', '2000-01-01T00:00:00Z')]:
            grant = json.loads(json.dumps(original))
            grant['scope'][key] = value
            self.owner.db.execute("UPDATE controls SET body=? WHERE kind='grant'", (json.dumps(grant),))
            self.assertFalse(self.router.active(), key)
        self.owner.db.execute("UPDATE controls SET body=? WHERE kind='grant'", (json.dumps(original),))
        self.assertTrue(self.router.active())

    def test_revoke_and_run_end_remain_inactive(self):
        self.owner.db.execute("INSERT INTO controls VALUES('run','run',?,1)", (json.dumps({'state': 'STOPPED'}),))
        self.assertFalse(self.router.active())
        self.owner.db.execute("DELETE FROM controls WHERE kind='run'")
        grant = json.loads(self.owner.db.execute("SELECT body FROM controls WHERE kind='grant'").fetchone()[0])
        grant['revoked'] = True
        self.owner.db.execute("UPDATE controls SET body=? WHERE kind='grant'", (json.dumps(grant),))
        self.assertFalse(self.router.active())


class UnstartedTests(unittest.TestCase):
    def setUp(self):
        self.owner = Owner()
        self.box = Mailbox(self.owner)
        self.op = self.box.receive('s', 'r', 'peer', 'm', 'task')['work']
        self.box.claim(self.op, 'a')
        self.box.observe(self.op, 'a', 'ATTEMPT_PROCESS_CLEAN', {'proof': 'fixture'})

    def tearDown(self):
        self.owner.db.close()

    def rpc(self, kind, frame):
        self.box.observe(self.op, 'a', kind, {'client_id': 'client', 'payload': frame})

    def test_before_intent_three_retries_four_total_failures(self):
        for n in range(1, 5):
            attempt = 'a' if n == 1 else 'a' + str(n)
            if n > 1:
                self.owner.db.execute('UPDATE c_retry_wait SET not_before=0')
                self.box.claim(self.op, attempt)
                self.box.observe(self.op, attempt, 'ATTEMPT_PROCESS_CLEAN', {'proof': 'fixture'})
            result = self.box.release_unstarted(self.op, attempt, 'fixture')
            self.assertEqual(result['failures'], n)
            self.assertEqual(result['terminal'], n == 4)
            self.assertEqual(result['backoff_s'], 60)
            self.assertIsNone(self.box.next_ready('s'))
        self.assertEqual(self.box.read_work(self.op)['delivery'], 'RETURNED')
        child = self.box.notify_peer_blocker(self.op, 'a4', 'coordinator', 'r', 'run')
        self.assertIn('FENCED', self.box.read_work(child)['input'])
        self.assertEqual(child, self.box.notify_peer_blocker(self.op, 'a4', 'coordinator', 'r', 'run'))

    def test_consecutive_count_persists_across_closed_owner_restarts(self):
        import time
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / 'owner.sqlite')
            owner = Owner(path)
            try:
                box = Mailbox(owner)
                op = box.receive('s', 'r', 'peer', 'persistent', 'same task')['work']
                for n in range(1, 5):
                    attempt = 'attempt-' + str(n)
                    box.claim(op, attempt)
                    box.observe(op, attempt, 'ATTEMPT_PROCESS_CLEAN', {'proof': 'fixture'})
                    # First failure uses ordinary release; the other three are
                    # classified after an actual close/reopen of the owner DB.
                    if n == 1:
                        self.assertFalse(box.release_unstarted(op, attempt, 'fixture')['terminal'])
                    else:
                        owner.db.close()
                        owner = Owner(path)
                        box = Mailbox(owner)
                        self.assertEqual(box.recover(), [op])
                    row = box.read_work(op)
                    self.assertEqual(json.loads(row['result'])['failures'], n)
                    self.assertEqual((row['state'], row['delivery']),
                                     ('FAILED', 'RETURNED') if n == 4 else ('QUEUED', 'READY'))
                    wait = owner.db.execute('SELECT failures,not_before FROM c_retry_wait WHERE operation=?', (op,)).fetchone()
                    self.assertEqual(wait['failures'], n)
                    self.assertGreater(wait['not_before'], time.time() + 55)
                    self.assertIsNone(box.next_ready('s'))
                    self.assertEqual(box.recover(), [])  # no double counting
                    owner.db.execute('UPDATE c_retry_wait SET not_before=0 WHERE operation=?', (op,))
                next_op = box.receive('s', 'r', 'peer', 'next', 'next task')['work']
                self.assertEqual(box.next_ready('s')['id'], next_op)
                child = box.notify_peer_blocker(op, attempt, 'coordinator', 'r', 'run')
                self.assertEqual(box.next_ready('coordinator')['id'], child)
            finally:
                owner.db.close()

    def test_completed_work_is_never_released_or_retried_on_restart(self):
        self.box.update(self.op, 'a', state='SUCCEEDED', delivery='RETURNED', result={'completed': True})
        before = self.box.read_work(self.op)
        self.assertIsNone(self.box.release_unstarted(self.op, 'a', 'late failure'))
        self.assertEqual(self.box.recover(), [])
        self.assertEqual(self.box.read_work(self.op), before)
        self.assertEqual(self.owner.db.execute('SELECT COUNT(*) FROM c_retry_wait').fetchone()[0], 0)

    def test_effect_and_cancel_predicates_fence_restart_as_well_as_release(self):
        cases = ('TURN_ACCEPTED', 'PROCESS_STOP_UNKNOWN', 'callback', 'question',
                 'outbox', 'git', 'verification', 'cancel')
        for case in cases:
            with self.subTest(case=case):
                owner = Owner()
                try:
                    box = Mailbox(owner)
                    op = box.receive('s', 'r', 'peer', case, 'task')['work']
                    box.claim(op, 'a')
                    box.observe(op, 'a', 'ATTEMPT_PROCESS_CLEAN', {})
                    if case in ('TURN_ACCEPTED', 'PROCESS_STOP_UNKNOWN'):
                        box.observe(op, 'a', case, {})
                    elif case == 'callback':
                        box.callback(op, 'a', 'callback', {})
                    elif case == 'question':
                        box.question('q', op, 'peer', {})
                    elif case == 'outbox':
                        box.prepare_send('out', op, {'content': 'fixture'})
                    elif case in ('git', 'verification'):
                        table = 'c_git_effect' if case == 'git' else 'c_verification_effect'
                        owner.db.execute(f'CREATE TABLE {table}(operation TEXT,attempt TEXT)')
                        owner.db.execute(f'INSERT INTO {table} VALUES(?,?)', (op, 'a'))
                    else:
                        box.control('cancel', op, 'a', 'cancel', {})
                    self.assertIsNone(box.release_unstarted(op, 'a', 'fixture'))
                    box.recover()
                    self.assertEqual(box.read_work(op)['delivery'], 'DELIVERY_UNKNOWN')
                    self.assertEqual(owner.db.execute('SELECT COUNT(*) FROM c_retry_wait').fetchone()[0], 0)
                finally:
                    owner.db.close()

    def test_definitive_same_rpc_error_only(self):
        self.box.observe(self.op, 'a', 'TURN_START_INTENT', {})
        self.rpc('STDIN_RPC', {'id': 1, 'method': 'turn/start'})
        self.rpc('STDOUT_RPC', {'id': 2, 'error': {'code': -1, 'message': 'definitive error'}})
        self.assertIsNone(self.box.release_unstarted(self.op, 'a', 'error'))
        self.owner.db.execute("DELETE FROM c_event WHERE kind='STDOUT_RPC'")
        self.rpc('STDOUT_RPC', {'id': 1, 'error': {'code': -1, 'message': 'definitive error'}})
        self.assertIsNotNone(self.box.release_unstarted(self.op, 'a', 'error'))

    def test_error_before_request_or_malformed_error_never_proves_rejection(self):
        self.rpc('STDOUT_RPC', {'id': 1, 'error': {'code': -1, 'message': 'old reply'}})
        self.box.observe(self.op, 'a', 'TURN_START_INTENT', {})
        self.rpc('STDIN_RPC', {'id': 1, 'method': 'turn/start'})
        self.assertIsNone(self.box.release_unstarted(self.op, 'a', 'old reply'))
        self.owner.db.execute("DELETE FROM c_event WHERE kind='STDOUT_RPC'")
        self.rpc('STDOUT_RPC', {'id': 1, 'error': {}})
        self.assertIsNone(self.box.release_unstarted(self.op, 'a', 'malformed'))

    def test_sent_without_reply_and_accepted_remain_fenced(self):
        self.box.observe(self.op, 'a', 'TURN_START_INTENT', {})
        self.rpc('STDIN_RPC', {'id': 1, 'method': 'turn/start'})
        self.assertIsNone(self.box.release_unstarted(self.op, 'a', 'lost'))
        self.box.update(self.op, 'a', delivery='STARTED')
        self.assertIsNone(self.box.release_unstarted(self.op, 'a', 'accepted'))

    def test_effect_rows_and_cleanup_unknown_exclude_release(self):
        self.box.callback(self.op, 'a', 'callback', {})
        self.assertIsNone(self.box.release_unstarted(self.op, 'a', 'callback'))
        self.owner.db.execute('DELETE FROM c_callback')
        self.box.prepare_send('out', self.op, {'content': 'fixture'})
        self.assertIsNone(self.box.release_unstarted(self.op, 'a', 'outbox'))
        self.owner.db.execute('DELETE FROM c_outbox')
        self.box.observe(self.op, 'a', 'PROCESS_STOP_UNKNOWN', {})
        self.assertIsNone(self.box.release_unstarted(self.op, 'a', 'cleanup'))

    def test_restart_uses_same_proof_not_restart_as_cessation(self):
        self.box.recover()
        self.assertEqual(self.box.read_work(self.op)['delivery'], 'READY')
        self.owner.db.execute('UPDATE c_retry_wait SET not_before=0')
        self.box.claim(self.op, 'b')
        self.box.recover()
        self.assertEqual(self.box.read_work(self.op)['delivery'], 'DELIVERY_UNKNOWN')


    def test_historical_accepted_plus_three_acks_is_never_unstarted(self):
        self.box.observe(self.op, 'a', 'TURN_ACCEPTED', {'turn': 'accepted'})
        self.box.update(self.op, 'a', delivery='STARTED')
        for n in range(3):
            key = 'ack' + str(n)
            self.box.prepare_send(key, self.op, {'content': 'business ' + str(n)})
            self.box.sent(key, {'id': key})
        self.assertIsNone(self.box.release_unstarted(self.op, 'a', 'GRANT_INACTIVE'))
        self.assertEqual(self.box.read_work(self.op)['delivery'], 'STARTED')
