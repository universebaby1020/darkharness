"""SDK4 serialization -> real GuardedTools -> receipt fences. No model/Band/Docker."""
import asyncio
from importlib.util import find_spec
import json
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_integration_verification as fixtures
from darkharness.integration.artifacts import SecretGuard, render_mandate
from darkharness.integration.mailbox import digest, encode
from darkharness.integration.git_broker import LocalGitBroker
from darkharness.integration.verification_bridge import VerificationBridge

HAS_SDK = find_spec('band') is not None

@unittest.skipUnless(sys.platform == 'linux' and HAS_SDK, 'Linux + installed SDK4 required')
class ReceiptFacadeTests(unittest.TestCase):
    def git(self, repo, *args):
        value = fixtures.VerificationTests.git(self, repo, *args)
        if args and args[0] == 'init':
            # DrvFS without metadata reports executable regular files. Record
            # the observed mode rather than Git's auto-disabled filemode default.
            fixtures.VerificationTests.git(self, repo, 'config', 'core.filemode', 'true')
        return value

    commit = fixtures.VerificationTests.commit
    publish = fixtures.VerificationTests.publish

    def setUp(self):
        fixtures.VerificationTests.setUp(self)
        from darkharness.integration.protected_tools import GuardedTools
        self.guard = SecretGuard([('fixture', 'NEVER_PRESENT_FIXTURE')], source_hash='fixture')
        self.bridge = VerificationBridge(self.box, self.router, self.guard, report_parsers=self.broker.parsers)
        adapter = SimpleNamespace(mailbox=self.box, router=self.router, guard=self.guard,
            git_broker=LocalGitBroker(self.box, self.router, 'fixture', 'fixture@actors.invalid', self.root),
            verification=self.bridge, _verification_effect=None)
        self.tools = GuardedTools(SimpleNamespace(), adapter, self.op, 'a')
        self.tools.call_id = 'snapshot-call'
        from receipt_sdk_boundary import sdk_tool_response
        response = asyncio.run(sdk_tool_response(self.tools, 'dh_review_snapshot',
            {'cwd': str(self.repo), 'revision': self.revision, 'name': 'sdk-returned'}, 'snapshot-call'))
        self.assertTrue(response['success'])
        self.serialized = response['contentItems'][0]['text']
        self.assertIsInstance(self.serialized, str)
        self.assertTrue(self.serialized.startswith('{'))
        self.receipt = json.loads(self.serialized)
        self.tools.call_id = 'verify-call'
        self.effect = digest(encode([self.op, 'a', self.tools.call_id, 'dh_verify']).encode())
        self.args = {'check_id': 'check', 'revision': self.revision, 'checkout_receipt': self.serialized}

    def tearDown(self):
        self.bridge.broker.close()
        fixtures.VerificationTests.tearDown(self)

    def verify(self, receipt=None):
        args = dict(self.args)
        if receipt is not None:
            args['checkout_receipt'] = receipt
        return asyncio.run(self.tools.execute_tool_call_structured('dh_verify', args))

    def test_serialized_facade_roundtrip(self):
        outcome = self.verify()
        self.assertTrue(outcome.ok, outcome.value)
        result = asyncio.run(self.bridge.broker.wait(self.op, 'a', self.effect))
        self.assertEqual(result['state'], 'SUCCEEDED')
        child = self.bridge.complete(self.op, 'a', self.effect)
        self.assertIsNotNone(child)
        self.assertEqual(self.bridge.complete(self.op, 'a', self.effect), child)
        self.box.claim(child, 'child-a')
        from darkharness.integration.protected_tools import GuardedTools
        reader = GuardedTools(SimpleNamespace(), self.tools.adapter, child, 'child-a')
        page = asyncio.run(reader.execute_tool_call_structured('dh_verification_read',
            {'effect_id': self.effect, 'artifact': 'report.json', 'offset': 0, 'limit': 16384}))
        self.assertTrue(page.ok, page.value)
        self.assertIn('accepted', page.value['text'])
        self.assertEqual(self.owner.db.execute('SELECT count(*) FROM c_verification_continuation').fetchone()[0], 1)

    def test_preintent_contract_and_same_input_retry(self):
        # Typed local validation fails once; ledger must remain empty.
        with patch.object(self.bridge.broker, '_configuration', side_effect=fixtures.IntegrationError('FIXTURE_PREFLIGHT')):
            denied = self.verify(self.receipt)
        self.assertFalse(denied.ok)
        self.assertEqual(denied.value['effect_phase'], 'NOT_STARTED')
        self.assertTrue(denied.value['same_input_safe_retry'])
        self.assertIn('same input', denied.value['retry_message'])
        self.assertEqual(self.owner.db.execute('SELECT count(*) FROM c_verification_effect').fetchone()[0], 0)
        self.assertTrue(self.verify(self.receipt).ok)
        self.assertEqual(asyncio.run(self.bridge.broker.wait(self.op, 'a', self.effect))['state'], 'SUCCEEDED')

    def test_invalid_receipts_remain_denied(self):
        cases = ['{', '[]', 'null', '1', json.dumps(self.serialized),
                 json.dumps({**self.receipt, 'path': str(self.repo)}),
                 self.serialized[:-1] + ',"state":"SNAPSHOT"}',
                 self.serialized[:-1] + ',"extra":NaN}']
        for value in cases:
            with self.subTest(value=value):
                denied = self.verify(value)
                self.assertFalse(denied.ok)
                self.assertEqual(denied.value['effect_phase'], 'NOT_STARTED')
                self.assertTrue(denied.value['same_input_safe_retry'])
        self.assertEqual(self.owner.db.execute('SELECT count(*) FROM c_verification_effect').fetchone()[0], 0)

    def test_ambiguous_unacked_and_wrong_run_denied(self):
        with self.owner.transaction(self.owner.epoch) as db:
            row = db.execute('SELECT * FROM c_git_effect WHERE receipt=?', (encode(self.receipt),)).fetchone()
            columns = ','.join(row.keys())
            values = list(row); values[0] = 'duplicate'
            db.execute('INSERT INTO c_git_effect (' + columns + ') VALUES (' + ','.join('?' for _ in values) + ')', values)
        self.assertFalse(self.verify().ok)
        with self.owner.transaction(self.owner.epoch) as db:
            db.execute("DELETE FROM c_git_effect WHERE id='duplicate'")
            db.execute("UPDATE c_git_effect SET state='UNKNOWN' WHERE id=?", (row['id'],))
        self.assertFalse(self.verify().ok)
        with self.owner.transaction(self.owner.epoch) as db:
            db.execute("UPDATE c_git_effect SET state='ACKED' WHERE id=?", (row['id'],))
            origins = db.execute("SELECT rowid AS event_row,body FROM c_event WHERE kind='GIT_RUN_ORIGIN'").fetchall()
            for origin in origins:
                body = json.loads(origin['body']); body['run_id'] = 'other-run'
                db.execute('UPDATE c_event SET body=? WHERE rowid=?', (encode(body), origin['event_row']))
        denied = self.verify()
        self.assertFalse(denied.ok)
        self.assertEqual(denied.value['error'], 'RECEIPT_RUN_MISMATCH')

    def test_after_intent_failure_and_existing_unknown_not_safe(self):
        import threading
        original_start = threading.Thread.start
        def fail_checker(thread):
            if thread._target == self.bridge.broker._run:
                raise RuntimeError('fixture')
            return original_start(thread)
        with patch('darkharness.integration.verification.threading.Thread.start', new=fail_checker):
            denied = self.verify(self.receipt)
        self.assertFalse(denied.ok)
        self.assertFalse(denied.value['same_input_safe_retry'])
        self.assertNotEqual(denied.value['effect_phase'], 'NOT_STARTED')
        self.assertEqual(self.bridge.broker.status(self.op, 'a', self.effect)['state'], 'UNKNOWN')
        self.tools.call_id = 'fresh-id'
        denied = self.verify('{')  # Even a preflight error must not hide old UNKNOWN.
        self.assertFalse(denied.value['same_input_safe_retry'])
        self.assertEqual(denied.value['effect_phase'], 'UNKNOWN')

    def test_thread_started_then_raised_retains_owned_cleanup(self):
        import threading
        original_start = threading.Thread.start
        def uncertain_checker_start(thread):
            value = original_start(thread)
            if thread._target == self.bridge.broker._run:
                raise RuntimeError('fixture after actual start')
            return value
        with patch('darkharness.integration.verification.threading.Thread.start', new=uncertain_checker_start):
            denied = self.verify(self.receipt)
        self.assertFalse(denied.ok)
        self.assertFalse(denied.value['same_input_safe_retry'])
        self.assertNotEqual(denied.value['effect_phase'], 'NOT_STARTED')
        self.bridge.broker.close()
        job = self.bridge.broker.jobs[self.effect]
        self.assertIsNotNone(job.thread)
        self.assertFalse(job.thread.is_alive())
        self.assertTrue(job.done.is_set())

    def test_projection_failure_after_started_not_safe(self):
        with patch.object(self.bridge.broker, 'public_result', side_effect=fixtures.IntegrationError('FIXTURE_PROJECTION')):
            denied = self.verify(self.receipt)
        self.assertFalse(denied.ok)
        self.assertFalse(denied.value['same_input_safe_retry'])
        self.assertNotEqual(denied.value['effect_phase'], 'NOT_STARTED')

