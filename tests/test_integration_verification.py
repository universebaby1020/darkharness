"""Disposable Linux subprocesses + independent Git snapshots; no Docker/provider calls."""
import asyncio
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from test_integration_mailbox import Owner
from darkharness.integration.mailbox import Mailbox, IntegrationError
from darkharness.integration.policy import ApprovalRouter
from darkharness.integration.git_broker import LocalGitBroker
from darkharness.integration.verification import VerificationBroker


class LockedOwner(Owner):
    def __init__(self):
        super().__init__()
        self.mutex = threading.RLock()

    @contextmanager
    def transaction(self, epoch=None):
        with self.mutex:
            with super().transaction(epoch) as db:
                yield db


@unittest.skipUnless(sys.platform == 'linux', 'Linux owned process groups')
class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name).resolve()
        self.repo = self.root / 'result'
        self.source = self.root / 'checker'
        for p in (self.repo, self.source):
            p.mkdir()
            self.git(p, 'init', '-q', '-b', 'main')
        (self.repo / 'base').write_text('base')
        self.commit(self.repo)
        (self.source / 'check.py').write_text('import os,sys,time,json\nfrom pathlib import Path\ncheckout,out,mode=sys.argv[1:]\nout=Path(out)\nout.mkdir(mode=0o700,exist_ok=True)\nprint(checkout,flush=True)\nprint("stderr captured",file=sys.stderr,flush=True)\nassert "UNRELATED_FIXTURE" not in os.environ\nif mode=="sleep": time.sleep(20)\nif mode=="large": print("x"*200000)\n(out/"report.json").write_text(json.dumps({"accepted":mode!="reject"}))\nsys.exit(7 if mode=="fail" else 0)\n')
        self.commit(self.source)
        self.owner = LockedOwner()
        self.box = Mailbox(self.owner)
        self.owner.grant(self.repo, seats=['s', 'peer'], local_git_write={'paths': ['reviewed']}, review_snapshot={'scratch': str(self.root / 'snapshots')})
        self.peer_router = ApprovalRouter(self.owner, 'g', 'peer', 'r', 'run', self.repo)
        peer_op = self.box.receive('peer', 'r', 'controller', 'peer-task', 'create reviewed revision')['work']
        self.box.claim(peer_op, 'pa')
        git = LocalGitBroker(self.box, self.peer_router, 'fixture', 'fixture@actors.invalid', self.root)
        (self.repo / 'reviewed').write_text('reviewed')
        self.parent = self.git(self.repo, 'rev-parse', 'HEAD').strip()
        self.created = git.commit(peer_op, 'pa', 'commit', cwd=str(self.repo), paths=['reviewed'], message='reviewed', expected_head=self.parent)
        self.revision = self.created['commit']
        self.snap = git.snapshot(peer_op, 'pa', 'snapshot', cwd=str(self.repo), revision=self.revision, name='exact')
        self.checkout = Path(self.snap['path'])
        # Mechanical controller adapter stamping; no per-receipt Grant update.
        self.assertEqual(VerificationBroker.record_git_origin(self.box, self.peer_router, self.created), 'commit')
        self.assertEqual(VerificationBroker.record_git_origin(self.box, self.peer_router, self.snap), 'snapshot')
        self.router = ApprovalRouter(self.owner, 'g', 's', 'r', 'run', self.repo)
        self.op = self.box.receive('s', 'r', 'controller', 'review-task', 'review exact checkout')['work']
        self.box.claim(self.op, 'a')
        self.cfg = {'source_root': str(self.source), 'source_head': self.git(self.source, 'rev-parse', 'HEAD').strip(), 'source_tree': self.git(self.source, 'rev-parse', 'HEAD^{tree}').strip(), 'executable': str(Path(sys.executable).resolve()), 'cwd': str(self.source), 'argv': ['-B', str(self.source / 'check.py'), '{checkout}', '{output}', 'ok'], 'output_root': str(self.root / 'outputs'), 'checkout_root': str(self.root / 'snapshots'), 'effects': {'docker': False, 'network': False}, 'report_contract': 'json-v1', 'report_files': ['report.json']}
        self.restrict_receipts = False
        self.publish()
        self.broker = VerificationBroker(self.box, self.router, report_parsers={'json-v1': lambda output, code: json.loads((output / 'report.json').read_text())['accepted']})

    def tearDown(self):
        self.broker.close()
        self.owner.db.close()
        self.td.cleanup()

    def git(self, repo, *args):
        return subprocess.check_output(['/usr/bin/git', *args], cwd=repo, stderr=subprocess.PIPE, timeout=5).decode()

    def commit(self, repo):
        self.git(repo, 'add', '.')
        self.git(repo, '-c', 'user.name=fixture', '-c', 'user.email=fixture@actors.invalid', 'commit', '-qm', 'fixture')

    def publish(self):
        with self.owner.transaction(self.owner.epoch) as db:
            g = json.loads(db.execute("SELECT body FROM controls WHERE id='g'").fetchone()[0])
            g['scope']['verification'] = {'checks': {'check': self.cfg}}
            if self.restrict_receipts:
                g['scope']['verification']['receipts'] = {'snapshot': {'commit_effect': 'commit', 'source_ref': 'refs/heads/main'}}
            db.execute("UPDATE controls SET body=? WHERE id='g'", (json.dumps(g),))

    def start(self, effect='effect', **kw):
        return self.broker.start(self.op, 'a', effect, check_id='check', checkout_receipt='snapshot', revision=self.revision, **kw)

    def wait(self, effect='effect'):
        return asyncio.run(asyncio.wait_for(self.broker.wait(self.op, 'a', effect), 8))

    def mode(self, mode):
        self.cfg['argv'][-1] = mode
        self.publish()

    def test_checker_owns_fresh_report_directory(self):
        from darkharness.integration.report_criteria import JsonReportCriteria
        p = self.source / 'check.py'
        p.write_text('import sys,json,subprocess\nfrom pathlib import Path\ncheckout,out,mode=sys.argv[1:]\nout=Path(out)\nassert not out.exists(), "precreated output"\nout.mkdir(mode=0o700)\nrevision=subprocess.check_output(["/usr/bin/git","-C",checkout,"rev-parse","HEAD"]).decode().strip()\nreport={"revision":revision if mode!="wrong" else "b"*40,"collected":0 if mode=="zero" else 1,"passed":1}\n(out/"report.json").write_text(json.dumps(report))\nprint("private fixture stdout")\n')
        self.commit(self.source)
        self.cfg.update(source_head=self.git(self.source, 'rev-parse', 'HEAD').strip(), source_tree=self.git(self.source, 'rev-parse', 'HEAD^{tree}').strip(), output_layout='checker-owned-v1', report_criteria=[{'file': 'report.json', 'revision_path': ['revision'], 'positive': [['collected']], 'equal_paths': [{'left': ['passed'], 'right': ['collected']}]}])
        self.broker.parsers['json-v1'] = JsonReportCriteria()
        self.publish()
        self.start()
        result = self.wait()
        self.assertEqual(result['state'], 'SUCCEEDED')
        self.assertEqual(json.loads((Path(result['output']) / 'report.json').read_text())['revision'], self.revision)
        self.assertNotEqual(Path(result['artifacts']['stdout']['path']).parent, Path(result['output']))
        for mode in ('zero', 'wrong'):
            self.mode(mode)
            self.start(mode)
            rejected = self.wait(mode)
            self.assertEqual(rejected['state'], 'FAILED')
            self.assertEqual(rejected['reason'], 'DECLARED_REPORT_REJECTED')
            self.assertFalse(rejected['accepted'])

    def test_nonzero_missing_report_is_known_process_failure(self):
        p = self.source / 'check.py'
        p.write_text('import sys\nsys.exit(2)\n')
        self.commit(self.source)
        self.cfg.update(source_head=self.git(self.source, 'rev-parse', 'HEAD').strip(), source_tree=self.git(self.source, 'rev-parse', 'HEAD^{tree}').strip())
        self.publish()
        self.start()
        result = self.wait()
        self.assertEqual(result['state'], 'FAILED')
        self.assertEqual(result['exit_code'], 2)
        self.assertFalse(result['accepted'])

    def test_automatic_receipt_resolution_and_returned_receipt_selector(self):
        with self.owner.transaction(self.owner.epoch) as db:
            g = json.loads(db.execute("SELECT body FROM controls WHERE id='g'").fetchone()[0])
            self.assertNotIn('receipts', g['scope']['verification'])
        self.broker.start(self.op, 'a', 'effect', check_id='check', checkout_receipt=self.snap, revision=self.revision)
        result = self.wait()
        self.assertEqual(result['state'], 'SUCCEEDED')
        self.assertEqual(result['source_commit_effect'], 'commit')
        self.assertEqual(self.start()['result'], result)
        forged = {**self.snap, 'path': str(self.repo)}
        with self.assertRaisesRegex(IntegrationError, 'RECEIPT_UNKNOWN'):
            self.broker.start(self.op, 'a', 'forged', check_id='check', checkout_receipt=forged, revision=self.revision)
        with self.assertRaisesRegex(IntegrationError, 'RECEIPT_UNKNOWN'):
            self.broker.start(self.op, 'a', 'path', check_id='check', checkout_receipt=str(self.checkout), revision=self.revision)

    def test_optional_receipt_restriction_is_not_required_registration(self):
        self.restrict_receipts = True
        self.publish()
        with self.owner.transaction(self.owner.epoch) as db:
            g = json.loads(db.execute("SELECT body FROM controls WHERE id='g'").fetchone()[0])
            g['scope']['verification']['receipts'] = {}
            db.execute("UPDATE controls SET body=? WHERE id='g'", (json.dumps(g),))
        with self.assertRaisesRegex(IntegrationError, 'RECEIPT_NOT_GRANTED'):
            self.start()
        self.publish()
        self.start()
        self.assertEqual(self.wait()['state'], 'SUCCEEDED')

    def test_same_run_origin_and_legacy_authenticated_resolver(self):
        with self.owner.transaction(self.owner.epoch) as db:
            saved = [tuple(r) for r in db.execute("SELECT operation,body FROM c_event WHERE kind='GIT_RUN_ORIGIN'")]
            db.execute("DELETE FROM c_event WHERE kind='GIT_RUN_ORIGIN'")
        with self.assertRaisesRegex(IntegrationError, 'RUN_PROVENANCE_REQUIRED'):
            self.start()
        self.broker.receipt_run_resolver = lambda db, effect: 'other-run'
        with self.assertRaisesRegex(IntegrationError, 'RUN_PROVENANCE_REQUIRED'):
            self.start()
        # This mapping is an authenticated coordinator fixture, not a Grant
        # receipts allowlist nor model input. Production must use real run ledger.
        known_origins = {json.loads(body)['id']: json.loads(body)['run_id'] for op, body in saved}
        self.broker.receipt_run_resolver = lambda db, effect: known_origins.get(effect['id'])
        self.start()
        self.assertEqual(self.wait()['state'], 'SUCCEEDED')
        with self.owner.transaction(self.owner.epoch) as db:
            for op, raw in saved:
                origin = json.loads(raw)
                origin['run_id'] = 'other-run'
                Mailbox.event(db, op, 'GIT_RUN_ORIGIN', origin)
        with self.assertRaisesRegex(IntegrationError, 'RECEIPT_RUN_MISMATCH'):
            self.start('cross-run')

    def test_reviewer_snapshot_of_peer_commit_and_authorized_seats(self):
        git = LocalGitBroker(self.box, self.router, 'reviewer fixture', 'reviewer@actors.invalid', self.root)
        snap = git.snapshot(self.op, 'a', 'reviewer-snapshot', cwd=str(self.repo), revision=self.revision, name='reviewer')
        VerificationBroker.record_git_origin(self.box, self.router, snap)
        self.broker.start(self.op, 'a', 'effect', check_id='check', checkout_receipt=snap, revision=self.revision)
        self.assertEqual(self.wait()['state'], 'SUCCEEDED')
        with self.owner.transaction(self.owner.epoch) as db:
            db.execute("UPDATE c_work SET seat='not-authorized' WHERE id=(SELECT operation FROM c_git_effect WHERE id='commit')")
        with self.assertRaisesRegex(IntegrationError, 'AUTHORIZED_RECEIPT_SEAT'):
            self.start('unauthorized')

    def test_real_venv_invocation_preserves_prefix_and_target_identity(self):
        import venv
        env_root = self.root / 'checker-venv'
        venv.EnvBuilder(with_pip=False, symlinks=True).create(env_root)
        exe = env_root / 'bin/python'
        self.assertTrue(exe.is_symlink())
        p = self.source / 'check.py'
        raw = p.read_text().replace('sys.exit(7 if mode=="fail" else 0)', '(out/"prefix.json").write_text(json.dumps({"prefix":sys.prefix,"base_prefix":sys.base_prefix}))\nsys.exit(7 if mode=="fail" else 0)')
        p.write_text(raw)
        self.commit(self.source)
        self.cfg.update(source_head=self.git(self.source, 'rev-parse', 'HEAD').strip(), source_tree=self.git(self.source, 'rev-parse', 'HEAD^{tree}').strip(), executable=str(exe))
        self.cfg['report_files'].append('prefix.json')
        self.publish()
        self.start()
        result = self.wait()
        self.assertEqual(result['state'], 'SUCCEEDED')
        prefix = json.loads((Path(result['output']) / 'prefix.json').read_text())
        self.assertEqual(prefix['prefix'], str(env_root))
        self.assertNotEqual(prefix['prefix'], prefix['base_prefix'])
        with self.owner.transaction(self.owner.epoch) as db:
            config = json.loads(db.execute("SELECT config FROM c_verification_effect WHERE id='effect'").fetchone()[0])
            spawned = json.loads(db.execute("SELECT body FROM c_event WHERE operation=? AND kind='VERIFICATION_SPAWN'", (self.op,)).fetchone()[0])
        self.assertEqual(spawned['argv'][0], str(exe))
        self.assertEqual(config['executable']['target'], str(exe.resolve()))
        self.assertEqual(config['executable'], spawned['executable'])
        self.assertIn('sha256', config['executable'])
        # Diagnostic control: exactly the previous resolve-and-execute behavior
        # loses pyvenv.cfg semantics. No network, pip or installed dependencies.
        control = subprocess.check_output([str(exe.resolve()), '-B', '-c', 'import sys; print(sys.prefix == sys.base_prefix)'], env={}, timeout=5)
        self.assertEqual(control.strip(), b'True')

    def test_fixed_argv_private_capture_report_and_idempotence(self):
        os.environ['UNRELATED_FIXTURE'] = 'fixture'
        try:
            self.start()
            result = self.wait()
        finally:
            os.environ.pop('UNRELATED_FIXTURE', None)
        self.assertEqual(result['state'], 'SUCCEEDED')
        self.assertTrue(result['accepted'])
        self.assertEqual(result['exit_code'], 0)
        self.assertEqual(self.start()['result'], result)
        self.assertEqual(len(list((self.root / 'outputs').iterdir())), 1)
        self.assertIn(str(self.checkout), Path(result['artifacts']['stdout']['path']).read_text())
        self.assertIn('stderr captured', Path(result['artifacts']['stderr']['path']).read_text())
        self.assertEqual(Path(result['output']).stat().st_mode & 0o777, 0o700)
        with self.assertRaises(TypeError):
            self.start(argv=['evil'])
        with self.assertRaisesRegex(IntegrationError, 'CHECK_NOT_GRANTED'):
            self.broker.start(self.op, 'a', 'evil', check_id='check;touch /tmp/no', checkout_receipt='snapshot', revision=self.revision)
        self.assertEqual(self.broker.public_result(self.op, 'a', 'effect')['accepted'], True)
        self.assertNotIn('artifacts', self.broker.public_result(self.op, 'a', 'effect'))

    def test_nonzero_and_report_rejection(self):
        self.mode('fail')
        self.start()
        self.assertEqual(self.wait()['exit_code'], 7)
        self.assertFalse(self.wait()['accepted'])
        self.mode('reject')
        self.start('reject')
        self.assertEqual(self.wait('reject')['state'], 'FAILED')
        self.assertFalse(self.wait('reject')['accepted'])

    def test_exit_zero_without_report_is_unknown_and_fenced(self):
        self.cfg['report_files'] = ['missing.json']
        self.publish()
        self.start()
        self.assertEqual(self.wait()['state'], 'UNKNOWN')
        with self.assertRaisesRegex(IntegrationError, 'UNKNOWN_FENCE'):
            self.start('retry')

    def test_dirty_pinned_source_and_revision_mismatch(self):
        p = self.source / 'check.py'
        raw = p.read_text()
        p.write_text(raw + '\n# dirty\n')
        with self.assertRaisesRegex(IntegrationError, 'SOURCE_DIRTY'):
            self.start()
        p.write_text(raw)
        self.cfg['source_head'] = '0' * 40
        self.publish()
        with self.assertRaisesRegex(IntegrationError, 'PIN_MISMATCH'):
            self.start()

    def test_checkout_provenance_branch_revision_and_peer(self):
        with self.owner.transaction(self.owner.epoch) as db:
            db.execute("UPDATE c_git_effect SET state='UNKNOWN' WHERE id='snapshot'")
        with self.assertRaisesRegex(IntegrationError, 'RECEIPT'):
            self.start()
        with self.owner.transaction(self.owner.epoch) as db:
            db.execute("UPDATE c_git_effect SET state='ACKED' WHERE id='snapshot'")
            db.execute("UPDATE c_work SET seat='s' WHERE id=(SELECT operation FROM c_git_effect WHERE id='commit')")
        with self.assertRaisesRegex(IntegrationError, 'PEER|RECEIPT_RUN_MISMATCH'):
            self.start()
        with self.owner.transaction(self.owner.epoch) as db:
            db.execute("UPDATE c_work SET seat='peer' WHERE id=(SELECT operation FROM c_git_effect WHERE id='commit')")
        self.git(self.repo, 'branch', '-m', 'other')
        with self.assertRaisesRegex(IntegrationError, 'SOURCE_IDENTITY'):
            self.start()

    def test_checkout_bytes_symlinks_hardlinks_and_escape(self):
        p = self.checkout / 'reviewed'
        p.write_text('tampered')
        with self.assertRaisesRegex(IntegrationError, 'CHECKOUT_DIRTY'):
            self.start()
        p.unlink()
        p.symlink_to(self.repo / 'reviewed')
        with self.assertRaisesRegex(IntegrationError, 'PATH_LINK'):
            self.start()
        p.unlink()
        os.link(self.repo / 'reviewed', p)
        with self.assertRaisesRegex(IntegrationError, 'PATH_LINK'):
            self.start()
        p.unlink()
        p.write_text('reviewed')
        self.cfg['checkout_root'] = str(self.root / 'elsewhere')
        self.publish()
        with self.assertRaisesRegex(IntegrationError, 'CHECKOUT_ESCAPE'):
            self.start()

    def test_checkout_revision_mismatch_and_metadata_link(self):
        with self.assertRaisesRegex(IntegrationError, 'RECEIPT_MISMATCH'):
            self.broker.start(self.op, 'a', 'effect', check_id='check', checkout_receipt='snapshot', revision=self.parent)
        head = self.checkout / '.git/HEAD'
        raw = head.read_bytes()
        head.unlink()
        os.link(self.repo / '.git/HEAD', head)
        with self.assertRaisesRegex(IntegrationError, 'PATH_LINK'):
            self.start()
        head.unlink()
        head.write_bytes(raw)
        with self.owner.transaction(self.owner.epoch) as db:
            row = db.execute("SELECT request FROM c_git_effect WHERE id='snapshot'").fetchone()
            request = json.loads(row[0])
            request['identity'][2] += 1
            db.execute("UPDATE c_git_effect SET request=? WHERE id='snapshot'", (json.dumps(request),))
        with self.assertRaisesRegex(IntegrationError, 'SOURCE_IDENTITY'):
            self.start()

    def test_index_dirty_even_when_tracked_bytes_restored(self):
        p = self.source / 'check.py'
        raw = p.read_bytes()
        p.write_bytes(raw + b'\n# staged dirty\n')
        self.git(self.source, 'add', 'check.py')
        p.write_bytes(raw)
        with self.assertRaisesRegex(IntegrationError, 'SOURCE_DIRTY'):
            self.start()

    def test_cancel_only_owned_group_and_stop(self):
        self.mode('sleep')
        unrelated = subprocess.Popen([sys.executable, '-B', '-c', 'import time;time.sleep(20)'], start_new_session=True)
        try:
            self.start()
            deadline = time.monotonic() + 5
            while self.broker.status(self.op, 'a', 'effect')['state'] != 'RUNNING':
                self.assertLess(time.monotonic(), deadline)
                time.sleep(.01)
            self.broker.cancel(self.op, 'a', 'effect')
            self.assertEqual(self.wait()['state'], 'CANCELLED')
            self.assertIsNone(unrelated.poll())
            self.start('stop')
            with self.owner.transaction(self.owner.epoch) as db:
                db.execute("INSERT INTO controls VALUES('run','run',?,1)", (json.dumps({'state': 'STOPPED'}),))
            self.assertEqual(self.wait('stop')['state'], 'CANCELLED')
            with self.assertRaisesRegex(IntegrationError, 'GRANT'):
                self.start('after-stop')
        finally:
            unrelated.terminate()
            unrelated.wait(timeout=3)

    def test_owned_descendant_cancel_and_normal_exit_cleanup(self):
        p = self.source / 'check.py'
        raw = p.read_text().replace('import os,sys,time,json', 'import os,sys,time,json,subprocess')
        raw = raw.replace('if mode=="sleep": time.sleep(20)', 'if mode in ("child", "childsleep"):\n child=subprocess.Popen([sys.executable,"-B","-c","import time;time.sleep(20)"])\n (out/"child.pid").write_text(str(child.pid))\n if mode=="childsleep": time.sleep(20)\nif mode=="sleep": time.sleep(20)')
        p.write_text(raw)
        self.commit(self.source)
        self.cfg.update(source_head=self.git(self.source, 'rev-parse', 'HEAD').strip(), source_tree=self.git(self.source, 'rev-parse', 'HEAD^{tree}').strip())
        for mode in ('childsleep', 'child'):
            self.mode(mode)
            self.start(mode)
            with self.owner.transaction(self.owner.epoch) as db:
                output = Path(db.execute('SELECT output FROM c_verification_effect WHERE id=?', (mode,)).fetchone()[0])
            deadline = time.monotonic() + 5
            while not (output / 'child.pid').exists():
                self.assertLess(time.monotonic(), deadline)
                time.sleep(.01)
            pid = int((output / 'child.pid').read_text())
            if mode == 'childsleep':
                self.broker.cancel(self.op, 'a', mode)
            result = self.wait(mode)
            self.assertEqual(result['state'], 'CANCELLED' if mode == 'childsleep' else 'SUCCEEDED')
            st = Path(f'/proc/{pid}/stat')
            deadline = time.monotonic() + 3
            while st.exists() and st.read_text().rsplit(')', 1)[1].split()[0] != 'Z':
                self.assertLess(time.monotonic(), deadline)
                time.sleep(.01)

    def test_revoke_external_unknown_and_await_cancellation(self):
        self.mode('sleep')
        self.start('revoke')
        with self.owner.transaction(self.owner.epoch) as db:
            grant = json.loads(db.execute("SELECT body FROM controls WHERE id='g'").fetchone()[0])
            grant['revoked'] = True
            db.execute("UPDATE controls SET body=? WHERE id='g'", (json.dumps(grant),))
        self.assertEqual(self.wait('revoke')['state'], 'CANCELLED')
        with self.assertRaisesRegex(IntegrationError, 'GRANT'):
            self.start('denied')
        with self.owner.transaction(self.owner.epoch) as db:
            grant['revoked'] = False
            db.execute("UPDATE controls SET body=? WHERE id='g'", (json.dumps(grant),))
            db.execute("INSERT INTO controls VALUES('run','run',?,1)", (json.dumps({'state': 'UNKNOWN'}),))
        with self.assertRaisesRegex(IntegrationError, 'EXTERNAL_UNKNOWN'):
            self.start('unknown')
        with self.owner.transaction(self.owner.epoch) as db:
            db.execute("DELETE FROM controls WHERE kind='run'")
        self.start('await')
        async def cancel_wait():
            t = asyncio.create_task(self.broker.wait(self.op, 'a', 'await'))
            await asyncio.sleep(.05)
            t.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await t
        asyncio.run(asyncio.wait_for(cancel_wait(), 5))
        self.assertEqual(self.wait('await')['state'], 'CANCELLED')

    def test_optional_timeout_with_source_not_inferred_policy(self):
        self.mode('sleep')
        self.cfg.update(timeout_seconds=.15, limit_source='controller fixture limit')
        self.publish()
        self.start()
        result = self.wait()
        self.assertEqual(result['state'], 'TIMED_OUT')
        self.assertEqual(result['limit_source'], 'controller fixture limit')

    def test_unknown_restart_conflict_and_attempt_fence(self):
        self.start()
        self.wait()
        with self.assertRaisesRegex(IntegrationError, 'ATTEMPT'):
            self.broker.status(self.op, 'wrong', 'effect')
        with self.owner.transaction(self.owner.epoch) as db:
            db.execute("UPDATE c_verification_effect SET state='INTENT',result=NULL WHERE id='effect'")
        self.broker.close()
        self.broker = VerificationBroker(self.box, self.router, report_parsers={'json-v1': lambda output, code: True})
        self.assertEqual(self.start()['state'], 'UNKNOWN')
        with self.assertRaisesRegex(IntegrationError, 'UNKNOWN_FENCE'):
            self.start('retry')
        with self.assertRaisesRegex(IntegrationError, 'CONFLICT'):
            self.broker.start(self.op, 'a', 'effect', check_id='check', checkout_receipt='snapshot', revision=self.parent)

    def test_stream_large_output_and_async_loop(self):
        self.mode('large')
        async def exercise():
            await self.broker.start_async(self.op, 'a', 'effect', check_id='check', checkout_receipt='snapshot', revision=self.revision)
            ticks = 0
            task = asyncio.create_task(self.broker.wait(self.op, 'a', 'effect'))
            while not task.done():
                ticks += 1
                await asyncio.sleep(.01)
            return await task, ticks
        result, ticks = asyncio.run(asyncio.wait_for(exercise(), 8))
        self.assertEqual(result['state'], 'SUCCEEDED')
        self.assertGreater(ticks, 0)
        self.assertGreater(result['artifacts']['stdout']['bytes'], 200000)

    def test_source_pin_tree_output_path_and_timeout_config(self):
        self.cfg['source_tree'] = '0' * 40
        self.publish()
        with self.assertRaisesRegex(IntegrationError, 'PIN_MISMATCH'):
            self.start()
        self.cfg['source_tree'] = self.git(self.source, 'rev-parse', 'HEAD^{tree}').strip()
        (self.root / 'link').symlink_to(self.root, target_is_directory=True)
        self.cfg['output_root'] = str(self.root / 'link' / 'out')
        self.publish()
        with self.assertRaisesRegex(IntegrationError, 'PATH'):
            self.start()
        self.cfg['output_root'] = str(self.root / 'outputs')
        self.cfg['timeout_seconds'] = .1
        self.publish()
        with self.assertRaisesRegex(IntegrationError, 'LIMIT_SOURCE'):
            self.start()
