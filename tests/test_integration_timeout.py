"""No provider/Band calls: canonical owner evidence and actual SDK4 stdio timeout."""
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from types import SimpleNamespace

import test_integration_recovery as recovery_fixtures
from test_integration_mailbox import Owner
from darkharness.integration.mailbox import Mailbox, IntegrationError, encode
from darkharness.integration.codex_timeout import CodexTimeoutRecovery
from darkharness.integration.thread_ownership import ThreadOwnership
from darkharness.integration.launch import SeatManager


@unittest.skipUnless(sys.platform == 'linux', 'Linux SDK/process evidence required')
class TimeoutTests(unittest.TestCase):
    setUp = recovery_fixtures.RecoveryTests.setUp
    tearDown = recovery_fixtures.RecoveryTests.tearDown

    def evidence(self, op=None, attempt='a', client='fixture-client', thread='original-thread'):
        op = op or self.op
        ThreadOwnership(self.box, self.router).bind(thread, 'fixture-compatibility')
        self.owner.db.execute("UPDATE c_work SET state='PAUSED',delivery='DELIVERY_UNKNOWN',thread=? WHERE id=? AND attempt=?", (thread, op, attempt))
        def record(kind, body):
            self.box.observe(op, attempt, kind, body)
        def rpc(kind, payload):
            record(kind, {'client_id': client, 'payload': payload})
        rpc('PROCESS_STARTED', {'identity': [99999999, 'fixture-boot', '1'], 'group': 99999999, 'cwd': str(self.root)})
        rpc('STDIN_RPC', {'id': 1, 'method': 'turn/start', 'params': {'threadId': thread, 'cwd': str(self.root), 'approvalPolicy': 'on-request', 'sandboxPolicy': {'type': 'workspaceWrite'}}})
        record('TURN_ACCEPTED', {'session': thread, 'turn': 'slice', 'cwd': str(self.root)})
        rpc('STDIN_RPC', {'id': 2, 'method': 'turn/interrupt', 'params': {'threadId': thread, 'turnId': 'slice'}})
        rpc('STDOUT_RPC', {'id': 2, 'result': {}})
        rpc('STDOUT_RPC', {'method': 'turn/completed', 'params': {'threadId': thread, 'turn': {'id': 'slice', 'status': 'interrupted', 'error': None}}})
        record('TURN_OUTCOME', {'thread_id': thread, 'turn_id': 'slice', 'room_id': 'r', 'turn_status': 'failed', 'turn_error': 'Codex turn timed out after 180.0s', 'settled_reply': False})
        record('RUNTIME_ERROR', {'code': 'TurnResultAlreadyReported', 'diagnostic': 'Codex turn timed out after 180.0s', 'replay': 'FENCED'})
        rpc('PROCESS_STOPPED', {'members': []})
        self.recovery = CodexTimeoutRecovery(self.box, self.router, self.broker)

    def capability(self):
        self.owner.grant(self.root, local_git_write={'directories': ['.']}, settled_timeout_recovery={'enabled': True, 'run_id': 'run', 'rooms': ['r'], 'seats': ['s'], 'workspace': str(self.root)})

    def commit(self, name):
        self.box.update(self.op, 'a', state='RUNNING', delivery='STARTED')
        # The existing base fixture work is terminal; an isolated fixture reset
        # models a live operation before the real broker effect.
        self.owner.db.execute("UPDATE c_work SET state='RUNNING',delivery='STARTED' WHERE id=?", (self.op,))
        head = self.broker._head()[1]
        (self.root / name).write_text('anonymous authored file ' + name)
        return self.broker.commit(self.op, 'a', name, cwd=str(self.root), paths=[name], message='fixture commit', expected_head=head)

    def mutate(self, kind, fn):
        row = self.owner.db.execute('SELECT seq,body FROM c_event WHERE operation=? AND kind=? ORDER BY seq DESC LIMIT 1', (self.op, kind)).fetchone()
        body = json.loads(row['body'])
        fn(body['data'])
        self.owner.db.execute('UPDATE c_event SET body=? WHERE seq=?', (encode(body), row['seq']))

    def test_late_native_terminal_and_prior_settled_reply(self):
        self.evidence()
        self.mutate('TURN_OUTCOME', lambda b: b.update(settled_reply=True))
        db = self.owner.db
        terminal = db.execute("SELECT seq FROM c_event WHERE kind='STDOUT_RPC' AND body LIKE '%turn/completed%'").fetchone()[0]
        outcome = db.execute("SELECT seq FROM c_event WHERE kind='TURN_OUTCOME'").fetchone()[0]
        db.execute('UPDATE c_event SET seq=-1 WHERE seq=?', (terminal,))
        db.execute('UPDATE c_event SET seq=? WHERE seq=?', (terminal, outcome))
        db.execute('UPDATE c_event SET seq=? WHERE seq=-1', (outcome,))
        child = self.recovery.recover(self.op, 'a')
        self.assertEqual(child, self.recovery.recover(self.op, 'a'))
        self.assertEqual(json.loads(self.box.read_work(self.op)['result'])['acceptance'], 'NOT_EVALUATED')

    def test_statcache_refresh_between_real_commits(self):
        first = self.commit('first')
        os.utime(self.root / 'first', None)
        subprocess.run(['git', '-C', str(self.root), 'status', '--porcelain'], check=True, capture_output=True, timeout=5)
        second = self.commit('second')
        self.assertNotEqual(first['index_after'], second['index_before'])
        os.utime(self.root / 'second', None)
        subprocess.run(['git', '-C', str(self.root), 'status', '--porcelain'], check=True, capture_output=True, timeout=5)
        self.evidence()
        self.assertTrue(self.recovery.recover(self.op, 'a')['id'])

    def test_index_semantics_v4_and_malformed_stage_evidence(self):
        from darkharness.integration.index_evidence import indexed_entries, tree_entries
        import hashlib
        self.commit('first')
        subprocess.run(['git', '-C', str(self.root), 'update-index', '--index-version=4'], check=True, timeout=5)
        raw = self.broker._index_bytes()
        self.assertEqual(indexed_entries(raw), tree_entries(self.broker, self.broker._head()[1]))
        malformed = bytearray(raw[:-20])
        malformed[72] |= 0x10  # nonzero stage flag in first entry
        malformed += hashlib.sha1(malformed).digest()
        with self.assertRaisesRegex(IntegrationError, 'INDEX_UNSAFE'):
            indexed_entries(bytes(malformed))
        with self.assertRaisesRegex(IntegrationError, 'INDEX_UNSAFE'):
            indexed_entries(raw[:-1] + b'x')

    def test_real_staged_change_still_blocks(self):
        self.commit('first')
        (self.root / 'first').write_text('staged changed content')
        subprocess.run(['git', '-C', str(self.root), 'add', 'first'], check=True, timeout=5)
        self.evidence()
        with self.assertRaises(IntegrationError):
            self.recovery.recover(self.op, 'a')

    def test_two_real_commits_timeout_continues_once_full_task_thread(self):
        first, latest = self.commit('first'), self.commit('second')
        self.evidence()
        child = self.recovery.recover(self.op, 'a')
        self.assertEqual(child, self.recovery.recover(self.op, 'a'))
        self.assertEqual(self.broker._head()[1], latest['commit'])
        self.assertEqual(subprocess.check_output(['git', '-C', str(self.root), 'rev-list', '--count', 'HEAD']).strip(), b'3')
        parent = self.box.read_work(self.op)
        self.assertEqual((parent['state'], parent['delivery']), ('FAILED', 'RECONCILED'))
        result = json.loads(parent['result'])
        self.assertEqual(result['classification'], 'SETTLED_SDK_TURN_TIMEOUT')
        self.assertEqual(result['acceptance'], 'NOT_EVALUATED')
        work = self.box.read_work(child['id'])
        body = json.loads(work['input'])
        self.assertEqual(work['thread'], 'original-thread')
        self.assertEqual(body['content'], 'original full task')
        self.assertEqual([r['commit'] for r in body['recovery_receipt']['committed_receipts']], [first['commit'], latest['commit']])
        self.assertEqual(self.owner.db.execute("SELECT COUNT(*) FROM c_event WHERE kind='SETTLED_TURN_TIMEOUT'").fetchone()[0], 1)
        for ref in result['evidence_refs']:
            raw = self.owner.db.execute('SELECT body FROM c_artifact WHERE hash=?', (ref['artifact_id'],)).fetchone()[0]
            stored = self.owner.db.execute('SELECT body FROM c_event WHERE seq=?', (ref['seq'],)).fetchone()[0]
            self.assertEqual(raw, stored.encode())

    def test_unrelated_head_and_immutable_receipt_mismatch_block(self):
        latest = self.commit('first')
        self.evidence()
        subprocess.run(['git', '-C', str(self.root), '-c', 'user.name=f', '-c', 'user.email=f@actors.invalid', 'commit', '--allow-empty', '-qm', 'unrelated'], check=True)
        with self.assertRaisesRegex(IntegrationError, 'COMMIT_UNKNOWN'):
            self.recovery.recover(self.op, 'a')
        self.assertEqual(self.box.read_work(self.op)['delivery'], 'DELIVERY_UNKNOWN')

    def test_bounded_capability_covers_new_authenticated_peer_task(self):
        self.capability()
        self.evidence()
        child = self.recovery.recover(self.op, 'a')
        new = self.box.receive('s', 'r', 'peer', 'new-message', 'later full task')['work']
        self.box.claim(new, 'later-attempt')
        self.evidence(new, 'later-attempt', 'later-client')
        later = self.recovery.recover(new, 'later-attempt')
        self.assertNotEqual(child['id'], later['id'])
        self.assertEqual(json.loads(self.box.read_work(later['id'])['input'])['content'], 'later full task')

    def test_repeated_slices_no_progress_blocks_telemetry_only(self):
        self.capability()
        self.evidence()
        child = self.recovery.recover(self.op, 'a')['id']
        self.box.claim(child, 'child-attempt')
        self.evidence(child, 'child-attempt', 'child-client')
        self.box.prepare_send('timeout-telemetry', child, {'content': 'timeout error'})
        self.box.sent('timeout-telemetry', {'id': 'ack'})
        with self.assertRaisesRegex(IntegrationError, 'NO_PROGRESS'):
            self.recovery.recover(child, 'child-attempt')
        self.assertEqual(self.owner.db.execute('SELECT COUNT(*) FROM c_work').fetchone()[0], 2)

    def test_startup_policy_default_off_enabled_and_idempotent(self):
        self.capability()
        self.evidence()
        manager = SeatManager(self.owner, '/unused')
        adapter = SimpleNamespace(auto_recover_settled_timeouts=False, router=self.router, git_broker=self.broker, alias='s', allowed_room='r')
        manager.adapters = [adapter]
        self.assertEqual(manager.recover_timeouts_on_startup(), [])
        adapter.auto_recover_settled_timeouts = True
        result = manager.recover_timeouts_on_startup()
        self.assertEqual(len(result), 1)
        self.assertEqual(manager.recover_timeouts_on_startup(), [])
        self.assertEqual(self.owner.db.execute('SELECT COUNT(*) FROM c_work').fetchone()[0], 2)

    def test_fabricated_reconcile_receipt_cannot_refine_unknown(self):
        from darkharness.integration.gateway import IntegrationSession
        from darkharness.core import Rejected
        self.evidence()
        session = IntegrationSession.__new__(IntegrationSession)
        session.binding, session.hello = None, True
        session.service = SimpleNamespace(environment_id='fixture-env', store=SimpleNamespace(owner=True), manager=SimpleNamespace(mailbox=self.box))
        req = {'action': 'integration.reconcile', 'environment_id': 'fixture-env', 'operation_id': self.op,
               'payload': {'attempt': 'a', 'receipt': {'state': 'safe', 'evidence_refs': ['arbitrary']}}}
        with self.assertRaisesRegex(Rejected, 'INVALID_RECOVERY_PAYLOAD'):
            session._dispatch_integration(req)
        self.assertEqual(self.box.read_work(self.op)['delivery'], 'DELIVERY_UNKNOWN')

    def test_generic_reported_failure_is_not_timeout_and_acked_send_not_replayed(self):
        self.evidence()
        self.mutate('RUNTIME_ERROR', lambda b: b.update(code='GenericFailure'))
        with self.assertRaisesRegex(IntegrationError, 'TERMINAL_EVIDENCE'):
            self.recovery.recover(self.op, 'a')
        self.assertEqual(self.box.read_work(self.op)['delivery'], 'DELIVERY_UNKNOWN')
        self.mutate('RUNTIME_ERROR', lambda b: b.update(code='TurnResultAlreadyReported'))
        payload = {'content': 'complete original handoff', 'mentions': ['peer']}
        self.box.prepare_send('settled-handoff', self.op, payload)
        self.box.sent('settled-handoff', {'id': 'known-platform-ack'})
        child = self.recovery.recover(self.op, 'a')['id']
        self.box.claim(child, 'recovery-attempt')
        from darkharness.integration.codex import GuardedTools
        from darkharness.integration.artifacts import SecretGuard
        from test_integration_codex import Tools
        raw = Tools()
        adapter = SimpleNamespace(router=self.router, mailbox=self.box, git_broker=self.broker, guard=SecretGuard([('fixture', 'never-present')], source_hash='fixture'))
        recovered = asyncio.run(GuardedTools(raw, adapter, child, 'recovery-attempt').send_message(**payload))
        self.assertEqual(recovered, {'id': 'known-platform-ack'})
        self.assertEqual(raw.sent, [])
        self.assertEqual(self.owner.db.execute('SELECT COUNT(*) FROM c_outbox').fetchone()[0], 1)

    def test_peer_answer_after_timeout_preserves_proof_not_untrusted_inherited_id(self):
        self.capability()
        committed = self.commit('first')
        self.evidence()
        timeout_child = self.recovery.recover(self.op, 'a')['id']
        self.box.claim(timeout_child, 'question-attempt')
        self.box.update(timeout_child, 'question-attempt', state='PAUSED', delivery='YIELDED')
        self.box.question('after-timeout-question', timeout_child, 'peer', {'question': 'handoff detail'})
        child = self.box.answer('after-timeout-question', 'peer', 'actual peer progress')
        self.box.claim(child, 'answer-timeout')
        self.evidence(child, 'answer-timeout', 'answer-timeout-client')
        continued = self.recovery.recover(child, 'answer-timeout')
        receipt = json.loads(self.box.read_work(continued['id'])['input'])['recovery_receipt']
        self.assertEqual(receipt['committed_receipts'][0]['commit'], committed['commit'])
        self.assertEqual(self.broker._head()[1], committed['commit'])

    def test_peer_answer_child_authorized_by_canonical_question(self):
        self.capability()
        ThreadOwnership(self.box, self.router).bind('original-thread', 'fixture')
        self.owner.db.execute("UPDATE c_work SET state='PAUSED',delivery='YIELDED' WHERE id=?", (self.op,))
        self.box.question('fixture-question', self.op, 'peer', {'question': 'full context'})
        child = self.box.answer('fixture-question', 'peer', 'authenticated answer')
        self.box.claim(child, 'answer-attempt')
        self.evidence(child, 'answer-attempt', 'answer-client')
        continued = self.recovery.recover(child, 'answer-attempt')
        self.assertIn('authenticated answer', self.box.read_work(continued['id'])['input'])
        self.assertIn('original full task', self.box.read_work(continued['id'])['input'])

    def test_verification_child_auth_result_hash_unknown_and_scope(self):
        from darkharness.integration.mailbox import digest
        self.capability()
        ThreadOwnership(self.box, self.router).bind('original-thread', 'fixture')
        self.owner.db.execute('CREATE TABLE c_verification_effect(id TEXT,operation TEXT,attempt TEXT,run_id TEXT,state TEXT,result TEXT)')
        self.owner.db.execute('CREATE TABLE c_verification_continuation(effect TEXT,result_ref TEXT,child TEXT)')
        raw = encode({'run_id': 'run', 'state': 'SUCCEEDED', 'accepted': True}).encode()
        with self.owner.transaction(self.owner.epoch) as db:
            ref = Mailbox.artifact(db, raw)
        child = digest(encode(['fixture-checker', ref, 'verification-continuation']).encode())
        self.owner.db.execute("INSERT INTO c_verification_effect VALUES('fixture-checker',?,'a','run','SUCCEEDED',?)", (self.op, raw.decode()))
        self.owner.db.execute("INSERT INTO c_verification_continuation VALUES('fixture-checker',?,?)", (ref, child))
        self.owner.db.execute("UPDATE c_work SET state='PAUSED',delivery='YIELDED' WHERE id=?", (self.op,))
        self.owner.db.execute("INSERT INTO c_work VALUES(?,'s','r',?,NULL,'QUEUED','READY','original-thread',NULL)", (child, encode({'content': 'original full task and actual verification result', 'parent_operation': 'untrusted-not-used'})))
        self.box.claim(child, 'verified-attempt')
        self.evidence(child, 'verified-attempt', 'verified-client')
        self.owner.db.execute("UPDATE c_verification_effect SET state='UNKNOWN'")
        with self.assertRaises(IntegrationError):
            self.recovery.recover(child, 'verified-attempt')
        self.owner.db.execute("UPDATE c_verification_effect SET state='SUCCEEDED',run_id='wrong-run'")
        with self.assertRaisesRegex(IntegrationError, 'LINEAGE_CONFLICT'):
            self.recovery.recover(child, 'verified-attempt')
        self.owner.db.execute("UPDATE c_verification_effect SET run_id='run'")
        self.owner.db.execute("UPDATE c_verification_continuation SET result_ref='forged'")
        with self.assertRaisesRegex(IntegrationError, 'LINEAGE_CONFLICT'):
            self.recovery.recover(child, 'verified-attempt')
        self.owner.db.execute('UPDATE c_verification_continuation SET result_ref=?', (ref,))
        continued = self.recovery.recover(child, 'verified-attempt')
        self.assertIn('original full task', self.box.read_work(continued['id'])['input'])

    def test_cancel_stale_attempt_process_and_native_terminal_fences(self):
        self.evidence()
        with self.assertRaisesRegex(IntegrationError, 'ATTEMPT'):
            self.recovery.recover(self.op, 'stale')
        self.box.control('cancel', self.op, 'a', 'cancel', {})
        with self.assertRaisesRegex(IntegrationError, 'CANCELLED'):
            self.recovery.recover(self.op, 'a')
        self.owner.db.execute("DELETE FROM c_control")
        self.mutate('PROCESS_STOPPED', lambda b: b['payload'].update(members=[[12, 'fixture', '1']]))
        with self.assertRaisesRegex(IntegrationError, 'TERMINAL_EVIDENCE'):
            self.recovery.recover(self.op, 'a')
        self.mutate('PROCESS_STOPPED', lambda b: b['payload'].update(members=[]))
        self.mutate('TURN_OUTCOME', lambda b: b.update(turn_status='interrupted', turn_error=None))
        with self.assertRaisesRegex(IntegrationError, 'NOT_SDK_TURN_TIMEOUT'):
            self.recovery.recover(self.op, 'a')

    def test_pending_network_git_checker_question_stop_revoke(self):
        self.evidence()
        self.box.prepare_send('pending', self.op, {'content': 'network effect'})
        with self.assertRaisesRegex(IntegrationError, 'OUTSTANDING_UNKNOWN'):
            self.recovery.recover(self.op, 'a')
        self.box.sent('pending', {'id': 'ack'})
        self.owner.db.execute("INSERT INTO c_git_effect VALUES('unknown',?, 'a','{}','UNKNOWN',NULL,NULL)", (self.op,))
        with self.assertRaisesRegex(IntegrationError, 'GIT_UNKNOWN'):
            self.recovery.recover(self.op, 'a')
        self.owner.db.execute("DELETE FROM c_git_effect WHERE id='unknown'")
        self.owner.db.execute('CREATE TABLE c_verification_effect(operation TEXT,state TEXT)')
        self.owner.db.execute("INSERT INTO c_verification_effect VALUES(?,'RUNNING')", (self.op,))
        with self.assertRaisesRegex(IntegrationError, 'CHECKER'):
            self.recovery.recover(self.op, 'a')
        self.owner.db.execute('DELETE FROM c_verification_effect')
        self.box.question('q', self.op, 'peer', {})
        with self.assertRaisesRegex(IntegrationError, 'CONTROL_PENDING'):
            self.recovery.recover(self.op, 'a')
        self.owner.db.execute('DELETE FROM c_question')
        self.owner.db.execute("INSERT INTO controls VALUES('run','run',?,1)", (json.dumps({'state': 'STOPPED'}),))
        with self.assertRaisesRegex(IntegrationError, 'GRANT'):
            self.recovery.recover(self.op, 'a')
        self.owner.db.execute("DELETE FROM controls WHERE kind='run'")
        self.owner.db.execute("UPDATE controls SET body=? WHERE kind='grant'", (json.dumps({'revoked': True}),))
        with self.assertRaisesRegex(IntegrationError, 'GRANT'):
            self.recovery.recover(self.op, 'a')

    def test_unknown_native_effect_pending_approval_and_live_owned_process(self):
        self.evidence()
        self.box.observe(self.op, 'a', 'STDOUT_RPC', {'client_id': 'fixture-client', 'payload': {'method': 'item/started', 'params': {'item': {'id': 'network', 'type': 'mcpToolCall'}}}})
        with self.assertRaisesRegex(IntegrationError, 'EXTERNAL_EFFECT_UNKNOWN'):
            self.recovery.recover(self.op, 'a')
        self.owner.db.execute("DELETE FROM c_event WHERE seq=(SELECT MAX(seq) FROM c_event)")
        self.box.observe(self.op, 'a', 'STDOUT_RPC', {'client_id': 'fixture-client', 'payload': {'id': 9, 'method': 'item/commandExecution/requestApproval', 'params': {}}})
        with self.assertRaisesRegex(IntegrationError, 'NEW_PERMISSION'):
            self.recovery.recover(self.op, 'a')
        self.owner.db.execute("DELETE FROM c_event WHERE seq=(SELECT MAX(seq) FROM c_event)")
        proc = subprocess.Popen([sys.executable, '-B', '-c', 'import time;time.sleep(30)'], cwd=self.root)
        try:
            with self.assertRaisesRegex(IntegrationError, 'PROCESS_OUTSTANDING'):
                self.recovery.recover(self.op, 'a')
        finally:
            proc.terminate()
            proc.wait(timeout=3)