class FailureLedgerTests(unittest.TestCase):
    def setUp(self):
        from pathlib import Path
        from test_integration_mailbox import Owner
        from darkharness.integration.mailbox import Mailbox
        from darkharness.integration.policy import ApprovalRouter
        from darkharness.integration.verification import VerificationBroker
        self.owner = Owner(); self.box = Mailbox(self.owner)
        root = Path(__file__).resolve().parent
        self.owner.grant(root, seats=['s', 'peer'])
        self.router = ApprovalRouter(self.owner, 'g', 's', 'r', 'run', root)
        self.op = self.box.receive('s', 'r', 'controller', 'fixture', 'task')['work']
        self.box.claim(self.op, 'a')
        self.broker = VerificationBroker(self.box, self.router, report_parsers={})
        self.addCleanup(self.owner.db.close)
        self.addCleanup(self.broker.close)

    def contract(self):
        return self.broker.failure_contract(self.op, 'a', 'fresh')

    def test_tool_blocked_audit_is_not_execution(self):
        self.box.observe(self.op, 'a', 'TOOL_BLOCKED', {'code': 'ANY_TYPED_PREFLIGHT'})
        value = self.contract()
        self.assertEqual(value['effect_phase'], 'NOT_STARTED')
        self.assertTrue(value['same_input_safe_retry'])

    def test_unreadable_ledger_and_inmemory_job_are_not_safe(self):
        from darkharness.integration.verification import _Job
        self.broker.jobs['fresh'] = _Job()
        self.assertFalse(self.contract()['same_input_safe_retry'])
        self.broker.jobs.clear()
        self.owner.db.execute('DROP TABLE c_verification_effect')
        self.assertEqual(self.contract()['effect_phase'], 'UNKNOWN')
        self.assertFalse(self.contract()['same_input_safe_retry'])

    def test_other_owned_pending_job_not_reclassified(self):
        self.owner.db.execute("INSERT INTO c_verification_effect VALUES('other','peer-operation','peer-attempt','run','{}','{}','RUNNING','other-owner','unused',NULL,NULL)")
        self.assertFalse(self.contract()['same_input_safe_retry'])
        self.assertEqual(self.owner.db.execute("SELECT state FROM c_verification_effect WHERE id='other'").fetchone()[0], 'RUNNING')

    def test_existing_unknown_and_run_unknown_remain_fenced(self):
        self.owner.db.execute("INSERT INTO c_verification_effect VALUES('old',?,'a','run','{}','{}','UNKNOWN','old-owner','unused',NULL,NULL)", (self.op,))
        self.assertFalse(self.contract()['same_input_safe_retry'])
        self.owner.db.execute('DELETE FROM c_verification_effect')
        self.owner.db.execute("INSERT INTO controls VALUES('run','run',?,1)", (encode({'state': 'UNKNOWN'}),))
        self.assertEqual(self.contract()['effect_phase'], 'UNKNOWN')
        self.assertFalse(self.contract()['same_input_safe_retry'])

