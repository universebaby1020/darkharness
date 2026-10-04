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
        self.assertEqual(denied.value['effect_phase'], 'NOT_STARTED')
        self.assertTrue(denied.value['same_input_safe_retry'])
        self.assertEqual(denied.value['same_input_expected_error'], 'RECEIPT_RUN_MISMATCH')
        self.assertTrue(denied.value['input_correction_required'])

    def test_not_granted_check_is_safe_from_duplicates_not_valid_input(self):
        self.args['check_id'] = 'not-granted'
        denied = self.verify()
        self.assertFalse(denied.ok)
        self.assertEqual(denied.value['error'], 'CHECK_NOT_GRANTED')
        self.assertEqual(denied.value['effect_phase'], 'NOT_STARTED')
        self.assertTrue(denied.value['same_input_safe_retry'])
        self.assertEqual(denied.value['same_input_expected_error'], 'CHECK_NOT_GRANTED')
        self.assertTrue(denied.value['input_correction_required'])
        self.assertEqual(self.owner.db.execute('SELECT count(*) FROM c_verification_effect').fetchone()[0], 0)

    def test_after_intent_failure_and_existing_unknown_not_safe(self):
        import threading
        original_start = threading.Thread.start
        def fail_checker(thread):
            if thread._target == self.bridge.broker._run:
                raise RuntimeError('fixture')
            return original_start(thread)
        with patch('darkharness.integration.verification.threading.Thread.start', new=fail_checker):
            denied = self.verify(self.receipt)
        # WO08: a thread that demonstrably never started is a known failure,
        # not uncertainty about a checker process or declared external effects.
        self.assertTrue(denied.ok)
        self.assertEqual(denied.value['state'], 'FAILED')
        status = self.bridge.broker.status(self.op, 'a', self.effect)
        self.assertEqual(status['result']['external_execution'], 'NOT_EXECUTED')
        self.assertIsNotNone(self.bridge.complete(self.op, 'a', self.effect))
        # A distinct genuinely unresolved historical effect must still override
        # any new request's preflight diagnosis (run-wide fence is unchanged).
        with self.owner.transaction(self.owner.epoch) as db:
            db.execute("UPDATE c_verification_effect SET state='UNKNOWN' WHERE id=?", (self.effect,))
        self.tools.call_id = 'fresh-id'
        denied = self.verify('{')  # Even a preflight error must not hide old UNKNOWN.
        self.assertFalse(denied.value['same_input_safe_retry'])
        self.assertEqual(denied.value['effect_phase'], 'UNKNOWN')

    def test_thread_started_then_raised_retains_owned_cleanup(self):
        import threading
        real_start = threading.Thread.start
        rapid = threading.Thread(target=lambda: None)
        def original_start(thread):
            value = real_start(thread)
            if thread is rapid:
                # Force Thread.run's cleanup before the wrapper resumes; the
                # global start patch also sees unrelated short-lived threads.
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive())
                self.assertFalse(hasattr(thread, '_target'))
            return value
        def uncertain_checker_start(thread):
            # Thread.run deletes _target on completion, even before start()
            # returns. Capture ownership before starting any real thread.
            is_checker = thread._target == self.bridge.broker._run
            value = original_start(thread)
            if is_checker:
                raise RuntimeError('fixture after actual start')
            return value
        with patch('darkharness.integration.verification.threading.Thread.start', new=uncertain_checker_start):
            rapid.start()
            denied = self.verify(self.receipt)
        self.assertFalse(denied.ok)
        self.assertEqual(denied.value['error'], 'VERIFICATION_START_UNKNOWN')
        self.assertFalse(denied.value['same_input_safe_retry'])
        self.assertNotEqual(denied.value['effect_phase'], 'NOT_STARTED')
        self.assertEqual(denied.value['retry_diagnostic'], 'OWN_EFFECT_EXISTS')
        job = self.bridge.broker.jobs[self.effect]
        self.assertIsNotNone(job.thread)
        self.assertIsNotNone(job.thread.ident)
        self.assertTrue(job.cancel.is_set())
        with patch.object(job.thread, 'join', wraps=job.thread.join) as joined:
            self.bridge.broker.close()
        joined.assert_called_once_with(timeout=5)
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

