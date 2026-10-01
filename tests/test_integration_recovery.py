"""Controller evidence-bearing same-run continuation, isolated Git/source only."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from test_integration_mailbox import Owner
from darkharness.integration.mailbox import Mailbox, IntegrationError
from darkharness.integration.policy import ApprovalRouter
from darkharness.integration.git_broker import LocalGitBroker
from darkharness.integration.recovery import Recovery


@unittest.skipUnless(os.name == 'posix', 'Linux evidence readback')
class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name).resolve()
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        (self.root / 'base').write_text('seat source')
        subprocess.run(['git', '-C', str(self.root), 'add', 'base'], check=True)
        subprocess.run(['git', '-C', str(self.root), '-c', 'user.name=f', '-c', 'user.email=f@actors.invalid', 'commit', '-qm', 'base'], check=True)
        self.owner = Owner()
        self.box = Mailbox(self.owner)
        self.op = self.box.receive('s', 'r', 'peer', 'm', 'original full task', envelope={'participants_msg': 'roster', 'metadata': {'x': 'kept'}})['work']
        self.box.claim(self.op, 'a')
        self.box.update(self.op, 'a', state='FAILED', delivery='RETURNED', thread='original-thread')
        self.owner.grant(self.root, continuation={'operations': [self.op]}, local_git_write={'directories': ['.']})
        self.router = ApprovalRouter(self.owner, 'g', 's', 'r', 'run', self.root)
        self.broker = LocalGitBroker(self.box, self.router, 'seat', 'seat@actors.invalid', self.root)
        self.recovery = Recovery(self.box, self.router, self.broker)

    def tearDown(self):
        self.owner.db.close()
        self.td.cleanup()

    def test_returned_original_input_thread_and_idempotent_continuation(self):
        receipt = self.recovery.observe(self.op, 'a')
        child = self.recovery.resume(self.op, 'a', receipt['evidence_id'])
        self.assertEqual(child, self.recovery.resume(self.op, 'a', receipt['evidence_id']))
        work = self.box.read_work(child['id'])
        body = json.loads(work['input'])
        self.assertEqual(body['content'], 'original full task')
        self.assertEqual(body['metadata'], {'x': 'kept'})
        self.assertEqual(body['parent_operation'], self.op)
        self.assertEqual(body['parent_attempt'], 'a')
        self.assertEqual(work['thread'], 'original-thread')
        self.assertEqual(work['delivery'], 'READY')
        self.assertEqual(self.box.read_work(self.op)['state'], 'FAILED')
        self.assertEqual(self.owner.db.execute('SELECT COUNT(*) FROM c_inbox').fetchone()[0], 1)

    def test_source_changed_forged_evidence_and_unknown_rejected(self):
        receipt = self.recovery.observe(self.op, 'a')
        (self.root / 'base').write_text('changed')
        with self.assertRaisesRegex(IntegrationError, 'SOURCE_CHANGED'):
            self.recovery.resume(self.op, 'a', receipt['evidence_id'])
        for proof in ['missing', {'safe': True}, True]:
            with self.assertRaises(IntegrationError):
                self.recovery.resume(self.op, 'a', proof)
        self.owner.db.execute("UPDATE c_work SET delivery='DELIVERY_UNKNOWN',state='PAUSED' WHERE id=?", (self.op,))
        with self.assertRaisesRegex(IntegrationError, 'UNKNOWN'):
            self.recovery.observe(self.op, 'a')

    def test_outbox_git_unknown_revoke_stop_and_stale_attempt_fences(self):
        self.box.prepare_send('send', self.op, {'content': 'reply'})
        with self.assertRaisesRegex(IntegrationError, 'UNKNOWN'):
            self.recovery.observe(self.op, 'a')
        self.box.sent('send', {'id': 'actual-ack'})
        self.owner.db.execute("INSERT INTO c_git_effect VALUES('uncertain',?, 'a','{}','UNKNOWN',NULL,NULL)", (self.op,))
        with self.assertRaisesRegex(IntegrationError, 'UNKNOWN'):
            self.recovery.observe(self.op, 'a')
        self.owner.db.execute("DELETE FROM c_git_effect WHERE id='uncertain'")
        with self.assertRaisesRegex(IntegrationError, 'ATTEMPT'):
            self.recovery.observe(self.op, 'stale')
        self.owner.db.execute("INSERT INTO controls VALUES('run','run',?,1)", (json.dumps({'state': 'STOPPED'}),))
        with self.assertRaisesRegex(IntegrationError, 'GRANT'):
            self.recovery.observe(self.op, 'a')
        self.owner.db.execute("DELETE FROM controls WHERE kind='run'")
        self.owner.db.execute("UPDATE controls SET body=? WHERE kind='grant'", (json.dumps({'revoked': True}),))
        with self.assertRaisesRegex(IntegrationError, 'GRANT'):
            self.recovery.observe(self.op, 'a')

    def test_yielded_provided_peer_answer_preserved(self):
        self.owner.db.execute("UPDATE c_work SET delivery='YIELDED',state='PAUSED' WHERE id=?", (self.op,))
        self.box.question('q', self.op, 'peer', {'question': 'byte count'})
        self.owner.db.execute("UPDATE c_question SET answer='21 bytes' WHERE id='q'")
        receipt = self.recovery.observe(self.op, 'a')
        child = self.recovery.resume(self.op, 'a', receipt['evidence_id'])
        body = json.loads(self.box.read_work(child['id'])['input'])
        self.assertEqual(body['content'], 'original full task')
        self.assertEqual(body['peer_answers'][0]['answer'], '21 bytes')
        self.assertEqual(body['peer_answers'][0]['context']['question'], 'byte count')

    def test_known_committed_effect_readback_not_replayed(self):
        self.owner.db.execute("UPDATE c_work SET delivery='STARTED',state='RUNNING' WHERE id=?", (self.op,))
        head = subprocess.check_output(['git', '-C', str(self.root), 'rev-parse', 'HEAD']).decode().strip()
        (self.root / 'smoke.txt').write_text('actual seat file')
        committed = self.broker.commit(self.op, 'a', 'effect', cwd=str(self.root), paths=['smoke.txt'], message='seat commit', expected_head=head)
        self.box.update(self.op, 'a', state='FAILED', delivery='RETURNED')
        receipt = self.recovery.observe(self.op, 'a')
        self.assertEqual(receipt['effect'], 'KNOWN_COMMITTED_EFFECT')
        self.recovery.resume(self.op, 'a', receipt['evidence_id'])
        current = subprocess.check_output(['git', '-C', str(self.root), 'rev-parse', 'HEAD']).decode().strip()
        self.assertEqual(current, committed['commit'])
        self.assertEqual(subprocess.check_output(['git', '-C', str(self.root), 'rev-list', '--count', 'HEAD']).decode().strip(), '2')