class ActualEvidenceTests(unittest.TestCase):
    def test_original_rpc_strings_select_unique_canonical_ack(self):
        import sqlite3
        from pathlib import Path
        from darkharness.integration.verification import VerificationBroker
        path = Path(__file__).resolve().parents[1] / 'evidence/wo03-context/RECEIPT_ACTUAL_EVIDENCE.json'
        if not path.exists():
            self.skipTest('Main redacted original evidence not present in this checkout')
        evidence = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(evidence['verification_effect_count'], 0)
        db = sqlite3.connect(':memory:'); db.row_factory = sqlite3.Row
        self.addCleanup(db.close)
        db.execute('CREATE TABLE c_git_effect(id TEXT, state TEXT, receipt TEXT)')
        for snap in evidence['canonical_snapshots']:
            db.execute('INSERT INTO c_git_effect VALUES(?,?,?)', (snap['id'], snap['state'], encode(snap['receipt'])))
        def receipts(value):
            if isinstance(value, dict):
                if isinstance(value.get('checkout_receipt'), str):
                    yield value['checkout_receipt']
                for child in value.values():
                    yield from receipts(child)
            elif isinstance(value, list):
                for child in value:
                    yield from receipts(child)
        comparisons = {x['event_seq']: x for x in evidence['verify_argument_comparisons']}
        for event in evidence['selected_rpc_events']:
            with self.subTest(seq=event['seq']):
                comparison = comparisons[event['seq']]
                self.assertEqual(comparison['input_type'], 'str')
                self.assertEqual(comparison['parsed_matches'], 1)
                self.assertEqual(comparison['canonical_json_text_matches'], 1)
                args = set(receipts(event['body']))
                self.assertEqual(len(args), 1)
                selector = args.pop()
                self.assertEqual(json.loads(selector), evidence['canonical_snapshots'][0]['receipt'])
                self.assertEqual(VerificationBroker._effect_selector(db, selector)['id'], evidence['canonical_snapshots'][0]['id'])

class MandateContractTests(unittest.TestCase):
    def test_generic_controller_pin_and_complete_handoff(self):
        for role in ('coordinator', 'builder', 'reviewer'):
            mandate = render_mandate('seat', role, 'Codex', 'test-model', 'high')
            self.assertIn('Factory pins are controller information', mandate)
            self.assertIn('not seat verification responsibility', mandate)
            self.assertIn('no empty placeholders or literal undefined', mandate)