class FactoryRepairTests(unittest.TestCase):
    """Model-free component contracts using the same ledger fixture."""
    setUp = FailureLedgerTests.setUp
    contract = FailureLedgerTests.contract
    def test_deterministic_denial_requires_correction_not_reconciliation(self):
        for code in ('CHECK_NOT_GRANTED', 'RECEIPT_RUN_MISMATCH'):
            with self.subTest(code=code):
                with patch.object(self.broker, '_start', side_effect=fixtures.IntegrationError(code)):
                    with self.assertRaisesRegex(fixtures.IntegrationError, code):
                        self.broker.start(self.op, 'a', 'fresh', check_id='check', checkout_receipt='receipt', revision='0' * 40)
                value = self.contract()
                self.assertEqual(value['effect_phase'], 'NOT_STARTED')
                self.assertTrue(value['same_input_safe_retry'])  # duplicate-effect safety unchanged
                self.assertTrue(value['input_correction_required'])
                self.assertEqual(value['same_input_expected_error'], code)
                self.assertIn('correct', value['retry_message'].lower())
                self.assertEqual(self.owner.db.execute('SELECT count(*) FROM c_verification_effect').fetchone()[0], 0)

    def test_deterministic_configuration_denials_require_correction(self):
        for code in ('CHECKER_PIN_MISMATCH', 'FIXED_ARGV_REQUIRED', 'OUTPUT_LAYOUT_INVALID',
                     'DECLARED_REPORT_CONTRACT_REQUIRED', 'DECLARED_CHECKER_EFFECTS_REQUIRED',
                     'LIMIT_SOURCE_REQUIRED', 'CLEAN_ENVIRONMENT_REQUIRED'):
            with self.subTest(code=code):
                with patch.object(self.broker, '_start', side_effect=fixtures.IntegrationError(code)):
                    with self.assertRaisesRegex(fixtures.IntegrationError, code):
                        self.broker.start(self.op, 'a', 'fresh', check_id='check', checkout_receipt='receipt', revision='0' * 40)
                value = self.contract()
                self.assertEqual(value['effect_phase'], 'NOT_STARTED')
                self.assertTrue(value['same_input_safe_retry'])
                self.assertTrue(value['input_correction_required'])
                self.assertEqual(value['same_input_expected_error'], code)

    def test_unknown_validation_and_new_attempt_do_not_reuse_correction(self):
        for code, correction in [('CHECK_NOT_GRANTED', True), ('VERIFICATION_VALIDATION_UNKNOWN', False)]:
            with patch.object(self.broker, '_start', side_effect=fixtures.IntegrationError(code)):
                with self.assertRaises(fixtures.IntegrationError):
                    self.broker.start(self.op, 'a', 'fresh', check_id='check', checkout_receipt='receipt', revision='0' * 40)
            self.assertEqual(self.contract()['input_correction_required'], correction)
        with patch.object(self.broker, '_start', return_value={'state': 'fixture-only'}):
            self.broker.start(self.op, 'a', 'fresh', check_id='check', checkout_receipt='receipt', revision='0' * 40)
        self.assertFalse(self.contract()['input_correction_required'])
        self.assertEqual(self.owner.db.execute('SELECT count(*) FROM c_verification_effect').fetchone()[0], 0)

    def test_other_seat_blocker_is_not_own_effect_reconciliation(self):
        self.owner.db.execute("INSERT INTO c_verification_effect VALUES('other','peer-operation','peer-attempt','run','{}','{}','RUNNING','other-owner','unused',NULL,NULL)")
        value = self.contract()
        self.assertEqual(value['effect_phase'], 'UNKNOWN')
        self.assertFalse(value['same_input_safe_retry'])
        self.assertEqual(value['retry_diagnostic'], 'OTHER_EFFECT_OUTSTANDING')
        self.assertIn('other', value['retry_message'].lower())
        self.assertEqual(self.owner.db.execute("SELECT state FROM c_verification_effect WHERE id='other'").fetchone()[0], 'RUNNING')

    def test_own_unknown_overrides_cached_input_diagnosis(self):
        with patch.object(self.broker, '_start', side_effect=fixtures.IntegrationError('CHECK_NOT_GRANTED')):
            with self.assertRaises(fixtures.IntegrationError):
                self.broker.start(self.op, 'a', 'fresh', check_id='check', checkout_receipt='receipt', revision='0' * 40)
        self.owner.db.execute("INSERT INTO c_verification_effect VALUES('old',?,'a','run','{}','{}','UNKNOWN','old-owner','unused',NULL,NULL)", (self.op,))
        value = self.contract()
        self.assertEqual(value['effect_phase'], 'UNKNOWN')
        self.assertFalse(value['same_input_safe_retry'])
        self.assertEqual(value['retry_diagnostic'], 'OWN_EFFECT_OUTSTANDING')
        self.assertIsNone(value['same_input_expected_error'])

