"""Synthetic native error order and durable blocker identity. No model/Band calls."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from darkharness.integration.mailbox import Mailbox, IntegrationError, encode
from test_integration_mailbox import Owner
import test_wo08_provider as provider


class RetryFrameTests(unittest.TestCase):
    def setUp(self):
        self.f=provider.ProviderTests('test_policy_retry_zero_effect_and_persistent_backoff')
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.f.change_error({'httpConnectionFailed':{'httpStatusCode':503}})

    def extra_error(self, **changes):
        db=self.f.owner.db
        row=next(r for r in db.execute('SELECT * FROM c_event') if json.loads(r['body']).get('data',{}).get('payload',{}).get('method')=='error')
        body=json.loads(row['body']);body['data']['payload']['params'].update(willRetry=True,**changes)
        # Reserve a gap before the terminal error without changing any body.
        db.execute('UPDATE c_event SET seq=seq+1000 WHERE seq>=?',(row['seq'],))
        db.execute('INSERT INTO c_event(seq,operation,kind,body,at) VALUES(?,?,?,?,?)',
                   (row['seq'],row['operation'],row['kind'],encode(body),row['at']))

    def test_prior_retryable_frame_allowed_terminal_exact(self):
        self.extra_error()
        result=self.f.recovery.recover(self.f.op,'a')
        self.assertEqual(result['ordinal'],1)

    def test_prior_other_turn_still_fenced(self):
        self.extra_error(turnId='other')
        with self.assertRaises(IntegrationError):self.f.recovery.recover(self.f.op,'a')
        self.assertEqual(self.f.box.read_work(self.f.op)['delivery'],'DELIVERY_UNKNOWN')

    def test_duplicate_terminal_frame_still_fenced(self):
        self.extra_error()
        db=self.f.owner.db
        row=next(r for r in db.execute('SELECT * FROM c_event') if json.loads(r['body']).get('data',{}).get('payload',{}).get('params',{}).get('willRetry') is True)
        body=json.loads(row['body']);body['data']['payload']['params']['willRetry']=False
        db.execute('UPDATE c_event SET body=? WHERE seq=?',(encode(body),row['seq']))
        with self.assertRaises(IntegrationError):self.f.recovery.recover(self.f.op,'a')

    def test_policy_code_remains_rejected_even_with_prior_retryable(self):
        self.f.change_error('cyberPolicy');self.extra_error()
        with self.assertRaisesRegex(IntegrationError,'NOT_ALLOWED_PROVIDER_ERROR'):
            self.f.recovery.recover(self.f.op,'a')
        self.assertEqual(self.f.owner.db.execute('SELECT COUNT(*) FROM c_provider_retry').fetchone()[0],0)

    def test_retryable_frame_after_terminal_is_not_a_prefix(self):
        self.extra_error()
        db=self.f.owner.db
        rows=list(db.execute('SELECT * FROM c_event ORDER BY seq'))
        prefix=next(r for r in rows if json.loads(r['body']).get('data',{}).get('payload',{}).get('params',{}).get('willRetry') is True)
        final=next(r for r in rows if json.loads(r['body']).get('data',{}).get('payload',{}).get('params',{}).get('willRetry') is False)
        # Swap the two frame bodies while preserving the rest of the native chain.
        db.execute('UPDATE c_event SET body=? WHERE seq=?',(final['body'],prefix['seq']))
        db.execute('UPDATE c_event SET body=? WHERE seq=?',(prefix['body'],final['seq']))
        with self.assertRaises(IntegrationError):self.f.recovery.recover(self.f.op,'a')


class BlockerTests(unittest.TestCase):
    def test_launch_restart_fourth_failure_delivers_one_blocker(self):
        from darkharness.integration.launch import SeatManager
        with TemporaryDirectory() as td:
            path=str(Path(td)/'owner.sqlite');owner=Owner(path);box=Mailbox(owner)
            op=box.receive('builder','room','human','m','fixture')['work']
            for n in range(1,5):
                box.claim(op,str(n));box.observe(op,str(n),'ATTEMPT_PROCESS_CLEAN',{})
                if n<4:
                    box.release_unstarted(op,str(n),'fixture')
                    owner.db.execute('UPDATE c_retry_wait SET not_before=0')
            owner.db.close();owner=Owner(path)
            try:
                manager=SeatManager(owner,'unused')  # constructor performs actual restart recovery
                self.assertEqual(manager.mailbox.read_work(op)['state'],'FAILED')
                self.assertEqual(json.loads(manager.mailbox.read_work(op)['result'])['failures'],4)
                adapter=SimpleNamespace(alias='builder',_notify_blocker=lambda operation,attempt: manager.mailbox.notify_peer_blocker(operation,attempt,'coordinator','room','run'))
                manager.notify_startup_blockers(adapter)
                manager.notify_startup_blockers(adapter)
                ready=manager.mailbox.next_ready('coordinator')
                self.assertTrue(json.loads(ready['input'])['internal_evidence'])
                self.assertEqual(owner.db.execute("SELECT COUNT(*) FROM c_event WHERE kind='COORDINATOR_BLOCKER_QUEUED'").fetchone()[0],1)
            finally:owner.db.close()

    def test_repeated_notification_has_one_durable_event_and_one_work(self):
        owner=Owner();self.addCleanup(owner.db.close);box=Mailbox(owner)
        op=box.receive('builder','room','human','m','fixture')['work'];box.claim(op,'a')
        box.update(op,'a',state='FAILED',delivery='RETURNED',result={'replay':'FENCED'})
        ids=[box.notify_peer_blocker(op,'a','coordinator','room','run') for _ in range(3)]
        self.assertEqual(len(set(ids)),1)
        self.assertEqual(owner.db.execute("SELECT COUNT(*) FROM c_work WHERE seat='coordinator'").fetchone()[0],1)
        self.assertEqual(owner.db.execute("SELECT COUNT(*) FROM c_event WHERE kind='COORDINATOR_BLOCKER_QUEUED'").fetchone()[0],1)