FIXTURE = r'''
import json, sys, subprocess
base, cwd = sys.argv[1:]
def emit(frame):
    print(json.dumps(frame), flush=True)
for raw in sys.stdin:
    frame = json.loads(raw)
    method, ident, params = frame.get('method'), frame.get('id'), frame.get('params', {})
    if ident is None:
        continue
    result = {}
    if method == 'thread/start':
        result = {'thread': {'id': 'fixture-own-thread'}}
    elif method == 'thread/resume':
        result = {'thread': {'id': params['threadId']}}
    elif method == 'model/list':
        result = {'data': [{'id': 'test-model', 'supportedReasoningEfforts': [{'reasoningEffort': 'high'}]}]}
    elif method == 'account/read':
        result = {'account': {'type': 'chatgpt'}}
    elif method == 'turn/start':
        emit({'id': ident, 'result': {'turn': {'id': 'fixture-slice'}}})
        head = subprocess.check_output(['git', '-C', cwd, 'rev-parse', 'HEAD']).decode().strip()
        if head == base:
            emit({'id': 500, 'method': 'item/tool/call', 'params': {'threadId': 'fixture-own-thread', 'turnId': 'fixture-slice', 'callId': 'fixture-commit-call', 'tool': 'dh_local_git_commit', 'arguments': {'cwd': cwd, 'paths': ['stage-file'], 'message': 'anonymous fixture commit', 'expected_head': base}}})
        else:
            emit({'method': 'turn/completed', 'params': {'threadId': 'fixture-own-thread', 'turn': {'id': 'fixture-slice', 'status': 'completed', 'error': None}}})
        continue
    elif method == 'turn/interrupt':
        emit({'method': 'turn/completed', 'params': {'threadId': 'fixture-own-thread', 'turn': {'id': 'fixture-slice', 'status': 'interrupted', 'error': None}}})
    elif method is None:
        continue
    emit({'id': ident, 'result': result})
'''


