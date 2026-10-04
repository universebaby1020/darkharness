"""Offline SDK4 + real OwnedStdio subprocess + real trusted checker fixtures.

No provider/Band/Docker access. All waits and subprocesses bounded in tests.
"""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import test_integration_verification as verification_fixture
from test_integration_codex import HAS_SDK, Tools
from darkharness.integration.artifacts import SecretGuard
from darkharness.integration.mailbox import IntegrationError, digest, encode
from darkharness.integration.report_criteria import JsonReportCriteria
from darkharness.integration.verification import _immutable
from darkharness.integration.verification_bridge import VerificationBridge

if HAS_SDK:
    from band.adapters.codex import CodexAdapterConfig
    from band.core.types import PlatformMessage
    from band.integrations.codex.types import CodexSessionState
    from darkharness.integration.codex import DurableCodexAdapter, OwnedStdioClient, ClientEvidence
    from datetime import datetime, timezone


RPC_SERVER = r'''
import sys,json,uuid
from pathlib import Path
marker=Path(sys.argv[1]); args=json.loads(sys.argv[2]); thread=None
for line in sys.stdin:
 p=json.loads(line); m=p.get('method'); v=p.get('params') or {}; result={}
 if m=='model/list': result={'data':[{'id':'test-model','supportedReasoningEfforts':[{'reasoningEffort':'high'}]}]}
 if m=='thread/start': thread=uuid.uuid4().hex; result={'thread':{'id':thread}}
 if m=='thread/resume': thread=v['threadId']; result={'thread':{'id':thread}}
 if m=='thread/read': result={'thread':{'id':v['threadId'],'turns':[{'input':'owned prior native goal','effects':'historical DATA, never replay'}]}}
 if m=='turn/start': result={'turn':{'id':'turn'}}
 if 'id' in p and m: print(json.dumps({'id':p['id'],'result':result}),flush=True)
 if m=='turn/start':
  if args and not marker.exists():
   marker.touch()
   print(json.dumps({'id':99,'method':'item/tool/call','params':{'tool':'dh_verify','callId':'native-check','arguments':args}}),flush=True)
  else:
   print(json.dumps({'method':'turn/completed','params':{'turn':{'id':'turn','status':'completed'}}}),flush=True)
'''


