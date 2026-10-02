"""Synthetic, in-memory Git fixtures; no commits, auth, or provider calls."""
import contextlib
import io
import unittest
from unittest.mock import patch
from tools import public_guard as g

PRIVATE = {"account_handles": ["synthetic-person"],
           "band_uuids": ["11111111-2222-4333-8444-555555555555"]}


class PublicGuardTests(unittest.TestCase):
    def test_forward_history_not_only_tip_and_all_metadata(self):
        def git(*args):
            if args[:2] == ("rev-parse", "--verify"):
                return ("a" * 40 if args[2].startswith("base") else "b" * 40).encode() + b"\n"
            if args[0] == "merge-base":
                return b"a" * 40 + b"\n"
            if args[0] == "rev-list":
                return b"c" * 40 + b"\n" + b"b" * 40 + b"\n"
            if args[0] == "ls-tree":
                return b"100644 blob " + b"d" * 40 + b"\tfixture.txt\0"
            if args[:2] == ("cat-file", "blob"):
                return (PRIVATE['band_uuids'][0] + "\n").encode()
            if args[:2] == ("cat-file", "commit"):
                if args[2] == "b" * 40:
                    return b"author Safe <safe@example.invalid> 0 +0000\ncommitter Safe <safe@example.invalid> 0 +0000\n\nclean\n"
                return b"author synthetic-person <safe@example.invalid> 0 +0000\ncommitter Other <personal@" + b"example.com> 0 +0000\n\nsynthetic-person\n"
            raise AssertionError(args)
        with patch.object(g, 'git', side_effect=git):
            rows = g.scan_ref('base', 'target', PRIVATE)
        self.assertEqual({r['commit'] for r in rows}, {'c' * 40, 'b' * 40})
        self.assertTrue({'account-handle', 'personal-email', 'real-band-uuid'} <= {r['type'] for r in rows})
        self.assertTrue(any(r['file'] == '<commit-message>' for r in rows))
        output = str(rows)
        for value in PRIVATE['account_handles'] + PRIVATE['band_uuids']:
            self.assertNotIn(value, output)
        self.assertTrue(all(set(r) == {'type', 'commit', 'file', 'line'} for r in rows))

    def test_names_and_multiple_lines(self):
        data = b'synthetic-person\nsynthetic-person\npersonal@' + b'example.com\n'
        rows = g.records('<author>', data, PRIVATE, 'a' * 40)
        self.assertEqual([r['line'] for r in rows], [1, 2, 3])
        self.assertFalse(g.records('safe.txt', b'Safe <safe@example.invalid>', PRIVATE))

    def test_private_path_and_file_name_never_echo_value(self):
        rows = g.records('synthetic-person/file.txt', b'/' + b'home/' + b'synthetic-person/file\n', PRIVATE)
        self.assertNotIn('synthetic-person', str(rows))
        self.assertIn('private-linux-path', {r['type'] for r in rows})

    def test_room_exception_only_assignment_not_bearer(self):
        token = 'Bearer ' + 'x' * 22 + '1'
        text = ('APP_' + 'TOKEN' + '=synthetic\n' + token).encode()
        self.assertEqual(g.findings('room.json', text), ['bearer-token'])
        self.assertIn('env-assignment', g.findings('config.json', text))

    def test_nonregular_and_unmerged_entries_block(self):
        with patch.object(g, 'git', return_value=b'120000 ' + b'a' * 40 + b' 0\tlink\0' + b'100644 ' + b'b' * 40 + b' 2\tconflict.txt\0'):
            rows = g.scan_tree(None, PRIVATE)
        self.assertEqual({r['type'] for r in rows}, {'nonregular-index-entry', 'unmerged-index-entry'})

    def test_config_outside_repo_required(self):
        from pathlib import Path
        with self.assertRaisesRegex(ValueError, 'PRIVATE_CONFIG_OUTSIDE_REPO_REQUIRED'):
            g.load_private(Path.cwd() / 'tools/publication-private.example.json', Path.cwd())

    def test_existing_hook_no_args_uses_external_env_and_enforces(self):
        from pathlib import Path
        hook = Path('.githooks/pre-commit').read_text()
        self.assertIn('exec python -B tools/public_guard.py', hook)
        env = {'DH_PUBLIC_PRIVATE_CONFIG': '/external/private.json', 'DH_PUBLIC_MESSAGE_FILE': '/external/message.txt'}
        for dirty in (False, True):
            row = {'type': 'account-handle', 'commit': 'INDEX', 'file': 'fixture.txt', 'line': 1}
            with patch.dict('os.environ', env), patch('sys.argv', ['tools/public_guard.py']), patch.object(g, 'load_private', return_value=PRIVATE) as loader, patch.object(g, 'git', return_value=b'.\n'), patch.object(g, 'scan_tree', return_value=[row] if dirty else []), patch.object(Path, 'read_bytes', return_value=b'Clean proposed message'), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(g.main(), int(dirty))
            self.assertEqual(str(loader.call_args.args[0]).replace('\\', '/'), '/external/private.json')

    def test_missing_config_or_message_denies_no_arg_hook(self):
        with patch.dict('os.environ', {}, clear=True), patch('sys.argv', ['guard']), patch.object(g, 'git', return_value=b'.\n'), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(g.main(), 2)
        with patch.dict('os.environ', {'DH_PUBLIC_PRIVATE_CONFIG': '/external/private.json'}, clear=True), patch('sys.argv', ['guard']), patch.object(g, 'git', return_value=b'.\n'), patch.object(g, 'load_private', return_value=PRIVATE), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(g.main(), 2)

    def test_dockerfile_assignment_uses_official_config_logic(self):
        self.assertIn('env-assignment', g.findings('Dockerfile', ('APP_' + 'TOKEN' + '=synthetic').encode()))

    def test_precommit_identity_and_message_are_checked(self):
        from pathlib import Path
        with patch('sys.argv', ['guard', '--private-config', '/external/private.json', '--message-file', '/external/message.txt']), patch.object(g, 'load_private', return_value=PRIVATE), patch.object(g, 'scan_tree', return_value=[]), patch.object(g, 'git', return_value=b'synthetic-person <safe@example.invalid>'), patch.object(Path, 'read_bytes', return_value=b'synthetic-person'), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(g.main(), 1)
        self.assertIn('<commit-message>', out.getvalue())
        self.assertNotIn('synthetic-person', out.getvalue())

    def test_cli_denies_ref_with_any_failure(self):
        with patch.object(g, 'load_private', return_value=PRIVATE), patch.object(g, 'git', return_value=b'.\n'), patch.object(g, 'scan_ref', return_value=[{'type':'account-handle','commit':'a'*40,'file':'fixture.txt','line':1}]), patch('sys.argv', ['guard', '--private-config', '/synthetic/private.json', '--ref', 'target']), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(g.main(), 1)
        self.assertIn('PUBLIC_GUARD_DENY', out.getvalue())
        self.assertNotIn('synthetic-person', out.getvalue())


if __name__ == '__main__':
    unittest.main()
