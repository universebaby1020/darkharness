"""Main-only real Docker + real VerificationBridge test preparation.

No model/provider/Band calls; in-memory controller owner is a fixture. Existing
completed result repo is read-only. All commits/mutations are disposable copies.
Run after Main commits/freezes checker source, from WSL Linux filesystem.
"""
import argparse
import asyncio
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import threading

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from common import RUNNER_IMAGE, atomic_json, regular_tree, sha256
from controller import configuration, git_read
from darkharness.integration.artifacts import SecretGuard
from darkharness.integration.git_broker import LocalGitBroker
from darkharness.integration.mailbox import IntegrationError, Mailbox
from darkharness.integration.policy import ApprovalRouter
from darkharness.integration.verification import VerificationBroker
from darkharness.integration.verification_bridge import VerificationBridge


class FixtureOwner:
    """Serialized controller store seam; not a room/SDK or runtime simulation."""
    def __init__(self, root, check, check_id):
        self.epoch = 1
        self.mutex = threading.RLock()
        self.db = sqlite3.connect(':memory:', isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute('CREATE TABLE controls(kind TEXT,id TEXT,body TEXT,revision INTEGER,PRIMARY KEY(kind,id))')
        body = {'source': 'Main-authorized disposable controller integration', 'end_condition': 'tests end or STOP',
                'scope': {'workspace': str(root), 'run_id': 'run', 'seats': ['s', 'peer'], 'rooms': ['r'],
                          'local_git_write': {'paths': ['uiqa-flow.json'], 'directories': ['stage-2']},
                          'review_snapshot': {'scratch': str(root.parent / 'snapshots')},
                          'verification': {'checks': {check_id: check}}}}
        self.db.execute("INSERT INTO controls VALUES('grant','g',?,1)", (json.dumps(body),))

    @contextmanager
    def transaction(self, epoch=None):
        with self.mutex:
            if epoch is not None and epoch != self.epoch:
                raise IntegrationError('OWNER_ATTEMPT_FENCE')
            self.db.execute('BEGIN IMMEDIATE')
            try:
                yield self.db
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise


def command(*args, cwd=None):
    # Log content stays private; never echo application build or browser logs.
    return subprocess.run(list(args), cwd=cwd, check=True, stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def prepare_copy(args, root, mode):
    result = root / 'result'
    command('git', 'clone', '--no-local', '--no-hardlinks', '--no-checkout', args.result_repo, str(result))
    command('git', 'checkout', '-q', '-b', 'uiqa-test', args.revision, cwd=result)
    # Reviewer-supplied spec-only flow, not reconstructed from shipped tests.
    flow = json.loads(Path(args.flow).read_text(encoding='utf-8'))
    paths = ['uiqa-flow.json']
    if mode in {'overflow', 'axe-violation'}:
        # Controlled defect fixture in a SEPARATE result copy; does not diagnose
        # the original toy app. The fixture spec is exactly this injected HTML.
        stage = result / 'stage-2'
        stage.mkdir(exist_ok=True)
        html = '<!doctype html><html lang="en"><head><title>Injected control</title></head><body><main><h1>Control</h1>'
        html += '<div style="width:2000px;height:50px">Overflow control</div>' if mode == 'overflow' else '<button></button>'
        html += '</main></body></html>'
        (stage / 'uiqa-control.html').write_text(html)
        (stage / 'Dockerfile').write_text(f'FROM {RUNNER_IMAGE}\nCOPY uiqa-control.html /srv/index.html\nWORKDIR /srv\nCMD ["python3", "-m", "http.server", "8000"]\n')
        flow = {'spec_reference': 'Injected negative control spec: heading Control at /; fixture is not production evidence',
                'author': 'controller negative fixture', 'viewports': flow['viewports'],
                'scenarios': [{'id': 'control', 'path': '/', 'steps': [{'action': 'text', 'selector': 'h1', 'value': 'Control'}]}]}
        paths += ['stage-2/Dockerfile', 'stage-2/uiqa-control.html']
    elif mode == 'startup-fail':
        p = result / 'stage-2/Dockerfile'
        p.write_text(p.read_text() + '\nCMD ["/bin/false"]\n')
        paths += ['stage-2/Dockerfile']
    (result / 'uiqa-flow.json').write_text(json.dumps(flow))
    return result, paths


async def run_case(args, root, mode, manifest, guard):
    root.mkdir(mode=0o700)
    result_repo, changed = prepare_copy(args, root, mode)
    axe_path = Path(manifest['path']) if mode != 'missing-axe' else root / 'deliberately-missing-axe.min.js'
    cfg = configuration(args.checker_source, sys.executable, root / 'outputs', root / 'snapshots',
                        stage=1 if mode == 'api-only' else 2, port=8000 if mode in {'overflow', 'axe-violation'} else args.port,
                        flow='' if mode == 'api-only' else 'uiqa-flow.json',
                        axe=axe_path, axe_version=manifest['version'], axe_sha256=manifest['sha256'],
                        api_only_spec=args.api_only_spec if mode == 'api-only' else '')
    check_id = 'uiqa-stage-1' if mode == 'api-only' else 'uiqa-stage-2'
    owner = FixtureOwner(result_repo, cfg, check_id)
    box = Mailbox(owner)
    peer = ApprovalRouter(owner, 'g', 'peer', 'r', 'run', result_repo)
    peer_op = box.receive('peer', 'r', 'controller', 'builder-task', 'Commit disposable fixture/Reviewer-authored flow')['work']
    box.claim(peer_op, 'pa')
    git = LocalGitBroker(box, peer, 'UIQA fixture', 'uiqa@actors.invalid', root)
    parent = git_read(result_repo, 'rev-parse', 'HEAD')
    commit = git.commit(peer_op, 'pa', 'commit', cwd=str(result_repo), paths=changed, message='Disposable UIQA controller fixture', expected_head=parent)
    snap = git.snapshot(peer_op, 'pa', 'snapshot', cwd=str(result_repo), revision=commit['commit'], name='independent')
    VerificationBroker.record_git_origin(box, peer, commit)
    VerificationBroker.record_git_origin(box, peer, snap)
    router = ApprovalRouter(owner, 'g', 's', 'r', 'run', result_repo)
    op = box.receive('s', 'r', 'controller', 'review-task', 'Full fixture specification and independent revision check; no model call')['work']
    box.claim(op, 'a')
    bridge = VerificationBridge(box, router, guard)
    try:
        assert {s['name'] for s in bridge.schemas()} == {'dh_verify', 'dh_verification_read'}
        effect, public = await bridge.start(op, 'a', 'controller-check', {'check_id': check_id, 'checkout_receipt': snap, 'revision': commit['commit']})
        # Deliberately NO new broker/checker timeout or asyncio.wait_for deadline.
        result = await bridge.broker.wait(op, 'a', effect)
        report_path = Path(result['output']) / 'uiqa.json'
        report = json.loads(report_path.read_text()) if report_path.exists() else None
        page = None
        if report:
            page = bridge.read_page(op, 'a', effect_id=effect, artifact='uiqa.json', offset=0, limit=1024)
        child = bridge.complete(op, 'a', effect)
        assert bridge.complete(op, 'a', effect) == child
        with owner.transaction() as db:
            count = db.execute('SELECT COUNT(*) FROM c_verification_continuation').fetchone()[0]
        if mode == 'positive':
            # Integration/tool-path success is NOT SUT criteria success. An
            # untouched app may genuinely have findings; preserve broker FAILED.
            passed = result['state'] in {'SUCCEEDED', 'FAILED'} and result['exit_code'] == 0 and report is not None and report['execution'] == 'COMPLETED' and report['summary']['applicable'] > 0 and report['cleanup']['verified'] and report['revision'] == commit['commit']
        else:
            passed = result['state'] == 'FAILED'
        passed = passed and child is not None and count == 1
        if mode in {'missing-axe', 'startup-fail'}:
            passed = passed and report is not None and report['execution'] == 'ERROR' and report['cleanup']['verified']
        if mode == 'api-only':
            passed = passed and report is not None and report['checks'][0]['status'] == 'N/A' and report['summary']['applicable'] == 0
        if mode in {'overflow', 'axe-violation'}:
            expected_kind = 'document_horizontal_overflow' if mode == 'overflow' else 'violations'
            passed = passed and report is not None and any(f['kind'] == expected_kind for f in report['findings'])
        # A known ERROR must not fence the next broker start. Use new independent
        # work attempt after continuation creation; do not mutate old effect state.
        fence_rows = owner.db.execute("SELECT COUNT(*) FROM c_verification_effect WHERE state='UNKNOWN'").fetchone()[0]
        passed = passed and fence_rows == 0
        evidence = {'mode': mode, 'level': 'real_controller_broker_docker_with_fixture_owner', 'passed': bool(passed),
                    'original_revision': args.revision, 'tested_revision': commit['commit'], 'checker_head': cfg['source_head'],
                    'checker_tree': cfg['source_tree'], 'cfg': cfg, 'result': result, 'report_path': str(report_path),
                    'report_sha256': sha256(report_path) if report_path.exists() else None,
                    'continuation_count': count, 'guarded_read_sha256': page['sha256'] if page else None,
                    'unknown_fence_rows': fence_rows, 'integration_tool_path_accepted': bool(passed),
                    'sut_criteria_accepted': result['accepted'], 'genuine_findings_preserved': bool(report and report['findings']),
                    'visual_review': 'NOT_PERFORMED'}
        atomic_json(root / 'case.json', evidence)
        return {'mode': mode, 'passed': bool(passed), 'state': result['state'], 'raw': str(root / 'case.json')}
    finally:
        bridge.broker.close()
        owner.db.close()


async def main(args):
    if sys.platform != 'linux':
        raise ValueError('WSL_LINUX_REQUIRED')
    original_head = git_read(args.result_repo, 'rev-parse', 'HEAD')
    original_status = git_read(args.result_repo, 'status', '--porcelain')
    if not args.revision.startswith(('ba36a2e', '8d7405a')):
        raise ValueError('AUTHORIZED_TOY_REVISION_REQUIRED')
    if git_read(args.result_repo, 'rev-parse', args.revision) != args.revision:
        raise ValueError('FULL_REVISION_REQUIRED')
    manifest = json.loads(Path(args.axe_manifest).read_text())
    if manifest['version'] != '4.10.3' or sha256(manifest['path']) != manifest['sha256']:
        raise ValueError('AXE_MANIFEST_PIN_MISMATCH')
    regular_tree(Path(args.workspace).parent)
    root = Path(args.workspace).absolute()
    root.mkdir(mode=0o700, exist_ok=False)
    guard = SecretGuard.official(Path(args.official_root))
    results = []
    for mode in ('positive', 'missing-axe', 'startup-fail', 'api-only', 'overflow', 'axe-violation'):
        results.append(await run_case(args, root / mode, mode, manifest, guard))
        print(json.dumps(results[-1]))
    unchanged = original_head == git_read(args.result_repo, 'rev-parse', 'HEAD') and original_status == git_read(args.result_repo, 'status', '--porcelain')
    atomic_json(root / 'summary.json', {'results': results, 'original_result_unchanged': unchanged, 'visual_review': 'NOT_PERFORMED'})
    return 0 if unchanged and all(r['passed'] for r in results) else 1


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('result-repo', 'revision', 'flow', 'axe-manifest', 'checker-source', 'official-root', 'workspace', 'api-only-spec'):
        p.add_argument('--' + name, required=True)
    p.add_argument('--port', type=int, required=True)
    args = p.parse_args()
    raise SystemExit(asyncio.run(main(args)))