class SendProgressTests(unittest.TestCase):
    def test_delivery_unknown_is_normal_preack_progress_not_proof_of_failure(self):
        from test_integration_mailbox import Owner
        from darkharness.integration.mailbox import Mailbox
        owner = Owner(); self.addCleanup(owner.db.close)
        box = Mailbox(owner)
        # Ledger-only simulation: no SDK/platform send is performed.
        self.assertIsNone(box.prepare_send('send', 'operation', {'content': 'fixture'}))
        self.assertEqual(owner.db.execute("SELECT state FROM c_outbox WHERE id='send'").fetchone()[0], 'DELIVERY_UNKNOWN')
        self.assertEqual([r[0] for r in owner.db.execute('SELECT kind FROM c_event')], ['SEND_INTENT'])
        with self.assertRaisesRegex(fixtures.IntegrationError, 'DELIVERY_UNKNOWN_FENCE'):
            box.prepare_send('send', 'operation', {'content': 'fixture'})
        box.sent('send', {'fixture_ack': True})
        self.assertEqual(owner.db.execute("SELECT state FROM c_outbox WHERE id='send'").fetchone()[0], 'ACKED')
        self.assertEqual([r[0] for r in owner.db.execute('SELECT kind FROM c_event')], ['SEND_INTENT', 'SEND_ACK'])

