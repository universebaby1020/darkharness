"""Real local preflight refusal; anonymous typed correction, no live providers."""
import asyncio
import json
from pathlib import Path
import sys
import unittest

import test_integration_verification as fixtures
from darkharness.integration.artifacts import SecretGuard
from darkharness.integration.git_broker import LocalGitBroker
from darkharness.integration.mailbox import Mailbox, IntegrationError, digest, encode
from darkharness.integration.preflight_recovery import reconcile_preflight
from darkharness.integration.recovery import Recovery
from darkharness.integration.thread_ownership import ThreadOwnership
from darkharness.integration.verification_bridge import VerificationBridge


@unittest.skipUnless(sys.platform == 'linux', 'Linux process ownership')
class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.VerificationTests('test_fixed_argv_private_capture_report_and_idempotence')
        self.f.setUp()
        f = self.f
        p = f.source / 'check.py'
        p.write_text('import sys\nfrom pathlib import Path\ncheckout,out,mode=sys.argv[1:]\ntry: Path(out).mkdir()\nexcept FileExistsError:\n print(out+" already exists; use a fresh output directory to preserve evidence",file=sys.stderr)\n sys.exit(2)\n')
        f.commit(f.source)
        f.cfg.update(source_head=f.git(f.source, 'rev-parse', 'HEAD').strip(), source_tree=f.git(f.source, 'rev-parse', 'HEAD^{tree}').strip(), output_layout='broker-owned-v0', effects={'docker': True, 'network': True})
        f.publish()
        ThreadOwnership(f.box, f.router).bind('review-thread', 'fixture')
        f.box.update(f.op, 'a', thread='review-thread')
        f.box.observe(f.op, 'a', 'TURN_ACCEPTED', {'session': 'review-thread', 'turn': 'review-turn', 'cwd': str(f.repo)})
        self.bridge = VerificationBridge(f.box, f.router, SecretGuard([('fixture', 'never-present')], source_hash='fixture'), report_parsers={'json-v1': lambda p, c: True})
        self.bridge.broker.start(f.op, 'a', 'old-effect', check_id='check', checkout_receipt='snapshot', revision=f.revision)
        self.result = asyncio.run(asyncio.wait_for(self.bridge.broker.wait(f.op, 'a', 'old-effect'), 8))
        self.assertEqual(self.result['state'], 'UNKNOWN')
        self.assertEqual(self.result['process_state'], 'FAILED')
        self.old_ref = digest(encode(self.result).encode())
        def event(kind, value):
            f.box.observe(f.op, 'a', kind, {'client_id': 'review-client', 'payload': value})
        event('STDIN_RPC', {'id': 9, 'method': 'turn/interrupt', 'params': {'threadId': 'review-thread', 'turnId': 'review-turn'}})
        event('STDOUT_RPC', {'id': 9, 'result': {}})
        f.box.observe(f.op, 'a', 'VERIFICATION_YIELD', {'effect': 'old-effect'})
        event('STDOUT_RPC', {'method': 'turn/completed', 'params': {'threadId': 'review-thread', 'turn': {'id': 'review-turn', 'status': 'interrupted', 'error': None}}})
        event('PROCESS_STOPPED', {'members': []})
        f.box.observe(f.op, 'a', 'VERIFICATION_WAIT', {'effect': 'old-effect'})
        f.box.update(f.op, 'a', state='PAUSED', delivery='DELIVERY_UNKNOWN')
        self.recovery = Recovery(f.box, f.router, LocalGitBroker(f.box, f.router, 'fixture', 'fixture@actors.invalid', f.repo))
        # Same authority, only checker output layout migration.
        f.cfg['output_layout'] = 'checker-owned-v1'
        f.publish()

    def tearDown(self):
        self.bridge.broker.close()
        self.f.tearDown()

    @staticmethod
    def trusted_fixture(cfg, argv, result, stdout, stderr):
        if stdout or result['exit_code'] != 2 or stderr != (result['output'] + ' already exists; use a fresh output directory to preserve evidence\n').encode():
            return None
        return {'recognizer': 'trusted-subprocess-fixture', 'external_execution': 'NOT_EXECUTED'}

    def recover(self, recognizer=None):
        return reconcile_preflight(self.bridge, self.recovery, self.f.op, 'a', 'old-effect', recognizer=recognizer or self.trusted_fixture)

    def test_real_refusal_old_artifact_preserved_migration_one_full_child(self):
        first = self.recover()
        again = self.recover()
        self.assertEqual(first['id'], again['id'])
        f = self.f
        child = f.box.read_work(first['id'])
        self.assertEqual(child['thread'], 'review-thread')
        content = json.loads(json.loads(child['input'])['content'])
        self.assertEqual(content['original_task'], 'review exact checkout')
        self.assertEqual(content['verification_result']['state'], 'FAILED')
        self.assertEqual(content['verification_result']['external_execution'], 'NOT_EXECUTED')
        self.assertFalse(content['verification_result']['accepted'])
        raw = f.owner.db.execute('SELECT body FROM c_artifact WHERE hash=?', (self.old_ref,)).fetchone()[0]
        self.assertEqual(json.loads(raw)['state'], 'UNKNOWN')
        self.assertEqual(digest(raw), self.old_ref)
        self.assertEqual(f.owner.db.execute('SELECT COUNT(*) FROM c_verification_continuation').fetchone()[0], 1)
        self.assertEqual(f.owner.db.execute("SELECT COUNT(*) FROM c_event WHERE kind='VERIFICATION_PREFLIGHT_CORRECTED'").fetchone()[0], 1)
        f.box.claim(first['id'], 'child')
        page = self.bridge.read_page(first['id'], 'child', effect_id='old-effect', artifact='stderr', offset=0, limit=16384)
        self.assertIn('already exists', page['text'])

    def test_typed_controller_route_not_seat_or_arbitrary_receipt(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from darkharness.integration.gateway import IntegrationSession
        from darkharness.core import Rejected
        f = self.f
        session = IntegrationSession.__new__(IntegrationSession)
        session.binding, session.hello = None, True
        manager = SimpleNamespace(mailbox=f.box, official_root=f.source, python=sys.executable, agents=[])
        session.service = SimpleNamespace(manager=manager, store=SimpleNamespace(owner=True), environment_id='fixture-env', recovery=lambda op, grant: self.recovery)
        session.response = lambda req, data: data
        req = {'action': 'integration.verification.reconcile_preflight', 'environment_id': 'fixture-env', 'operation_id': f.op, 'payload': {'attempt': 'a', 'grant_id': 'g', 'effect_id': 'old-effect'}}
        session.binding = 'seat'
        with self.assertRaisesRegex(Rejected, 'SEAT_ACTION_DENIED'):
            session._dispatch_integration(req)
        session.binding = None
        bad = {**req, 'payload': {**req['payload'], 'safe': True}}
        with self.assertRaisesRegex(Rejected, 'INVALID_RECOVERY_PAYLOAD'):
            session._dispatch_integration(bad)
        def trusted_route(bridge, recovery, op, attempt, effect):
            bridge.broker.parsers['json-v1'] = lambda p, c: True
            return reconcile_preflight(bridge, recovery, op, attempt, effect, recognizer=self.trusted_fixture)
        with patch('darkharness.integration.artifacts.SecretGuard.official', return_value=self.bridge.guard), patch('darkharness.integration.preflight_recovery.reconcile_preflight', side_effect=trusted_route):
            result = session._dispatch_integration(req)
        self.assertEqual(result['classification'], 'VERIFIED_PREEXECUTION_REFUSAL')
        self.assertEqual(f.box.read_work(result['id'])['delivery'], 'READY')

    def test_official_recognizer_never_accepts_unpinned_fixture(self):
        from darkharness.integration.checker_preflight import official_output_exists
        with self.assertRaisesRegex(IntegrationError, 'NOT_RECOGNIZED'):
            self.recover(official_output_exists)
        self.assertEqual(self.f.box.read_work(self.f.op)['delivery'], 'DELIVERY_UNKNOWN')

    def test_wrong_canonical_spawn_blocks(self):
        f = self.f
        row = f.owner.db.execute("SELECT seq,body FROM c_event WHERE kind='VERIFICATION_SPAWN'").fetchone()
        b = json.loads(row['body']); b['argv'][-1] = 'forged'
        f.owner.db.execute('UPDATE c_event SET body=? WHERE seq=?', (encode(b), row['seq']))
        with self.assertRaisesRegex(IntegrationError, 'SPAWN_MISMATCH'):
            self.recover()

    def test_logs_exact_hashes_and_nonzero_not_generic_safety(self):
        Path(self.result['artifacts']['stderr']['path']).write_text('different refusal')
        with self.assertRaisesRegex(IntegrationError, 'LOG_HASH_MISMATCH'):
            self.recover()

    def test_current_layout_can_be_omitted_without_new_authority_gate(self):
        self.f.cfg.pop('output_layout')
        self.f.publish()
        self.assertTrue(self.recover()['id'])

    def test_current_grant_source_effect_authority_not_ceremonial_layout_gate(self):
        self.f.cfg['effects']['network'] = False
        self.f.publish()
        with self.assertRaisesRegex(IntegrationError, 'CURRENT_AUTHORITY'):
            self.recover()

    def test_cancel_pending_unknown_snapshot_and_native_cessation_block(self):
        f = self.f
        f.box.control('cancel', f.op, 'a', 'cancel', {})
        with self.assertRaisesRegex(IntegrationError, 'PENDING_OR_UNKNOWN'):
            self.recover()
        f.owner.db.execute("DELETE FROM c_control WHERE id='cancel'")
        f.owner.db.execute("UPDATE c_git_effect SET state='UNKNOWN' WHERE id='snapshot'")
        with self.assertRaisesRegex(IntegrationError, 'PENDING_OR_UNKNOWN'):
            self.recover()
        f.owner.db.execute("UPDATE c_git_effect SET state='ACKED' WHERE id='snapshot'")
        f.owner.db.execute("DELETE FROM c_event WHERE kind='PROCESS_STOPPED'")
        with self.assertRaisesRegex(IntegrationError, 'CANONICAL_EVENT_REQUIRED'):
            self.recover()

    def test_live_checker_group_cannot_be_asserted_stopped(self):
        import subprocess
        p = subprocess.Popen([sys.executable, '-B', '-c', 'import time;time.sleep(20)'], start_new_session=True)
        try:
            row = self.f.owner.db.execute("SELECT process FROM c_verification_effect WHERE id='old-effect'").fetchone()
            proc = json.loads(row[0]); proc.update(pid=p.pid, pgid=p.pid, start_ticks=Path(f'/proc/{p.pid}/stat').read_text().rsplit(')',1)[1].split()[19])
            self.f.owner.db.execute("UPDATE c_verification_effect SET process=? WHERE id='old-effect'", (encode(proc),))
            event = self.f.owner.db.execute("SELECT seq,body FROM c_event WHERE kind='VERIFICATION_SPAWN'").fetchone()
            b = json.loads(event['body']); b['process'] = proc
            self.f.owner.db.execute('UPDATE c_event SET body=? WHERE seq=?', (encode(b), event['seq']))
            with self.assertRaisesRegex(IntegrationError, 'PROCESS_OUTSTANDING'):
                self.recover()
        finally:
            p.kill(); p.wait(timeout=3)