@unittest.skipUnless(sys.platform == 'linux', 'WSL real SDK4 stdio fixture')
class LiveSliceTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_sdk_TurnResultAlreadyReported_committed_resume_no_replay(self):
        from test_integration_codex import Tools
        from band.adapters.codex import CodexAdapterConfig
        from band.core.types import PlatformMessage
        from band.integrations.codex.types import CodexSessionState
        from darkharness.integration.codex import DurableCodexAdapter
        from darkharness.integration.artifacts import SecretGuard
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            subprocess.run(['git', 'init', '-q', td], check=True)
            (root / 'base').write_text('base')
            subprocess.run(['git', '-C', td, 'add', 'base'], check=True)
            subprocess.run(['git', '-C', td, '-c', 'user.name=fixture', '-c', 'user.email=fixture@actors.invalid', 'commit', '-qm', 'base'], check=True)
            head = subprocess.check_output(['git', '-C', td, 'rev-parse', 'HEAD']).decode().strip()
            (root / 'stage-file').write_text('actual anonymous seat source')
            owner, tools = Owner(), Tools()
            box = Mailbox(owner)
            from darkharness.integration.policy import ApprovalRouter
            owner.grant(root, local_git_write={'directories': ['.']}, settled_timeout_recovery={'enabled': True, 'run_id': 'run', 'seats': ['s'], 'rooms': ['r'], 'workspace': td})
            router = ApprovalRouter(owner, 'g', 's', 'r', 'run', root)
            config = CodexAdapterConfig(model='test-model', reasoning_effort='high', workspace_for_room=lambda _: td,
                sandbox='workspace-write', approval_policy='on-request', system_prompt='fixture full mandate', enable_self_config_tools=False,
                codex_command=(sys.executable, '-B', '-c', FIXTURE, head, td), turn_timeout_s=0.5, inject_history_on_resume_failure=False)
            adapter = DurableCodexAdapter(mailbox=box, router=router, guard=SecretGuard([('fixture-pattern', r'fixture-secret-never-present')], source_hash='fixture'), alias='s', display_name='anonymous builder', room_id='r', workspace=td, coordinator_id='peer', config=config, auto_recover_settled_timeouts=True)
            try:
                await adapter.on_started('fixture', 'fixture')
                msg = PlatformMessage('single-dispatch', 'r', 'entire original multi-stage task and handoff', 'peer', 'agent', 'peer', 'text', {}, datetime.now(timezone.utc))
                await adapter.on_message(msg, tools, CodexSessionState(), 'roster', None, is_session_bootstrap=True, room_id='r')
                await asyncio.wait_for(adapter.worker, timeout=8)
                works = [dict(r) for r in owner.db.execute('SELECT * FROM c_work ORDER BY rowid')]
                self.assertEqual(len(works), 2, [dict(r) for r in owner.db.execute("SELECT kind,body FROM c_event WHERE kind NOT IN ('STDIN_RPC','STDOUT_RPC')")])
                self.assertEqual((works[0]['state'], works[0]['delivery']), ('FAILED', 'RECONCILED'))
                self.assertEqual((works[1]['state'], works[1]['delivery']), ('SUCCEEDED', 'RETURNED'))
                self.assertEqual(works[0]['thread'], works[1]['thread'])
                self.assertEqual(subprocess.check_output(['git', '-C', td, 'rev-list', '--count', 'HEAD']).strip(), b'2')
                self.assertEqual(owner.db.execute('SELECT COUNT(*) FROM c_git_effect').fetchone()[0], 1)
                self.assertEqual(owner.db.execute('SELECT COUNT(*) FROM c_inbox').fetchone()[0], 1)
                result = json.loads(works[0]['result'])
                self.assertEqual(result['classification'], 'SETTLED_SDK_TURN_TIMEOUT')
                self.assertEqual(result['observed_timeout_s'], 0.5)
                frames = [json.loads(r[0])['data']['payload'] for r in owner.db.execute("SELECT body FROM c_event WHERE operation=? AND kind='STDIN_RPC'", (works[1]['id'],))]
                started = next(f for f in frames if f.get('method') == 'turn/start')
                self.assertIn('entire original multi-stage task and handoff', str(started['params']['input']))
                self.assertIn('recovery_receipt', str(started['params']['input']))
                self.assertTrue(any(f.get('method') == 'thread/resume' and f['params']['threadId'] == 'fixture-own-thread' for f in frames))
            finally:
                adapter.stopping = True
                if adapter.worker and not adapter.worker.done():
                    adapter.worker.cancel()
                    await asyncio.wait_for(asyncio.gather(adapter.worker, return_exceptions=True), 3)
                await asyncio.wait_for(adapter.on_cleanup('r'), 3)
                owner.db.close()