class CriteriaTests(unittest.TestCase):
    def test_generic_coordinator_mandate_closes_accepted_assignment(self):
        from darkharness.integration.artifacts import render_mandate
        text = render_mandate('generic coordinator', 'coordinator', 'generic harness', 'test-model', 'high')
        self.assertIn('actual accepted revision and evidence to its original requester', text)
        self.assertIn('Do not silently end with band_no_reply', text)

    def test_revision_counts_state_and_summary_without_revision(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            revision = 'a' * 40
            report = {'revision': revision, 'schema': 2, 'state': 'completed', 'mode': 'isolated', 'claimed': '2', 'highest': 2, 'checks': [{'collected': 3, 'passed': 3, 'skipped': 0, 'errors': 0, 'deselected': 0, 'xfailed': 0}]}
            summary = {'folders': {'1': {'claimed': True}, '2': {'claimed': True}}}
            spec = {'file': 'report.json', 'revision_path': ['revision'], 'equals': [{'path': ['schema'], 'value': 2}, {'path': ['state'], 'value': 'completed'}, {'path': ['mode'], 'value': 'isolated'}, {'path': ['claimed'], 'value': '2'}], 'at_least': [{'path': ['highest'], 'value': 2}], 'each': [{'path': ['checks'], 'positive': [['collected']], 'equal_paths': [{'left': ['passed'], 'right': ['collected']}], 'zero': [['skipped'], ['errors'], ['deselected'], ['xfailed']]}]}
            aggregate = {'file': 'summary.json', 'each': [{'path': ['folders'], 'equals': [{'path': ['claimed'], 'value': True}]}]}
            config = {'report_files': ['report.json', 'summary.json'], 'report_criteria': [spec, aggregate]}
            context = _immutable({'request': {'revision': revision}, 'config': config})
            parser = JsonReportCriteria()
            (root / 'summary.json').write_text(json.dumps(summary))
            def accepted(code=0):
                (root / 'report.json').write_text(json.dumps(report))
                return parser.parse_request(root, code, context)
            self.assertTrue(accepted())
            self.assertFalse(accepted(7))
            for path, bad in [('revision', 'b'*40), ('schema', True), ('state', 'running'), ('mode', 'preview'), ('claimed', '1'), ('highest', 1)]:
                old = report[path]; report[path] = bad
                self.assertFalse(accepted(), path)
                report[path] = old
            for key, bad in [('collected', 0), ('passed', 2), ('skipped', 1), ('errors', 1), ('deselected', 1), ('xfailed', 1)]:
                old = report['checks'][0][key]; report['checks'][0][key] = bad
                self.assertFalse(accepted(), key)
                report['checks'][0][key] = old
            report['checks'] = []
            self.assertFalse(accepted())
            with self.assertRaises(TypeError):
                context['request']['revision'] = 'b'*40
            no_anchor = _immutable({'request': {'revision': revision}, 'config': {'report_files': ['summary.json'], 'report_criteria': [aggregate]}})
            with self.assertRaisesRegex(IntegrationError, 'REVISION_ANCHOR'):
                parser.parse_request(root, 0, no_anchor)
            report['checks'] = [{'collected': 3, 'passed': 3, 'skipped': 0, 'errors': 0, 'deselected': 0, 'xfailed': 0}]
            self.assertTrue(accepted())
            (root / 'summary.json').unlink()
            with self.assertRaises(FileNotFoundError):
                parser.parse_request(root, 0, context)


    def test_aggregate_all_required_files_and_every_revision_anchor(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            revision = 'a'*40
            files = ['summary.json', *['part-'+str(i)+'.json' for i in range(1, 5)]]
            specs = [{'file': 'summary.json', 'equals': [{'path': ['required'], 'value': [1, 2, 3, 4]}, {'path': ['claimed'], 'value': True}]}]
            (root/'summary.json').write_text(json.dumps({'required': [1, 2, 3, 4], 'claimed': True}))
            for name in files[1:]:
                (root/name).write_text(json.dumps({'revision': revision, 'count': 1}))
                specs.append({'file': name, 'revision_path': ['revision'], 'positive': [['count']]})
            context = _immutable({'request': {'revision': revision}, 'config': {'report_files': files, 'report_criteria': specs}})
            parser = JsonReportCriteria()
            self.assertTrue(parser.parse_request(root, 0, context))
            (root/files[-1]).write_text(json.dumps({'revision': 'b'*40, 'count': 1}))
            self.assertFalse(parser.parse_request(root, 0, context))
            (root/files[-1]).write_text(json.dumps({'revision': revision, 'count': 1}))
            (root/files[-1]).unlink()
            with self.assertRaises(FileNotFoundError):
                parser.parse_request(root, 0, context)
            bad = _immutable({'request': {'revision': revision}, 'config': {'report_files': files, 'report_criteria': specs[:-1]}})
            with self.assertRaisesRegex(IntegrationError, 'COVERAGE'):
                parser.parse_request(root, 0, bad)


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = verification_fixture.VerificationTests('test_automatic_receipt_resolution_and_returned_receipt_selector')
        self.fixture.setUp()
        f = self.fixture
        self.guard = SecretGuard([('fixture', r'\bsk-\w{16,}')], source_hash='fixture')
        self.bridge = VerificationBridge(f.box, f.router, self.guard, report_parsers={'json-v1': lambda p, code: json.loads((p/'report.json').read_text())['accepted']})

    def tearDown(self):
        self.bridge.broker.close()
        self.fixture.tearDown()

    def run_check(self, mode='ok'):
        f = self.fixture
        f.mode(mode)
        effect, public = asyncio.run(asyncio.wait_for(self.bridge.start(f.op, 'a', 'native-call', {'check_id': 'check', 'checkout_receipt': f.snap, 'revision': f.revision}), 8))
        self.assertIn(public['state'], {'INTENT', 'RUNNING', 'SUCCEEDED', 'FAILED'})
        result = asyncio.run(asyncio.wait_for(self.bridge.broker.wait(f.op, 'a', effect), 8))
        return effect, result

    def test_real_success_one_completion_and_diagnostics(self):
        f = self.fixture
        effect, result = self.run_check()
        child = self.bridge.complete(f.op, 'a', effect)
        self.assertEqual(child, self.bridge.complete(f.op, 'a', effect))
        self.assertEqual(f.box.read_work(f.op)['state'], 'PAUSED')
        content = json.loads(f.box.read_work(child)['input'])['content']
        self.assertIn('review exact checkout', content)
        self.assertIn(result['revision'], content)
        self.assertNotIn(result['output'], content)
        self.assertEqual(f.owner.db.execute('SELECT COUNT(*) FROM c_verification_continuation').fetchone()[0], 1)
        f.box.claim(child, 'child-attempt')
        page = self.bridge.read_page(child, 'child-attempt', effect_id=effect, artifact='stderr', offset=0, limit=8)
        self.assertEqual(page['text'], 'stderr c')
        self.assertEqual(page['next_offset'], 8)
        Path(result['artifacts']['stderr']['path']).write_text('changed')
        with self.assertRaisesRegex(IntegrationError, 'HASH_MISMATCH'):
            self.bridge.read_page(child, 'child-attempt', effect_id=effect, artifact='stderr', offset=0, limit=8)
        with self.assertRaisesRegex(IntegrationError, 'SAME_RUN'):
            self.bridge.read_page(child, 'child-attempt', effect_id='unknown', artifact='stderr', offset=0, limit=8)

    def test_nonzero_failure_details_continues_not_acceptance(self):
        f = self.fixture
        effect, result = self.run_check('fail')
        self.assertEqual(result['exit_code'], 7)
        self.assertFalse(result['accepted'])
        child = self.bridge.complete(f.op, 'a', effect)
        f.box.claim(child, 'child')
        page = self.bridge.read_page(child, 'child', effect_id=effect, artifact='stderr', offset=0, limit=16384)
        self.assertIn('stderr captured', page['text'])
        self.assertIn('CHECKER_NONZERO', json.loads(f.box.read_work(child)['input'])['content'])

    def test_unknown_actual_missing_report_never_replay(self):
        f = self.fixture
        # A real parser exception leaves UNKNOWN, not a synthesized failure.
        self.bridge.broker.parsers['json-v1'] = lambda p, code: (p/'absent.json').read_text()
        effect, result = self.run_check()
        self.assertEqual(result['state'], 'UNKNOWN')
        child = self.bridge.complete(f.op, 'a', effect)
        self.assertIn('FENCED', f.box.read_work(child)['input'])
        f.box.claim(child, 'child')
        with self.assertRaisesRegex(IntegrationError, 'UNKNOWN_FENCE'):
            self.bridge.broker.start(child, 'child', 'different-effect', check_id='check', checkout_receipt=f.snap, revision=f.revision)
        self.assertEqual(f.owner.db.execute('SELECT COUNT(*) FROM c_verification_continuation').fetchone()[0], 1)

    def test_late_stale_stop_and_revoke_evidence_only(self):
        f = self.fixture
        effect, result = self.run_check()
        f.box.control('stop', f.op, 'a', 'cancel', {'source': 'fixture'})
        self.assertIsNone(self.bridge.complete(f.op, 'a', effect))
        with f.owner.transaction(f.owner.epoch) as db:
            db.execute('DELETE FROM c_control')
            db.execute("UPDATE c_work SET attempt='new' WHERE id=?", (f.op,))
        self.assertIsNone(self.bridge.complete(f.op, 'a', effect))
        with f.owner.transaction(f.owner.epoch) as db:
            db.execute("UPDATE c_work SET attempt='a' WHERE id=?", (f.op,))
            g = json.loads(db.execute("SELECT body FROM controls WHERE id='g'").fetchone()[0]); g['revoked']=True
            db.execute("UPDATE controls SET body=? WHERE id='g'", (json.dumps(g),))
        self.assertIsNone(self.bridge.complete(f.op, 'a', effect))

    def test_cancel_owned_checker_no_continuation(self):
        f = self.fixture
        f.mode('sleep')
        async def exercise():
            effect, _ = await self.bridge.start(f.op, 'a', 'native', {'check_id': 'check', 'checkout_receipt': f.snap, 'revision': f.revision})
            self.bridge.broker.cancel(f.op, 'a', effect)
            result = await self.bridge.broker.wait(f.op, 'a', effect)
            self.assertEqual(result['state'], 'CANCELLED')
            self.assertIsNone(self.bridge.complete(f.op, 'a', effect))
        asyncio.run(asyncio.wait_for(exercise(), 8))

    def test_full_file_secret_guard_before_paging_and_cross_run_hash_fences(self):
        f = self.fixture
        checker = f.source / 'check.py'
        checker.write_text(checker.read_text().replace('print("stderr captured",file=sys.stderr,flush=True)', 'print("stderr captured",file=sys.stderr,flush=True)\nprint("sk"+"-"+"Z"*20,file=sys.stderr,flush=True)'))
        f.commit(f.source)
        f.cfg['source_head'] = f.git(f.source, 'rev-parse', 'HEAD').strip()
        f.cfg['source_tree'] = f.git(f.source, 'rev-parse', 'HEAD^{tree}').strip()
        f.publish()
        effect, result = self.run_check()
        child = self.bridge.complete(f.op, 'a', effect)
        f.box.claim(child, 'child')
        page = self.bridge.read_page(child, 'child', effect_id=effect, artifact='stderr', offset=16, limit=7)
        self.assertNotIn('Z', page['text'])
        self.assertIn('[REDACT', page['text'])
        with f.owner.transaction(f.owner.epoch) as db:
            db.execute("UPDATE c_verification_effect SET run_id='other-run' WHERE id=?", (effect,))
        with self.assertRaisesRegex(IntegrationError, 'SAME_RUN'):
            self.bridge.read_page(child, 'child', effect_id=effect, artifact='stderr', offset=0, limit=8)
        with f.owner.transaction(f.owner.epoch) as db:
            db.execute("UPDATE c_verification_effect SET run_id='run' WHERE id=?", (effect,))
            db.execute('UPDATE c_artifact SET body=? WHERE hash=?', (b'forged', page['result_ref']))
        with self.assertRaisesRegex(IntegrationError, 'RESULT_HASH'):
            self.bridge.read_page(child, 'child', effect_id=effect, artifact='stderr', offset=0, limit=8)

    def test_legacy_historical_ledger_not_current_grant_or_human_preparation(self):
        from darkharness.integration.legacy_git_origin import execution_ledger_run
        f = self.fixture
        with f.owner.transaction(f.owner.epoch) as db:
            effect = dict(db.execute("SELECT * FROM c_git_effect WHERE id='commit'").fetchone())
            self.assertIsNone(execution_ledger_run(db, effect))
            db.execute('INSERT INTO c_event(seq,operation,kind,body,at) VALUES(-2,NULL,?,?,?)', ('RUNTIME_BINDINGS', encode({'run_id': 'historical-run', 'bindings': [{'workspace': str(f.repo)}]}), 'fixture'))
            db.execute('INSERT INTO c_event(seq,operation,kind,body,at) VALUES(-1,?,?,?,?)', (effect['operation'], 'TURN_ACCEPTED', encode({'attempt': effect['attempt'], 'data': {'cwd': str(f.repo), 'session': 'own-thread', 'turn': 'own-turn'}}), 'fixture'))
            self.assertEqual(execution_ledger_run(db, effect), 'historical-run')
            forged = dict(effect, receipt=encode({'state': 'COMMITTED', 'commit': 'a'*40}))
            self.assertIsNone(execution_ledger_run(db, forged))
            self.assertIsNone(execution_ledger_run(db, dict(effect, attempt='other')))
            db.execute("UPDATE c_event SET body=? WHERE seq=-1", (encode({'attempt': effect['attempt'], 'data': {'cwd': str(f.source), 'session': 'other', 'turn': 'turn'}}),))
            self.assertIsNone(execution_ledger_run(db, effect))
            db.execute('DELETE FROM c_event WHERE seq=-1')
            self.assertIsNone(execution_ledger_run(db, effect))  # human-preparation is not a native creating seat.

    def test_stale_check_config_known_result_evidence_only(self):
        f = self.fixture
        effect, _ = self.run_check()
        f.cfg['argv'][-1] = 'reject'
        f.publish()
        self.assertIsNone(self.bridge.complete(f.op, 'a', effect))
        self.assertEqual(f.owner.db.execute('SELECT COUNT(*) FROM c_verification_continuation').fetchone()[0], 0)

    def test_declared_parser_request_context_real_revision_mismatch(self):
        f = self.fixture
        f.cfg['report_contract'] = 'json-criteria-v1'
        f.cfg['report_criteria'] = [{'file': 'report.json', 'revision_path': ['revision'], 'equals': [{'path': ['accepted'], 'value': True}]}]
        f.publish()
        effect, result = self.run_check()
        self.assertEqual(result['state'], 'FAILED')  # report has no requested revision
        self.assertEqual(result['reason'], 'DECLARED_REPORT_REJECTED')


@unittest.skipUnless(HAS_SDK and sys.platform == 'linux', 'installed SDK4/Linux required')
class OwnedSdkTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = verification_fixture.VerificationTests('test_automatic_receipt_resolution_and_returned_receipt_selector')
        self.fixture.setUp()
        f = self.fixture
        # The fixture's initial reviewer work must not block SDK's own queue.
        f.box.update(f.op, 'a', state='SUCCEEDED', delivery='RETURNED')
        self.guard = SecretGuard([('fixture', r'\bsk-\w{16,}')], source_hash='fixture')
        self.tools = Tools()
        self.clients = []
        self.verify_args = {}
        self.marker = f.root / 'requested'
        outer = self
        class Adapter(DurableCodexAdapter):
            def _build_client(self, config):
                state = self._require_active_client_state()
                evidence = ClientEvidence(self, self.current.get())
                client = OwnedStdioClient(command=(sys.executable, '-B', '-u', '-c', RPC_SERVER, str(outer.marker), json.dumps(outer.verify_args)), cwd=state.workspace, env={}, guard=self.guard, record=evidence.record)
                client.evidence = evidence
                outer.clients.append(client)
                return client
        self.adapter = Adapter(mailbox=f.box, router=f.router, guard=self.guard, alias='s', display_name='fixture reviewer', room_id='r', workspace=f.repo, coordinator_id='coordinator-id', config=CodexAdapterConfig(model='test-model', reasoning_effort='high', workspace_for_room=lambda room: str(f.repo), sandbox='workspace-write', approval_policy='on-request', system_prompt='exact generic mandate', inject_history_on_resume_failure=False), report_parsers={'json-v1': lambda p, code: json.loads((p/'report.json').read_text())['accepted']})
        await self.adapter.on_started('fixture reviewer', 'component')

    async def asyncTearDown(self):
        await asyncio.wait_for(self.adapter.on_cleanup('r'), 8)
        self.fixture.tearDown()

    async def deliver(self, mid, content, thread=None):
        msg = PlatformMessage(mid, 'r', content, 'peer', 'agent', 'generic peer', 'text', {}, datetime.now(timezone.utc))
        await self.adapter.on_message(msg, self.tools, CodexSessionState(thread_id=thread, room_id='r'), None, None, is_session_bootstrap=True, room_id='r')

    async def settle(self):
        await asyncio.wait_for(self.adapter.worker, 12)

    def events(self, kind):
        return [json.loads(r[0])['data'] for r in self.fixture.owner.db.execute('SELECT body FROM c_event WHERE kind=?', (kind,))]

    async def test_real_owned_retirement_own_resume_cross_seat_and_stale_history(self):
        await self.deliver('first', 'original full goal', 'foreign-seat-thread')
        await self.settle()
        first = self.clients[0]
        self.assertIsNotNone(first._proc.returncode)
        self.assertIsNone(self.adapter._room_client('r').client)
        self.assertNotIn('r', self.adapter._room_threads)
        bound = self.events('THREAD_TOOLSET_BOUND')[-1]['thread']
        await self.deliver('second', 'reviewer ACCEPT evidence', 'foreign-seat-thread')
        await self.settle()
        requests = [e['payload'] for e in self.events('STDIN_RPC') if 'method' in e['payload']]
        self.assertEqual(len([r for r in requests if r['method']=='thread/start']), 1)
        resumes = [r for r in requests if r['method']=='thread/resume']
        self.assertEqual([r['params']['threadId'] for r in resumes], [bound])
        self.assertNotIn('history', resumes[0]['params'])
        inputs = [r['params']['input'] for r in requests if r['method']=='turn/start']
        self.assertEqual(sum('[System Instructions]' in str(v) for v in inputs), 1)
        owned = self.adapter.thread_ownership.latest()
        self.assertEqual(owned['prompt_hash'], digest(b'exact generic mandate'))
        with self.fixture.owner.transaction(self.fixture.owner.epoch) as db:
            db.execute("UPDATE c_owned_thread SET seat='peer' WHERE thread=?", (bound,))
        await self.deliver('third', 'another message', bound)
        await self.settle()
        self.assertEqual(len(self.events('THREAD_MANDATE_INJECTED')), 2)
        self.assertNotEqual(self.events('THREAD_TOOLSET_BOUND')[-1]['thread'], bound)

    async def test_toolset_cutover_has_full_own_goal_native_history_and_exact_mandate(self):
        await self.deliver('first', 'original full user assignment and evidence')
        await self.settle()
        old = self.adapter.thread_ownership.latest()['thread']
        with self.fixture.owner.transaction(self.fixture.owner.epoch) as db:
            db.execute("UPDATE c_owned_thread SET compatibility='old-tools' WHERE thread=?", (old,))
        await self.deliver('second', 'latest reviewer ACCEPT')
        await self.settle()
        bound = self.events('THREAD_TOOLSET_BOUND')[-1]
        self.assertTrue(bound['cutover'])
        self.assertNotEqual(bound['thread'], old)
        requests = [e['payload'] for e in self.events('STDIN_RPC') if 'method' in e['payload']]
        self.assertTrue(any(r['method']=='thread/read' and r['params']=={'threadId': old, 'includeTurns': True} for r in requests))
        self.assertFalse(any(r['method']=='thread/resume' for r in requests))
        text = str([r for r in requests if r['method']=='turn/start'][-1]['params']['input'])
        for expected in ('original full user assignment and evidence', 'latest reviewer ACCEPT', 'owned prior native goal', '[System Instructions]', 'exact generic mandate', 'history_ref', 'Do not replay'):
            self.assertIn(expected, text)

    async def test_implementation_hash_change_does_not_cutover(self):
        await self.deliver('first', 'full task')
        await self.settle()
        with patch('darkharness.integration.codex.Path.read_bytes', return_value=b'implementation changed'):
            await self.deliver('second', 'ordinary next message')
            await self.settle()
        bound = self.events('THREAD_TOOLSET_BOUND')
        self.assertEqual(bound[0]['thread'], bound[1]['thread'])
        self.assertNotEqual(bound[0]['sources'], bound[1]['sources'])
        self.assertFalse(bound[1]['cutover'])

    async def test_sdk_start_yield_real_result_one_continuation(self):
        f = self.fixture
        self.verify_args = {'check_id': 'check', 'checkout_receipt': f.snap, 'revision': f.revision}
        # Checker pauses briefly; no model polling, still RUNNING/STARTED.
        checker = f.source / 'check.py'
        checker.write_text(checker.read_text().replace('if mode=="sleep": time.sleep(20)', 'if mode=="sleep": time.sleep(20)\ntime.sleep(.3)'))
        f.commit(f.source)
        f.cfg['source_head'] = f.git(f.source, 'rev-parse', 'HEAD').strip()
        f.cfg['source_tree'] = f.git(f.source, 'rev-parse', 'HEAD^{tree}').strip()
        f.publish()
        await self.deliver('verification', 'original full verification task')
        async def until_yield():
            while not self.events('VERIFICATION_WAIT'):
                await asyncio.sleep(.01)
        await asyncio.wait_for(until_yield(), 8)
        parent = f.owner.db.execute("SELECT * FROM c_work WHERE delivery='STARTED'").fetchone()
        self.assertEqual(parent['state'], 'RUNNING')
        await self.settle()
        self.assertEqual(len(self.events('VERIFICATION_YIELD')), 1)
        canonical = f.owner.db.execute('SELECT * FROM c_verification_continuation').fetchall()
        self.assertEqual(len(canonical), 1)
        child = f.box.read_work(canonical[0]['child'])
        self.assertIn('original full verification task', json.loads(child['input'])['content'])
        self.assertIn('DECLARED_REPORT_ACCEPTED', json.loads(child['input'])['content'])
        self.assertEqual(f.box.read_work(parent['id'])['state'], 'PAUSED')
        self.assertEqual(child['state'], 'SUCCEEDED')
        self.assertEqual(self.events('VERIFICATION_WAIT')[0]['sdk_turn_timeout_seconds'], 180)
        callbacks = [e['payload'] for e in self.events('STDIN_RPC') if e['payload'].get('id')==99]
        self.assertEqual(len(callbacks), 1)
        self.assertTrue(callbacks[0]['result']['success'])
        self.assertNotIn('output', callbacks[0]['result']['contentItems'][0]['text'])
        self.assertTrue(all(c._proc.returncode is not None for c in self.clients))

    async def test_manager_stop_cancels_waiting_real_checker_without_continuation(self):
        f = self.fixture
        f.mode('sleep')
        self.verify_args = {'check_id': 'check', 'checkout_receipt': f.snap, 'revision': f.revision}
        await self.deliver('verification', 'full original task')
        async def until_yield():
            while not self.events('VERIFICATION_WAIT'):
                await asyncio.sleep(.01)
        await asyncio.wait_for(until_yield(), 8)
        await asyncio.wait_for(self.adapter.on_interrupt('r', 'stop'), 8)
        await asyncio.wait_for(asyncio.to_thread(self.adapter.verification.broker.close), 8)
        self.assertEqual(f.owner.db.execute('SELECT COUNT(*) FROM c_verification_continuation').fetchone()[0], 0)
        v = f.owner.db.execute('SELECT state FROM c_verification_effect').fetchone()
        self.assertEqual(v[0], 'CANCELLED')
