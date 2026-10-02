"""Synthetic publication guard controls; no account/auth/provider operations."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools import public_guard as g
from tools import prepare_design_pack as prep


def config():
    return {
        'schema_version': 'darkharness-publication-private-2',
        'account_handles': ['synthetic-person'],
        'band_uuids': ['11111111-2222-4333-8444-555555555555'],
        'project_names': ['synthetic-private-project'],
        'local_filenames': ['synthetic-private-attachment.zip'],
        'account_emails': ['synthetic-person@' + 'example.com'],
        'account_display_names': ['Synthetic Private Name'],
        'development_participant_ids': ['66666666-7777-4888-8999-000000000000'],
        'design_pack_replacements': {
            'THIRD_PARTY_NOTICES.md': [{'source': 'synthetic-private-attachment.zip', 'replacement': 'selected input'}],
            'governance/COMMON_GUIDANCE_SCOPE_KO.md': [{'source': 'synthetic-private-project', 'replacement': 'other project'}],
        },
    }


class PublicationRepairTests(unittest.TestCase):
    def test_all_private_categories_literal_not_regex_and_safe_paths(self):
        private = config()
        private['project_names'] = ['synthetic[project]+name']
        for key, kind in g.PRIVATE_TYPES.items():
            value = private[key][0]
            rows = g.records('safe.txt', value.encode(), private)
            self.assertIn(kind, {r['type'] for r in rows})
            self.assertNotIn(value, json.dumps(rows))
            self.assertNotIn(value, g.safe_path(value + '/file.txt', private))
        self.assertFalse(g.records('safe.txt', b'syntheticpprojectname', private))

    def test_wsl_paths_unicode_and_noreply(self):
        paths = ['/' + 'home/synthetic/file', '/' + 'home/사용자/file',
                 '/' + 'mnt/c/Users/synthetic/file', '/' + 'mnt/C/Users/사용자/file',
                 'C:' + '/Users/사용자/file']
        for text in paths:
            self.assertTrue(g.findings('safe.txt', text.encode()))
            self.assertNotIn(text, g.safe_path(text, config()))
        for domain in ('users.noreply.github.com', 'example.com', 'example.invalid.com'):
            self.assertIn('personal-email', g.findings('<author>', ('person@' + domain).encode()))
        self.assertFalse(g.findings('<author>', b'Person <person@example.invalid>'))

    def test_license_exemption_exact_path_and_entire_blob_only(self):
        rel = 'docs/design/ui-design-engineering-clean/licenses/shadcn-LICENSE.md'
        data = Path(rel).read_bytes()
        self.assertEqual(hashlib.sha256(data).hexdigest(), g.LICENSE_BLOBS[rel])
        private = config()
        private['account_handles'] = ['shadcn']  # Public upstream attribution, not local PII.
        self.assertFalse(g.records(rel, data, private))
        for path, body in ((rel, data + b'\n'), ('copied-license.txt', data), ('<commit-message>', data)):
            self.assertIn('account-handle', {r['type'] for r in g.records(path, body, private)})
        private['account_display_names'] = ['shadcn']
        self.assertIn('account-display-name', {r['type'] for r in g.records(rel, data, private)})

    def test_pinned_attribution_never_exempts_email_or_secret(self):
        rel = next(iter(g.LICENSE_BLOBS))
        data = ('synthetic-person\nprivate@' + 'example.com\nBearer ' + 'x' * 22 + '1').encode()
        with patch.dict(g.LICENSE_BLOBS, {rel: hashlib.sha256(data).hexdigest()}):
            kinds = {r['type'] for r in g.records(rel, data, config())}
        self.assertNotIn('account-handle', kinds)
        self.assertTrue({'personal-email', 'bearer-token'} <= kinds)

    def test_external_config_version_fields_and_hash(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'private.json'
            repo = Path(td) / 'repo'
            raw = (json.dumps(config(), indent=2) + '\n').encode()
            path.write_bytes(raw)
            loaded = g.load_private(path.resolve(), repo)
            self.assertEqual(loaded['_sha256'], hashlib.sha256(raw).hexdigest())
            for field in g.PRIVATE_TYPES:
                value = config()
                del value[field]
                path.write_text(json.dumps(value), encoding='utf-8')
                with self.assertRaisesRegex(ValueError, 'SCHEMA_INVALID'):
                    g.load_private(path.resolve(), repo)
            value = config()
            value['schema_version'] = 'darkharness-publication-private-1'
            path.write_text(json.dumps(value), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'SCHEMA_INVALID'):
                g.load_private(path.resolve(), repo)

    def test_invalid_replacements_and_empty_required_lists_fail_closed(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'private.json'
            for modify in (
                lambda c: c.update(project_names=[]),
                lambda c: c.update(design_pack_replacements={}),
                lambda c: c['design_pack_replacements']['THIRD_PARTY_NOTICES.md'][0].update(replacement='synthetic-person'),
                lambda c: c['local_filenames'].append('unmapped-private-input.zip'),
                lambda c: c['account_display_names'].append(''),
            ):
                value = config()
                modify(value)
                path.write_text(json.dumps(value), encoding='utf-8')
                with self.assertRaises(ValueError):
                    g.load_private(path.resolve(), Path(td) / 'repo')

    def test_preparation_missing_private_config_has_no_copy_effect(self):
        with patch.dict('os.environ', {}, clear=True), patch('sys.argv', ['prepare', '--package', '/synthetic/package']), patch.object(prep.shutil, 'copytree') as copy, contextlib.redirect_stderr(io.StringIO()) as out:
            with self.assertRaises(SystemExit) as error:
                prep.main()
        self.assertEqual(error.exception.code, 2)
        copy.assert_not_called()
        self.assertIn('values suppressed', out.getvalue())

    def test_substitution_retained_exact_and_missing_input_denied(self):
        pairs = [{'source': 'synthetic-private-attachment.zip', 'replacement': 'selected input'},
                 {'source': 'synthetic-private-project', 'replacement': 'other project'}]
        self.assertEqual(prep.substitute('synthetic-private-attachment.zip / synthetic-private-project', pairs), 'selected input / other project')
        with self.assertRaisesRegex(ValueError, 'SOURCE_LITERAL_MISSING'):
            prep.substitute('changed supplement', pairs)

    def test_mandates_optional_uiqa_and_dispatched_task_boundary(self):
        from darkharness.integration.artifacts import render_mandate
        for role in ('coordinator', 'builder', 'reviewer'):
            text = render_mandate('dh ' + role, role, 'DarkHarness', 'configured-model', 'high')
            self.assertIn('any UI quality helper granted for this run', text)
            self.assertIn("final report ends the current dispatched task", text)
            self.assertNotIn('final report ends this run', text)
            self.assertIn('must not reply', text)
            self.assertIn('Never echo credential or token values', text)

    def test_uiqa_public_links_resolve_and_private_evidence_is_not_a_link(self):
        import re
        doc = Path('docs/uiqa-checker.md')
        text = doc.read_text(encoding='utf-8')
        self.assertNotIn('`evidence/', text)
        links = re.findall(r'\[[^\]]+\]\(([^)]+)\)', text)
        self.assertGreater(len(links), 0)
        for rel in links:
            self.assertTrue((doc.parent / rel).is_file(), rel)

    def test_c7_notice_is_consistent_and_reproduction_script_repairs_it(self):
        notice = Path('docs/design/ui-design-engineering-clean/THIRD_PARTY_NOTICES.md').read_text(encoding='utf-8')
        self.assertIn('원 작성 문안은 이 repo의 MIT LICENSE를 따른다', notice)
        self.assertNotIn('이 파일로 사용자 원문의 저작권자·배포조건을 새로 발명하지 않는다', notice)
        self.assertIn('원 작성 문안은 이 repo의 MIT LICENSE를 따른다', Path(prep.__file__).read_text(encoding='utf-8'))

    def test_cli_reports_script_and_list_hashes_separately(self):
        private = config()
        private['_sha256'] = 'e' * 64
        with patch.object(g, 'load_private', return_value=private), patch.object(g, 'git', return_value=b'.\n'), patch.object(g, 'scan_ref', return_value=[]), patch('sys.argv', ['guard', '--private-config', '/synthetic/private.json', '--ref', 'candidate']), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(g.main(), 0)
        report = json.loads(out.getvalue())
        self.assertEqual(report['private_config_sha256'], 'e' * 64)
        self.assertEqual(report['guard_sha256'], hashlib.sha256(Path(g.__file__).read_bytes()).hexdigest())
        self.assertNotEqual(report['guard_sha256'], report['private_config_sha256'])


if __name__ == '__main__':
    unittest.main()