class TimeoutPreparationTests(unittest.TestCase):
    def setUp(self):
        from pathlib import Path
        self.workspace = str(Path(__file__).resolve().parent)
        self.scope = {'workspace': self.workspace, 'run_id': 'new-run', 'rooms': ['r'],
                      'seats': ['s', 'peer'], 'file_write': True, 'verification': {'checks': {}}}

    def prepare(self, **overrides):
        from darkharness.integration.codex_timeout import prepare_settled_timeout_recovery
        args = dict(run_id='new-run', workspace=self.workspace, rooms=['r'], seats=['s'])
        args.update(overrides)
        return prepare_settled_timeout_recovery(self.scope, **args)

    def test_scope_copy_binds_existing_consumer_before_grant_record(self):
        from test_integration_mailbox import Owner
        from darkharness.integration.policy import ApprovalRouter
        from darkharness.integration.codex_timeout import CodexTimeoutRecovery
        prepared = self.prepare()
        self.assertNotIn('settled_timeout_recovery', self.scope)
        cap = prepared['settled_timeout_recovery']
        self.assertEqual(cap, {'enabled': True, 'run_id': 'new-run', 'workspace': self.workspace, 'rooms': ['r'], 'seats': ['s']})
        owner = Owner(); self.addCleanup(owner.db.close)
        owner.grant(self.workspace, **{k: v for k, v in prepared.items() if k != 'workspace'})
        for seat, allowed in [('s', True), ('peer', False)]:
            router = ApprovalRouter(owner, 'g', seat, 'r', 'new-run', self.workspace)
            recovery = CodexTimeoutRecovery.__new__(CodexTimeoutRecovery); recovery.router = router
            self.assertEqual(recovery.automatic_allowed(owner.db), allowed)
        prepared['verification']['checks']['mutated'] = {}
        self.assertEqual(self.scope['verification']['checks'], {})
        self.assertEqual(self.prepare(), self.prepare())

    def test_prepared_capability_never_settles_unknown_run(self):
        from test_integration_mailbox import Owner
        from darkharness.integration.policy import ApprovalRouter
        from darkharness.integration.codex_timeout import CodexTimeoutRecovery
        owner = Owner(); self.addCleanup(owner.db.close)
        prepared = self.prepare()
        owner.grant(self.workspace, **{k: v for k, v in prepared.items() if k != 'workspace'})
        recovery = CodexTimeoutRecovery.__new__(CodexTimeoutRecovery)
        recovery.router = ApprovalRouter(owner, 'g', 's', 'r', 'new-run', self.workspace)
        self.assertTrue(recovery.automatic_allowed(owner.db))
        owner.db.execute("INSERT INTO controls VALUES('run','new-run',?,1)", (encode({'state': 'UNKNOWN'}),))
        with self.assertRaisesRegex(fixtures.IntegrationError, 'TIMEOUT_RUN_UNRESOLVED'):
            recovery._parent(owner.db, 'not-started', 'a')
        self.assertEqual(json.loads(owner.db.execute("SELECT body FROM controls WHERE kind='run'").fetchone()[0])['state'], 'UNKNOWN')

    def test_capability_consumer_rejects_each_changed_scope_dimension(self):
        from copy import deepcopy
        from test_integration_mailbox import Owner
        from darkharness.integration.policy import ApprovalRouter
        from darkharness.integration.codex_timeout import CodexTimeoutRecovery
        owner = Owner(); self.addCleanup(owner.db.close)
        prepared = self.prepare()
        recovery = CodexTimeoutRecovery.__new__(CodexTimeoutRecovery)
        recovery.router = ApprovalRouter(owner, 'g', 's', 'r', 'new-run', self.workspace)
        for field, value in [('enabled', False), ('run_id', 'old-run'), ('workspace', self.workspace + '-other'),
                             ('rooms', ['other']), ('seats', ['peer'])]:
            with self.subTest(field=field):
                changed = deepcopy(prepared); changed['settled_timeout_recovery'][field] = value
                owner.grant(self.workspace, **{k: v for k, v in changed.items() if k != 'workspace'})
                self.assertFalse(recovery.automatic_allowed(owner.db))

    def test_no_scope_widening_or_implicit_path_normalization(self):
        for overrides in ({'run_id': 'old-run'}, {'rooms': ['other']}, {'seats': ['outsider']},
                          {'rooms': []}, {'seats': ['s', 's']}, {'workspace': 'relative'},
                          {'workspace': self.workspace + '/..'}, {'seats': 's'}):
            with self.subTest(overrides=overrides), self.assertRaises(fixtures.IntegrationError):
                self.prepare(**overrides)
        self.assertNotIn('settled_timeout_recovery', self.scope)

    def test_existing_capability_not_silently_replaced(self):
        cap = self.prepare()['settled_timeout_recovery']
        self.scope['settled_timeout_recovery'] = cap
        self.assertEqual(self.prepare()['settled_timeout_recovery'], cap)
        self.scope['settled_timeout_recovery'] = {**cap, 'seats': ['peer']}
        with self.assertRaisesRegex(fixtures.IntegrationError, 'TIMEOUT_PREPARATION_CONFLICT'):
            self.prepare()

class ResultRepoBoundaryTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.workspace = self.root / 'workspace'; self.workspace.mkdir()
        self.repo = self.workspace / 'band-work' / 'result'; self.repo.mkdir(parents=True)
        (self.repo / '.git').mkdir()
        (self.workspace / 'kickoff').mkdir()
        self.config = {'run_id': 'run', 'room_id': 'r', 'workspace': str(self.workspace),
            'result_repo': str(self.repo), 'credentials_path': str(self.root / 'not-read'), 'grant_id': 'g',
            'model': 'fixture', 'effort': 'high', 'codex_command': ['codex', 'app-server'],
            'seats': [{'alias': role, 'display_name': role, 'role': role, 'participant_id': role}
                      for role in ('coordinator', 'builder', 'reviewer')]}

    def test_config_settings_pin_and_native_workspace_are_separate(self):
        from dataclasses import replace
        from darkharness.integration.launch import resolve_settings, validate_config, SeatManager
        from test_integration_mailbox import Owner
        self.assertTrue(validate_config(self.config)['valid'])
        settings = resolve_settings(self.config)
        self.assertEqual(settings[0].workspace, str(self.workspace))
        self.assertEqual(settings[0].result_repo, str(self.repo))
        self.assertNotEqual(settings[0].fingerprint, replace(settings[0], result_repo=str(self.workspace / 'other')).fingerprint)
        owner = Owner(); self.addCleanup(owner.db.close)
        manager = SeatManager(owner, self.workspace)
        manager.pin_settings(self.config, settings)
        with self.assertRaisesRegex(fixtures.IntegrationError, 'RUN_SETTINGS_PIN_MISMATCH'):
            manager.pin_settings(self.config, tuple(replace(s, result_repo=str(self.workspace / 'other')) for s in settings))
        self.assertFalse((self.workspace / '.git').exists())

    def test_explicit_repo_grant_binding_and_broker_root(self):
        from test_integration_mailbox import Owner
        from darkharness.integration.mailbox import Mailbox
        from darkharness.integration.policy import ApprovalRouter
        owner = Owner(); self.addCleanup(owner.db.close)
        box = Mailbox(owner)
        owner.grant(self.workspace, result_repo=str(self.repo))
        router = ApprovalRouter(owner, 'g', 's', 'r', 'run', self.workspace, result_repo=str(self.repo))
        self.assertTrue(router.active())
        broker = LocalGitBroker(box, router, 'fixture', 'fixture@actors.invalid', self.workspace)
        self.assertEqual(broker.root, self.repo)
        self.assertEqual(router.workspace, str(self.workspace))
        legacy = ApprovalRouter(owner, 'g', 's', 'r', 'run', self.workspace)
        self.assertFalse(legacy.active())
        with owner.transaction(owner.epoch) as db:
            g = json.loads(db.execute("SELECT body FROM controls WHERE id='g'").fetchone()[0])
            g['scope']['result_repo'] = str(self.workspace / 'other')
            db.execute("UPDATE controls SET body=? WHERE id='g'", (encode(g),))
        self.assertFalse(router.active())

    def test_escape_noncanonical_and_nested_repo_rejected(self):
        from darkharness.integration.launch import validate_config
        cases = [str(self.root / 'outside'), 'band-work/result', str(self.workspace),
                 str(self.repo / '..' / 'result'), str(self.repo) + '/']
        for value in cases:
            with self.subTest(value=value), self.assertRaises(fixtures.IntegrationError):
                validate_config({**self.config, 'result_repo': value})
        (self.workspace / 'band-work' / '.git').mkdir()
        with self.assertRaisesRegex(fixtures.IntegrationError, 'RESULT_REPO_NESTED_DENIED'):
            validate_config(self.config)

    def test_symlink_path_is_rejected_without_reading_credentials(self):
        from pathlib import Path
        from darkharness.integration.git_broker import result_repo_boundary
        original = Path.is_symlink
        with patch.object(Path, 'is_symlink', new=lambda path: path == self.repo or original(path)):
            with self.assertRaisesRegex(fixtures.IntegrationError, 'RESULT_REPO_SYMLINK_DENIED'):
                result_repo_boundary(str(self.workspace), str(self.repo))

    def test_result_repo_origin_is_bound_to_same_run_peer_receipt(self):
        from test_integration_mailbox import Owner
        from darkharness.integration.mailbox import Mailbox
        from darkharness.integration.policy import ApprovalRouter
        from darkharness.integration.verification import VerificationBroker
        owner = Owner(); self.addCleanup(owner.db.close)
        box = Mailbox(owner)
        owner.grant(self.workspace, result_repo=str(self.repo), seats=['s', 'peer'])
        peer = ApprovalRouter(owner, 'g', 'peer', 'r', 'run', self.workspace, result_repo=str(self.repo))
        router = ApprovalRouter(owner, 'g', 's', 'r', 'run', self.workspace, result_repo=str(self.repo))
        op = box.receive('peer', 'r', 'controller', 'fixture', 'task')['work']; box.claim(op, 'pa')
        LocalGitBroker(box, peer, 'fixture', 'fixture@actors.invalid', self.workspace)
        owner.db.execute("INSERT INTO c_git_effect VALUES('receipt',?,'pa','{}','ACKED',NULL,'{}')", (op,))
        VerificationBroker.record_git_origin(box, peer, 'receipt')
        broker = VerificationBroker(box, router, report_parsers={}); self.addCleanup(broker.close)
        with owner.transaction(owner.epoch) as db:
            row = db.execute("SELECT * FROM c_git_effect WHERE id='receipt'").fetchone()
            self.assertEqual(broker._same_run(db, row, router._scope(db))['seat'], 'peer')
            raw = db.execute("SELECT body FROM c_event WHERE kind='GIT_RUN_ORIGIN'").fetchone()[0]
            body = json.loads(raw); self.assertEqual(body['result_repo'], str(self.repo))
            body['result_repo'] = str(self.workspace / 'other')
            db.execute("UPDATE c_event SET body=? WHERE kind='GIT_RUN_ORIGIN'", (encode(body),))
            with self.assertRaisesRegex(fixtures.IntegrationError, 'RECEIPT_RUN_MISMATCH'):
                broker._same_run(db, row, router._scope(db))

    def test_legacy_config_still_requires_workspace_repo(self):
        from darkharness.integration.launch import validate_config, resolve_settings
        legacy = {k: v for k, v in self.config.items() if k != 'result_repo'}
        with self.assertRaisesRegex(fixtures.IntegrationError, 'SCOPED_GIT_REPO_REQUIRED'):
            validate_config(legacy)
        (self.workspace / '.git').mkdir()
        self.assertTrue(validate_config(legacy)['valid'])
        self.assertIsNone(resolve_settings(legacy)[0].result_repo)

