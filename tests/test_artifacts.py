"""Track D mandate and controller hygiene unit checks; not runtime qualification."""
import json
import os
from pathlib import Path
import sys
import unittest

from darkharness.integration.artifacts import render_mandate, official_call

OFFICIAL = Path(os.environ.get('DH_OFFICIAL_ROOT', str(Path.cwd().parent / 'inputs/handoff-20261001/DarkHarness_Opus55_Codex_Handoff_20261001/official/dark-factory-803560d2a678')))


class MandateHygieneTests(unittest.TestCase):
    def test_same_generic_hygiene_for_every_role(self):
        for role in ('coordinator', 'builder', 'reviewer'):
            text = render_mandate('dh ' + role, role, 'DarkHarness (Band SDK Codex)', 'synthetic-model', 'high')
            self.assertIn('Harness: DarkHarness (Band SDK Codex)', text)
            self.assertIn("The coordinator's final report ends the current dispatched task.", text)
            self.assertIn('unless reporting a concrete defect', text)
            self.assertIn('Do not start acknowledgement or confirmation loops.', text)
            self.assertIn('Never echo credential or token values in commands, outputs or messages', text)
            self.assertIn('Never assign secrets in Dockerfiles or committed configuration.', text)
            self.assertEqual(text.count('## UI quality\n'), 1)
            self.assertEqual(len(text.split('## UI quality\n')[1].split('\n\n')[0].splitlines()), 1)

    @unittest.skipUnless((OFFICIAL / 'harness/check.py').is_file(), 'trusted official checkout unavailable')
    def test_three_official_track_vocabularies_zero(self):
        # Genuine organizer vocabulary; no local imitation and no temp repo needed.
        texts = [render_mandate('dh ' + r, r, 'DarkHarness (Band SDK Codex)', 'synthetic-model', 'high') for r in ('coordinator', 'builder', 'reviewer')]
        result = official_call(OFFICIAL,
            'import json,sys; from harness import vocabulary; from harness.cli import TRACKS; texts=json.loads(sys.argv[1]); print(json.dumps({t:[term for text in texts for kind,term in vocabulary.terms_in(text) if term in set(vocabulary.for_track(t))] for t in TRACKS}))',
            json.dumps(texts), python=sys.executable)
        self.assertEqual(result, {'toy': [], 'tablekeeper': [], 'pocketful': []})


class ControllerHygieneTests(unittest.TestCase):
    @unittest.skipUnless((OFFICIAL / 'harness/check.py').is_file(), 'trusted official checkout unavailable')
    def test_room_assignment_exception_bearer_still_blocked(self):
        from tools.submission_hygiene import official_policy, scan_text
        policy = official_policy(OFFICIAL)
        raw = ('APP_' + 'TOKEN' + '=synthetic\nBearer ' + 'x' * 22 + '1').encode()
        self.assertEqual([r['type'] for r in scan_text('room.json', raw, policy, room=True)], ['bearer-token'])
        self.assertEqual({r['type'] for r in scan_text('Dockerfile', raw, policy)}, {'env-assignment', 'bearer-token'})
        self.assertNotIn('synthetic', str(scan_text('config.json', raw, policy)))

    def test_export_boundaries_and_exact_api_messages(self):
        from tools.submission_hygiene import boundary
        room = {'scope': 'full', 'messages': [{'id': 'initial', 'content': 'task'}, {'messageId': 'final', 'content': 'report'}]}
        good = boundary(room, 'initial', 'final', room['messages'])
        self.assertTrue(all(good[k] for k in ('full_scope', 'initial_present', 'final_present', 'ordered', 'unique_ids')))
        self.assertEqual(good['api_compare'], 'MATCH')
        self.assertFalse(boundary(room, 'missing', 'final')['initial_present'])
        self.assertFalse(boundary(room, 'final', 'initial')['ordered'])
        self.assertEqual(boundary(room, 'initial', 'final', [])['api_compare'], 'MISMATCH')
        room['scope'] = 'filtered'
        self.assertFalse(boundary(room, 'initial', 'final')['full_scope'])
        room['messages'].append(room['messages'][0])
        self.assertFalse(boundary(room, 'initial', 'final')['unique_ids'])
        self.assertFalse(boundary([], 'initial', 'final')['full_scope'])

    def test_exact_clone_plan_no_execution(self):
        from tools.exact_final_check import plan
        from unittest.mock import patch
        with patch.object(Path, 'exists', return_value=False):
            steps = plan(Path.cwd(), 'a' * 40, Path.cwd().parent / 'synthetic-new-clone', OFFICIAL, 'toy', Path.cwd().parent / 'synthetic-new-output')
            self.assertIn('--no-local', steps[0]['argv'])
            self.assertEqual(steps[1]['argv'][-2:], ['--detach', 'a' * 40])
            self.assertIn('check', steps[3]['argv'])
            self.assertIn('--all', steps[4]['argv'])
            self.assertIn('isolated', steps[4]['argv'])
            with self.assertRaisesRegex(ValueError, 'EXACT_FULL_SHA_REQUIRED'):
                plan(Path.cwd(), 'HEAD', Path.cwd().parent / 'synthetic-new-clone', OFFICIAL, 'toy', Path.cwd().parent / 'synthetic-new-output')
            with self.assertRaisesRegex(ValueError, 'EXTERNAL_NEW_DESTINATION'):
                plan(Path.cwd(), 'a' * 40, Path.cwd() / 'bad-clone', OFFICIAL, 'toy', Path.cwd().parent / 'synthetic-new-output')

    def test_all_text_not_only_official_suffixes_and_no_auth_read(self):
        from tools.submission_hygiene import scan_repo
        from unittest.mock import patch
        root = Path.cwd() / 'synthetic-tree'
        odd = root / 'nested' / 'no-extension'
        auth = root / 'agents.json'
        policy = {'patterns': [('fixture-shape', 'synthetic-detection')], 'config_only': [], 'config_suffixes': [], 'config_names': []}
        def children(path):
            if path == root:
                return [root / 'nested', auth]
            if path == root / 'nested':
                return [odd]
            return []
        def read(path):
            self.assertEqual(path, odd)  # Opening auth would fail this assertion.
            return b'synthetic-detection'
        with patch.object(Path, 'iterdir', children), patch.object(Path, 'is_symlink', return_value=False), patch.object(Path, 'is_junction', return_value=False, create=True), patch.object(Path, 'is_dir', lambda p: p == root / 'nested'), patch.object(Path, 'is_file', return_value=True), patch.object(Path, 'read_bytes', read):
            rows, counts = scan_repo(root, policy)
        self.assertEqual({r['type'] for r in rows}, {'private-file', 'fixture-shape'})
        self.assertEqual(counts['text_files'], 1)
        self.assertEqual(counts['private_paths_not_read'], 1)


if __name__ == '__main__':
    unittest.main()
