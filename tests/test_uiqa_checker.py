"""COMPONENT fake Docker tests only; never real Docker/provider/room calls."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

UIQA = Path(__file__).resolve().parents[1] / 'tools/uiqa'
sys.path.insert(0, str(UIQA))
import checker
import common
from browser_worker import validate_flow
from darkharness.integration.report_criteria import JsonReportCriteria

REVISION = 'a' * 40
FLOW = {'spec_reference': 'fixture specification: visible heading', 'author': 'Reviewer fixture',
        'viewports': [{'width': 375, 'height': 812}],
        'scenarios': [{'id': 'home', 'path': '/', 'steps': [{'action': 'visible', 'selector': 'h1'}]}]}


class FakeDocker:
    def __init__(self, scratch, mode='pass'):
        self.scratch, self.mode, self.calls = scratch, mode, []
        self.objects = {'container': set(), 'network': set(), 'image': set()}

    def __call__(self, *args):
        self.calls.append(args)
        output, code = b'', 0
        if args[0] == 'inspect':
            output = json.dumps({'running': self.mode != 'startup-fail', 'status': 'exited' if self.mode == 'startup-fail' else 'running', 'exit_code': 1 if self.mode == 'startup-fail' else 0}).encode()
        elif args[:2] == ('image', 'inspect'):
            output = common.RUNNER_IMAGE.encode()
        elif args[0] == 'build':
            (self.scratch / 'app-image-id').write_text('sha256:app')
            self.objects['image'].add('sha256:app')
            if self.mode == 'build-fail': code = 1
        elif args[:2] == ('network', 'create'):
            self.objects['network'].add('network')
        elif args[0] == 'run':
            self.objects['container'].add('container-' + str(len(self.calls)))
            if '-d' not in args:
                status = 'FAIL' if self.mode == 'findings' else 'PARTIAL' if self.mode == 'incomplete' else 'PASS'
                report = {'execution': 'COMPLETED', 'checks': [{'id': 'scan', 'status': status}],
                          'findings': [{'kind': 'candidate'}] if status != 'PASS' else [], 'artifacts': [],
                          'tools': {'playwright': common.PLAYWRIGHT_VERSION, 'axe_runtime': '4.10.3'}, 'viewports': FLOW['viewports']}
                if self.mode == 'container-error':
                    code = 1
                    report['execution'] = 'ERROR'
                    report['checks'][0]['status'] = 'ERROR'
                if self.mode != 'no-candidate':
                    common.atomic_json(self.scratch / 'candidate.json', report)
                if self.mode == 'symlink':
                    (self.scratch / 'evil').symlink_to(self.scratch / 'candidate.json')
        elif args[0] == 'ps' or args[:2] in {('network', 'ls'), ('image', 'ls')}:
            typ = 'container' if args[0] == 'ps' else args[0]
            output = '\n'.join(self.objects[typ]).encode()
            if self.mode == 'cleanup-unknown': code = 1
        elif args[0] == 'rm' or args[:2] in {('network', 'rm'), ('image', 'rm')}:
            typ = 'container' if args[0] == 'rm' else args[0]
            if self.mode != 'residual': self.objects[typ].clear()
        return subprocess.CompletedProcess(args, code, output, b'')


class CheckerTests(unittest.TestCase):
    def setUp(self):
        # All test-generated files stay within the owned evidence subtree.
        area = Path(__file__).resolve().parents[1] / 'evidence/wo-dh0-02r3-uiqa/local-tests'
        area.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=area)
        self.root = Path(self.temp.name)
        self.checkout = self.root / 'checkout'
        (self.checkout / 'stage-2').mkdir(parents=True)
        (self.checkout / 'stage-2/Dockerfile').write_text('FROM fixture')
        (self.checkout / 'flows').mkdir()
        (self.checkout / 'flows/ui.json').write_text(json.dumps(FLOW))
        self.axe = self.root / 'axe.min.js'
        self.axe.write_text('fixed fixture, not real axe')
        self.out = self.root / 'output/reports'
        self.out.parent.mkdir()
        self.args = checker.parser().parse_args([str(self.checkout), str(self.out), '--stage', '2', '--port', '8000', '--flow', 'flows/ui.json', '--axe', str(self.axe), '--axe-version', '4.10.3', '--axe-sha256', common.sha256(self.axe)])
        self.args.handle_signals = False

    def tearDown(self):
        self.temp.cleanup()

    def run_fake(self, mode='pass'):
        def factory(scratch):
            self.docker = FakeDocker(scratch, mode)
            return self.docker
        with patch.object(checker.subprocess, 'check_output', return_value=REVISION.encode()), patch.object(checker.os, 'getuid', create=True, return_value=1000), patch.object(checker.os, 'getgid', create=True, return_value=1000):
            return checker.execute(self.args, factory)

    def accepted(self):
        return JsonReportCriteria().parse_request(self.out, 0, {'request': {'revision': REVISION}, 'config': {'report_files': ['uiqa.json'], 'report_criteria': common.criteria()}})

    def report(self):
        return json.loads((self.out / 'uiqa.json').read_text())

    def test_success_cleanup_and_mount_contract(self):
        self.assertEqual(self.run_fake(), 0)
        self.assertTrue(self.accepted())
        self.assertTrue(self.report()['cleanup']['verified'])
        run = next(call for call in self.docker.calls if call[0] == 'run' and '-d' not in call)
        mounts = [run[i+1] for i, a in enumerate(run) if a == '--mount']
        self.assertEqual(len(mounts), 4)
        self.assertEqual(sum(m.endswith(',readonly') for m in mounts), 3)
        self.assertFalse(any(str(self.out) in m for m in mounts))
        self.assertIn(common.RUNNER_IMAGE, run)
        network = next(c for c in self.docker.calls if c[:2] == ('network', 'create'))
        self.assertIn('--internal', network)
        self.assertIn('--label', network)
        self.assertEqual(self.report()['effects'], {'docker': True, 'network': True})
        self.assertEqual(self.report()['visual_review'], 'NOT_PERFORMED')

    def test_official_runner_identity_and_private_owner_handoff(self):
        self.run_fake()
        run = next(c for c in self.docker.calls if c[0] == 'run' and '-d' not in c)
        self.assertNotIn('--user', run)
        self.assertNotIn('--cap-drop', run)
        self.assertFalse(any(a.startswith('HOME=') for a in run))
        self.assertNotIn('--privileged', run)
        self.assertIn('--read-only', run)
        self.assertIn('no-new-privileges', run)
        self.assertEqual(run[run.index('--host-uid') + 1], '1000')
        self.assertEqual(run[run.index('--host-gid') + 1], '1000')
        self.assertIn('TMPDIR=/scratch/tmp', run)

    def test_host_diagnostic_class_and_phase_only(self):
        self.run_fake('build-fail')
        diagnostic = self.report()['diagnostic']
        self.assertEqual(diagnostic, {'phase': 'app_build', 'exception_class': 'RuntimeError'})
        self.assertEqual(json.loads((self.docker.scratch / 'host-diagnostic.json').read_text()), diagnostic)
        self.assertNotIn('DOCKER_COMMAND_FAILED', json.dumps(self.report()))

    def test_browser_diagnostic_redaction_and_owner_return(self):
        import browser_worker
        argv = ['flow', 'axe', str(self.root), 'http://app:8080', '--host-uid', '1000', '--host-gid', '1000']
        def failed(flow, axe, scratch, base, progress):
            progress['phase'] = 'browser_launch'
            raise RuntimeError('UNTRUSTED_EXCEPTION_SENTINEL')
        with patch.object(browser_worker, 'run', failed), patch.object(browser_worker.os, 'chown', create=True) as chown:
            self.assertEqual(browser_worker.main(argv), 1)
        candidate = json.loads((self.root / 'candidate.json').read_text())
        self.assertEqual(candidate['diagnostic'], {'phase': 'browser_launch', 'exception_class': 'RuntimeError'})
        self.assertNotIn('UNTRUSTED_EXCEPTION_SENTINEL', json.dumps(candidate))
        self.assertTrue(chown.called)
        self.assertTrue(all(c.kwargs == {'follow_symlinks': False} and c.args[1:] == (1000, 1000) for c in chown.call_args_list))

    def test_startup_exit_cause_not_browser_initialization_contamination(self):
        self.assertEqual(self.run_fake('startup-fail'), 1)
        report = self.report()
        self.assertEqual(report['app_state'], {'running': False, 'status': 'exited', 'exit_code': 1})
        self.assertEqual(report['diagnostic']['phase'], 'app_startup_state')
        self.assertFalse(any(c[0] == 'run' and '-d' not in c for c in self.docker.calls))
        spec = importlib.util.spec_from_file_location('uiqa_controller_tests', UIQA / 'run_controller_tests.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertTrue(module.expected_failure_cause('startup-fail', report))
        for phase in ('tool_import', 'playwright_start', 'browser_launch', 'axe_scan'):
            report['diagnostic']['phase'] = phase
            self.assertFalse(module.expected_failure_cause('startup-fail', report))
        report['diagnostic']['phase'] = 'navigation'
        report['diagnostic']['exception_class'] = 'Error'
        self.assertTrue(module.expected_failure_cause('startup-fail', report))
        report['app_state']['exit_code'] = 0
        self.assertFalse(module.expected_failure_cause('startup-fail', report))
        self.assertFalse(module.expected_failure_cause('missing-axe', report))

    def test_real_phase_tracking_with_fake_browser_launch_failure(self):
        from types import SimpleNamespace
        import browser_worker
        def launch(**kwargs):
            raise RuntimeError('PRIVATE_MESSAGE_MUST_NOT_ESCAPE')
        class Playwright:
            def __enter__(self): return SimpleNamespace(chromium=SimpleNamespace(launch=launch))
            def __exit__(self, *args): pass
        module = SimpleNamespace(sync_playwright=Playwright, expect=None)
        argv = [str(self.checkout / 'flows/ui.json'), str(self.axe), str(self.root), 'http://app:8080', '--host-uid', '1000', '--host-gid', '1000']
        with patch.dict(sys.modules, {'playwright': SimpleNamespace(), 'playwright.sync_api': module}), patch('importlib.metadata.version', return_value=common.PLAYWRIGHT_VERSION), patch.object(browser_worker.os, 'chown', create=True):
            self.assertEqual(browser_worker.main(argv), 1)
        diagnostic = json.loads((self.root / 'browser-diagnostic.json').read_text())
        self.assertEqual(diagnostic, {'phase': 'browser_launch', 'exception_class': 'RuntimeError'})
        self.assertNotIn('PRIVATE_MESSAGE_MUST_NOT_ESCAPE', (self.root / 'candidate.json').read_text())

    def test_browser_owner_return_preserves_private_mode_and_denies_links(self):
        import browser_worker
        path = self.root / 'private.json'
        common.atomic_json(path, {'fixture': True})
        before = path.stat().st_mode
        with patch.object(browser_worker.os, 'chown', create=True):
            browser_worker.return_scratch_ownership(self.root, 1000, 1000)
        self.assertEqual(path.stat().st_mode, before)
        import os
        os.link(path, self.root / 'hardlinked')
        with patch.object(browser_worker.os, 'chown', create=True) as chown:
            with self.assertRaises(ValueError):
                browser_worker.return_scratch_ownership(self.root, 1000, 1000)
            chown.assert_not_called()

    def test_negative_control_extends_toy_dockerfile_not_runner_id(self):
        from types import SimpleNamespace
        spec = importlib.util.spec_from_file_location('uiqa_controller_tests', UIQA / 'run_controller_tests.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for mode in ('overflow', 'axe-violation'):
            root = self.root / mode
            stage = root / 'result/stage-2'
            stage.mkdir(parents=True)
            original = 'FROM python:3.12-slim\nWORKDIR /app\nCMD ["python", "server.py"]\n'
            (stage / 'Dockerfile').write_text(original)
            args = SimpleNamespace(result_repo='readonly', revision=REVISION, flow=str(self.checkout / 'flows/ui.json'))
            with patch.object(module, 'command'):
                result, paths = module.prepare_copy(args, root, mode)
            dockerfile = (stage / 'Dockerfile').read_text()
            self.assertTrue(dockerfile.startswith(original))
            self.assertNotIn('FROM sha256:', dockerfile)
            self.assertIn('http.server', dockerfile)
            self.assertEqual(json.loads((result / 'uiqa-flow.json').read_text())['viewports'], FLOW['viewports'])
            self.assertIn('stage-2/uiqa-control.html', paths)

    def test_completed_findings_exit_zero_but_rejected(self):
        self.assertEqual(self.run_fake('findings'), 0)
        self.assertEqual(self.report()['execution'], 'COMPLETED')
        self.assertFalse(self.accepted())

    def test_incomplete_is_not_pass(self):
        self.assertEqual(self.run_fake('incomplete'), 0)
        self.assertFalse(self.accepted())

    def test_missing_axe_known_error_after_verified_cleanup(self):
        self.axe.unlink()
        self.assertEqual(self.run_fake(), 1)
        self.assertEqual(self.report()['execution'], 'ERROR')
        self.assertTrue(self.report()['cleanup']['verified'])
        self.assertFalse(any(c[0] in {'build', 'run'} for c in self.docker.calls))
        self.assertFalse(self.accepted())

    def test_build_failure_cleanup(self):
        self.assertEqual(self.run_fake('build-fail'), 1)
        self.assertEqual(self.report()['execution'], 'ERROR')
        self.assertFalse(any(self.docker.objects.values()))

    def test_container_error_known_report(self):
        self.assertEqual(self.run_fake('container-error'), 1)
        self.assertEqual(self.report()['execution'], 'ERROR')

    def test_missing_candidate_error_not_empty_pass(self):
        self.assertEqual(self.run_fake('no-candidate'), 1)
        self.assertFalse(self.accepted())

    def test_cleanup_unknown_no_report(self):
        self.assertEqual(self.run_fake('cleanup-unknown'), 1)
        self.assertFalse(self.out.exists())
        self.assertTrue((self.docker.scratch / 'cleanup-unknown.json').exists())

    def test_successful_remove_but_residual_no_report(self):
        self.assertEqual(self.run_fake('residual'), 1)
        self.assertFalse(self.out.exists())

    def test_api_spec_na_separate_not_pass(self):
        self.args.api_only_spec = 'stage specification: API only, no UI'
        self.args.flow = ''
        self.assertEqual(self.run_fake(), 0)
        self.assertEqual(self.report()['checks'][0]['status'], 'N/A')
        self.assertFalse(self.accepted())

    def test_empty_and_all_na_criteria_rejected(self):
        self.run_fake()
        for checks in ([], [{'id': 'na', 'status': 'N/A', 'spec_reference': 'API-only fixture spec'}]):
            report = self.report()
            report.update(checks=checks, summary=common.summarize(checks))
            (self.out / 'uiqa.json').write_text(json.dumps(report))
            self.assertFalse(self.accepted())

    def test_mixed_na_and_applicable_pass_accepted(self):
        self.run_fake()
        report = self.report()
        report['checks'].append({'id': 'na', 'status': 'N/A', 'spec_reference': 'non-applicable fixture spec'})
        report['summary'] = common.summarize(report['checks'])
        (self.out / 'uiqa.json').write_text(json.dumps(report))
        self.assertTrue(self.accepted())

    def test_schema_invalid_status_and_duplicate_id(self):
        for checks in ([{'id': 'x', 'status': 'UNKNOWN'}], [{'id': 'x', 'status': 'PASS'}, {'id': 'x', 'status': 'PASS'}]):
            with self.assertRaises(ValueError): common.summarize(checks)

    def test_declared_runtime_versions_and_api_network_effect(self):
        self.args.api_only_spec = 'API-only fixture'
        self.args.flow = ''
        self.run_fake()
        self.assertEqual(self.report()['effects'], {'docker': True, 'network': False})
        self.assertIsNone(self.report()['runner_image_observed'])
        self.assertEqual(self.report()['tools']['playwright'], 'NOT_EXECUTED')

    def test_fixture_spec_provenance_and_default_port(self):
        flow = json.loads((UIQA / 'fixtures/toy-stage-2.flow.json').read_text())
        validate_flow(flow)
        self.assertIn('DEVELOPER FIXTURE', flow['author'])
        self.assertIn('8080', flow['spec_reference'])
        self.assertEqual({s['selector'] for s in flow['scenarios'][0]['steps']}, {'[data-testid=counter-value]', '[data-testid=increment-button]'})

    def test_symlink_scratch_no_report(self):
        probe = self.root / 'probe'
        try: probe.symlink_to(self.axe)
        except OSError: self.skipTest('OS denies test symlink creation')
        probe.unlink()
        self.assertEqual(self.run_fake('symlink'), 1)
        self.assertFalse(self.out.exists())

    def test_revision_mismatch_rejected(self):
        self.run_fake()
        report = self.report()
        report['revision'] = 'b' * 40
        (self.out / 'uiqa.json').write_text(json.dumps(report))
        self.assertFalse(self.accepted())

    def test_fixed_hash_mismatch_error(self):
        self.args.axe_sha256 = '0' * 64
        self.assertEqual(self.run_fake(), 1)
        self.assertEqual(self.report()['execution'], 'ERROR')

    def test_report_not_visible_before_cleanup(self):
        original = checker.cleanup
        def observe(docker, effect):
            self.assertFalse(self.out.exists())
            self.assertTrue((docker.scratch / 'error-placeholder.json').exists())
            return original(docker, effect)
        with patch.object(checker, 'cleanup', observe):
            self.run_fake()

    def test_flow_data_only_and_local_routes(self):
        for key, value in (('path', 'https://other.invalid/'), ('path', '//other.invalid/')):
            bad = json.loads(json.dumps(FLOW))
            bad['scenarios'][0][key] = value
            with self.assertRaises(ValueError): validate_flow(bad)
        bad = json.loads(json.dumps(FLOW))
        bad['scenarios'][0]['steps'][0]['action'] = 'shell'
        with self.assertRaises(ValueError): validate_flow(bad)
        self.assertEqual(validate_flow(FLOW), FLOW)

    def test_path_escape_rejected(self):
        self.args.flow = '../outside.json'
        self.assertEqual(self.run_fake(), 1)
        self.assertEqual(self.report()['execution'], 'ERROR')

    def test_hardlink_denied(self):
        import os
        path = self.root / 'hardlink'
        os.link(self.axe, path)
        with self.assertRaises(ValueError): common.regular_tree(path)

    def test_symlink_denied(self):
        path = self.root / 'link'
        try:
            path.symlink_to(self.axe)
        except OSError:
            self.skipTest('OS denies test symlink creation')
        with self.assertRaises(ValueError): common.regular_tree(path)

    def test_browser_worker_fake_scan_and_increment_flow(self):
        from types import SimpleNamespace
        from browser_worker import run
        fixture = json.loads((UIQA / 'fixtures/toy-stage-2.flow.json').read_text())
        flow_path = self.root / 'browser-flow.json'
        flow_path.write_text(json.dumps(fixture))
        for mode in ('pass', 'overflow', 'violations', 'incomplete'):
            scratch = self.root / ('browser-' + mode)
            scratch.mkdir()
            class Locator:
                def __init__(self, page, selector): self.page, self.selector = page, selector
                def click(self): self.page.value += 1
                def inner_text(self): return str(self.page.value)
            class Page:
                def __init__(self): self.value = 0
                def on(self, *args): pass
                def goto(self, *args, **kw): pass
                def reload(self, **kw): pass
                def locator(self, selector): return Locator(self, selector)
                def add_script_tag(self, **kw): pass
                def screenshot(self, path, **kw): Path(path).write_bytes(b'fake screenshot')
                def evaluate(self, script):
                    if script == 'axe.version': return '4.10.3'
                    if script.startswith('document.documentElement'): return mode == 'overflow'
                    item = {'id': 'button-name', 'impact': 'critical', 'tags': ['wcag412'], 'nodes': [{}]}
                    return {'violations': [item] if mode == 'violations' else [], 'incomplete': [item] if mode == 'incomplete' else []}
            class Assertions:
                def __init__(self, loc): self.loc = loc
                def to_be_visible(self): pass
                def to_be_focused(self): pass
                def to_have_text(self, expected):
                    actual = self.loc.inner_text()
                    if hasattr(expected, 'fullmatch'): assert expected.fullmatch(actual)
                    else: assert actual == expected
            class Trace:
                def start(self, **kw): pass
                def stop(self, path): Path(path).write_bytes(b'fake trace')
            class Context:
                def __init__(self): self.tracing = Trace()
                def new_page(self): return Page()
                def route(self, *args): pass
                def close(self): pass
            class Browser:
                version = 'fake-chromium'
                def new_context(self, **kw): return Context()
                def close(self): pass
            class Playwright:
                def __enter__(self): return SimpleNamespace(chromium=SimpleNamespace(launch=lambda **kw: Browser()))
                def __exit__(self, *args): pass
            module = SimpleNamespace(sync_playwright=Playwright, expect=Assertions)
            with patch.dict(sys.modules, {'playwright': SimpleNamespace(), 'playwright.sync_api': module}), patch('importlib.metadata.version', return_value=common.PLAYWRIGHT_VERSION):
                self.assertEqual(run(flow_path, self.axe, scratch, 'http://app:8080'), 0)
            report = json.loads((scratch / 'candidate.json').read_text())
            self.assertEqual(report['execution'], 'COMPLETED')
            self.assertEqual(report['tools']['axe_runtime'], '4.10.3')
            self.assertTrue(all(c['status'] == 'PASS' for c in report['checks'] if c['id'].endswith('-flow')))
            if mode != 'pass': self.assertTrue(report['findings'])
            for artifact in report['artifacts']:
                self.assertEqual(artifact['sha256'], common.sha256(scratch / artifact['file']))
            if mode == 'incomplete': self.assertGreater(report['summary']['partial'], 0)

    def test_stop_cleanup_component(self):
        original = FakeDocker.__call__
        def interrupted(docker, *args):
            if args[0] == 'run' and '-d' not in args: raise KeyboardInterrupt
            return original(docker, *args)
        with patch.object(FakeDocker, '__call__', interrupted):
            self.assertEqual(self.run_fake(), 1)
        self.assertTrue(self.report()['cleanup']['verified'])


if __name__ == '__main__':
    unittest.main()
