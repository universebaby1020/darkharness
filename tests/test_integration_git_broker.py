"""Isolated Linux Git processes, no provider/network calls."""
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


@unittest.skipUnless(os.name == 'posix', 'Linux Git broker')
class GitBrokerTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name).resolve()
        self.repo = self.root / 'result'
        self.repo.mkdir()
        self.git('init', '-q')
        (self.repo / 'base').write_text('base')
        self.git('add', 'base')
        self.git('-c', 'user.name=fixture', '-c', 'user.email=fixture@actors.invalid', 'commit', '-qm', 'base')
        self.head = self.git('rev-parse', 'HEAD').strip()
        self.owner = Owner()
        self.box = Mailbox(self.owner)
        self.owner.grant(self.repo, local_git_write={'paths': ['smoke.txt']}, review_snapshot={'scratch': str(self.root / 'review'), 'max_bytes': 1024, 'max_files': 20})
        self.router = ApprovalRouter(self.owner, 'g', 's', 'r', 'run', self.repo)
        self.op = self.box.receive('s', 'r', 'peer', 'm', 'seat full task')['work']
        self.box.claim(self.op, 'a')
        self.broker = LocalGitBroker(self.box, self.router, 'Band builder', 'builder@actors.invalid', self.root)
        (self.repo / 'smoke.txt').write_bytes(b'DarkHarness smoke ok\n')

    def tearDown(self):
        self.owner.db.close()
        self.td.cleanup()

    def git(self, *args):
        return subprocess.check_output(['git', *args], cwd=self.repo, stderr=subprocess.PIPE).decode()

    def commit(self, identifier='effect'):
        return self.broker.commit(self.op, 'a', identifier, cwd=str(self.repo), paths=['smoke.txt'], message='Add DarkHarness smoke artifact', expected_head=self.head)

    def test_commit_preserves_index_and_unrelated_work_and_duplicate(self):
        (self.repo / 'base').write_text('unrelated staged')
        self.git('add', 'base')
        staged = self.git('ls-files', '--stage', '--', 'base')
        result = self.commit()
        self.assertEqual(result['state'], 'COMMITTED')
        self.assertEqual(self.git('show', result['commit'] + ':smoke.txt'), 'DarkHarness smoke ok\n')
        self.assertEqual(self.git('show', result['commit'] + ':base'), 'base')
        self.assertEqual(self.git('rev-parse', result['commit'] + '^').strip(), self.head)
        self.assertEqual(self.git('show', '-s', '--format=%an|%ae|%cn|%ce', result['commit']).strip(), 'Band builder|builder@actors.invalid|Band builder|builder@actors.invalid')
        self.assertEqual(self.git('ls-files', '--stage', '--', 'base'), staged)
        self.assertEqual(self.git('status', '--porcelain', '--', 'smoke.txt'), '')
        self.assertEqual(self.git('status', '--porcelain', '--', 'base'), 'M  base\n')
        self.assertEqual(self.commit(), result)
        self.assertEqual(self.git('rev-list', '--count', 'HEAD').strip(), '2')

    def test_clean_baseline_and_directory_scope(self):
        with self.owner.transaction(self.owner.epoch) as db:
            row = db.execute("SELECT body FROM controls WHERE kind='grant'").fetchone()
            grant = json.loads(row[0])
            grant['scope']['local_git_write'] = {'directories': ['.']}
            db.execute("UPDATE controls SET body=? WHERE kind='grant'", (json.dumps(grant),))
        result = self.commit()
        self.assertEqual(self.git('status', '--porcelain'), '')
        self.assertEqual(result['state'], 'COMMITTED')

    def test_captured_shell_denial_stays_denied(self):
        import asyncio
        from darkharness.integration.contract import PermissionRequest
        request = PermissionRequest(self.op, 'a', 'native', 'command', str(self.repo), ('/bin/bash', '-lc', 'git add -- smoke.txt && git commit --only -m "Add DarkHarness smoke artifact" -- smoke.txt'), (), False)
        self.assertFalse(asyncio.run(self.router.decide(request)))
        self.assertEqual(self.commit()['state'], 'COMMITTED')

    def test_no_hooks_filters_or_global_config(self):
        marker = self.root / 'executed'
        hook = self.repo / '.git/hooks/reference-transaction'
        hook.write_text('#!/bin/sh\ntouch ' + str(marker) + '\n')
        hook.chmod(0o755)
        (self.repo / '.gitattributes').write_text('*.txt filter=evil\n')
        self.git('config', 'filter.evil.clean', 'touch ' + str(marker))
        self.git('config', 'core.fsmonitor', 'touch ' + str(marker))
        self.commit()
        self.assertFalse(marker.exists())

    def test_scope_identity_symlink_and_cas_denied(self):
        for paths in [['base'], ['../outside'], [':(glob)*'], ['.git/config']]:
            with self.assertRaises(IntegrationError):
                self.broker.commit(self.op, 'a', 'bad', cwd=str(self.repo), paths=paths, message='m', expected_head=self.head)
        (self.repo / 'smoke.txt').unlink()
        (self.repo / 'smoke.txt').symlink_to(self.repo / 'base')
        with self.assertRaises(IntegrationError):
            self.commit()
        (self.repo / 'smoke.txt').unlink()
        (self.repo / 'smoke.txt').write_text('smoke')
        with self.assertRaises(IntegrationError):
            self.broker.commit(self.op, 'a', 'bad', cwd=str(self.root), paths=['smoke.txt'], message='m', expected_head=self.head)
        self.git('-c', 'user.name=f', '-c', 'user.email=f@actors.invalid', 'commit', '--allow-empty', '-qm', 'other')
        with self.assertRaisesRegex(IntegrationError, 'HEAD_CHANGED'):
            self.commit()

    def test_unknown_never_blindly_recommits(self):
        self.commit()
        self.owner.db.execute("UPDATE c_git_effect SET state='UNKNOWN',receipt=NULL")
        with self.assertRaisesRegex(IntegrationError, 'UNKNOWN'):
            self.commit()
        receipt = self.broker.reconcile('effect')
        self.assertEqual(receipt['state'], 'COMMITTED')
        self.assertEqual(self.commit(), receipt)
        self.assertEqual(self.git('rev-list', '--count', 'HEAD').strip(), '2')

    def test_review_independent_exact_no_overwrite(self):
        self.owner.grant(self.repo, local_git_write={'directories': ['.']}, review_snapshot={'scratch': str(self.repo / '.review')})
        result = self.commit()
        review = self.broker.snapshot(self.op, 'a', 'review', cwd=str(self.repo), revision=result['commit'], name='one')
        checkout = Path(review['path'])
        self.assertEqual(subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=checkout).decode().strip(), result['commit'])
        self.assertEqual((checkout / 'smoke.txt').read_bytes(), (self.repo / 'smoke.txt').read_bytes())
        self.assertNotEqual((checkout / '.git/objects').stat().st_ino, (self.repo / '.git/objects').stat().st_ino)
        self.assertFalse(subprocess.check_output(['git', 'remote'], cwd=checkout))
        blob = self.git('rev-parse', result['commit'] + ':smoke.txt').strip()
        source_object = self.repo / '.git/objects' / blob[:2] / blob[2:]
        review_object = checkout / '.git/objects' / blob[:2] / blob[2:]
        self.assertEqual(review_object.stat().st_nlink, 1)
        self.assertNotEqual(source_object.stat().st_ino, review_object.stat().st_ino)
        self.assertEqual(review['files'], 2)
        self.assertEqual(review['bytes'], len(b'base') + len(b'DarkHarness smoke ok\n'))
        with self.assertRaises(IntegrationError):
            self.broker.snapshot(self.op, 'a', 'different', cwd=str(self.repo), revision=result['commit'], name='one')
        with self.assertRaises(IntegrationError):
            self.broker.snapshot(self.op, 'a', 'bad', cwd=str(self.repo), revision='HEAD', name='two')

    def test_revoked_and_wrong_attempt_denied(self):
        with self.assertRaises(IntegrationError):
            self.broker.commit(self.op, 'stale', 'bad', cwd=str(self.repo), paths=['smoke.txt'], message='m', expected_head=self.head)
        self.owner.db.execute("UPDATE controls SET body=? WHERE kind='grant'", (json.dumps({'revoked': True}),))
        with self.assertRaises(IntegrationError):
            self.commit()