@unittest.skipUnless(sys.platform == 'linux' and HAS_SDK, 'Linux + installed SDK4 required')
class ResultRepoLinuxTests(unittest.TestCase):
    def test_real_new_layout_snapshot_verify_read_without_docker(self):
        import importlib.util
        from pathlib import Path
        path = Path(__file__).resolve().parents[1] / 'evidence/wo04-factory/controller_e2e.py'
        if not path.is_file():
            self.skipTest('Main controller E2E helper not present in this checkout; run the handoff E2E separately')
        spec = importlib.util.spec_from_file_location('wo04_controller_e2e', path)
        helper = importlib.util.module_from_spec(spec); spec.loader.exec_module(helper)
        result = helper.run_e2e()
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual(result['snapshot_verify_read'], 'PASS')
        self.assertEqual(result['docker'], 'NOT_RUN')
        self.assertEqual(result['model'], 'NOT_RUN')

class GuardScopeMetadataTests(unittest.TestCase):
    def test_ref_metadata_covers_all_enumerated_commits(self):
        from tools import public_guard as guard
        base, target, side = '1' * 40, '2' * 40, '3' * 40
        def git(*args):
            if args[0] == 'rev-parse':
                return (base if args[-1].startswith('base') else target).encode()
            if args[0] == 'merge-base':
                return base.encode()
            if args[0] == 'rev-list':
                self.assertNotIn('--first-parent', args)
                return (side + '\n' + target + '\n').encode()
            self.fail('unexpected Git call')
        metadata = {}
        with patch.object(guard, 'git', side_effect=git), patch.object(guard, 'scan_commit', return_value=[]) as scan:
            self.assertEqual(guard.scan_ref('base', 'target', {}, metadata=metadata), [])
        self.assertEqual(metadata, {'mode': 'ref', 'base': base, 'target': target, 'checked_commit_count': 2})
        self.assertEqual([call.args[0] for call in scan.call_args_list], [side, target])

    def test_main_revision_and_index_metadata_without_private_reads(self):
        from tools import public_guard as guard
        import contextlib
        import io
        for mode, extra, expected in [('revision', ['--revision', 'tip'], 1), ('index', ['--message-file', 'message'], 0),
                                      ('published-history', ['--published-history', 'tip'], 2)]:
            with self.subTest(mode=mode):
                output = io.StringIO()
                def git(*args):
                    if args[0] == 'rev-list':
                        return ('1' * 40 + '\n' + '2' * 40).encode()
                    return b'fixture'
                with patch.object(sys, 'argv', ['public_guard', *extra]), patch.object(guard, 'git', side_effect=git), \
                     patch.object(guard, 'load_private', return_value={}), patch.object(guard, 'resolve', return_value='2' * 40), \
                     patch.object(guard, 'scan_commit', return_value=[]), patch.object(guard, 'scan_tree', return_value=[]), \
                     patch.object(guard, 'records', return_value=[]), patch.object(guard.Path, 'read_bytes', return_value=b'fixture'), \
                     contextlib.redirect_stdout(output):
                    self.assertEqual(guard.main(), 0)
                value = json.loads(output.getvalue())
                self.assertEqual(value['mode'], mode)
                self.assertIsNone(value['base'])
                self.assertEqual(value['target'], 'INDEX' if mode == 'index' else '2' * 40)
                self.assertEqual(value['checked_commit_count'], expected)

@unittest.skipUnless(HAS_SDK, 'Installed SDK4 required; readiness RPCs are mocked')
class ReadinessPromptTests(unittest.TestCase):
    def test_ready_hash_uses_actual_adapter_prompt_not_caller_binding(self):
        from contextvars import ContextVar
        from darkharness.integration.codex import CodexRuntime
        async def ready():
            return None
        async def request(method, params, **kwargs):
            return {'account': {'type': 'chatgpt'}} if method == 'account/read' else {'fixture': True}
        config = SimpleNamespace(system_prompt='actual configured mandate', model='fixture',
                                 reasoning_effort='high', turn_timeout_s=3600.0)
        adapter = SimpleNamespace(config=config, allowed_room='r', _active_room=ContextVar('fixture-room'),
                                  _room_client=lambda room: None, _ensure_client_ready=ready,
                                  _client=SimpleNamespace(request=request), _visible_model_ids=lambda value: ['fixture'],
                                  binding=SimpleNamespace(prompt_sha256='wrong-caller-input'))
        value = asyncio.run(CodexRuntime(adapter).readiness())
        self.assertEqual(value['prompt_sha256'], digest(config.system_prompt.encode()))
        self.assertNotEqual(value['prompt_sha256'], adapter.binding.prompt_sha256)
        self.assertEqual(value['inference'], 'NOT_PROBED')

class MandateContractTests(unittest.TestCase):
    def test_user_specified_harness_is_preserved_for_every_role(self):
        from darkharness.integration.launch import BACKENDS
        self.assertEqual(BACKENDS['codex'].harness, 'Codex (DarkHarness Band SDK adapter)')
        for role in ('coordinator', 'builder', 'reviewer'):
            text = render_mandate('seat', role, 'Codex (DarkHarness Band SDK adapter)', 'test-model', 'high')
            self.assertIn('Harness: Codex (DarkHarness Band SDK adapter)\n', text)
            self.assertNotIn('Harness: DarkHarness (Band SDK Codex)', text)

    def test_generic_controller_pin_and_complete_handoff(self):
        for role in ('coordinator', 'builder', 'reviewer'):
            mandate = render_mandate('seat', role, 'Codex', 'test-model', 'high')
            self.assertIn('Factory pins are controller information', mandate)
            self.assertIn('not seat verification responsibility', mandate)
            self.assertIn('no empty placeholders or literal undefined', mandate)
