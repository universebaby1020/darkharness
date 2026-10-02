"""Trusted host executable, invoked only by controller-pinned VerificationBroker."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import uuid

from common import (LABEL, PLAYWRIGHT_VERSION, REPORT_FILE, RUNNER_IMAGE,
                    atomic_json, regular_tree, relative_file, sha256, summarize)


class Docker:
    def __init__(self, private):
        self.private = Path(private)
        self.sequence = 0

    def __call__(self, *argv):
        self.sequence += 1
        # Never forward app/build logs to room; broker captures host safe output only.
        result = subprocess.run(['docker', *argv], stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for name, data in (('stdout', result.stdout), ('stderr', result.stderr)):
            p = self.private / f'docker-{self.sequence}-{name}'
            with p.open('xb') as f:
                os.chmod(p, 0o600)
                f.write(data)
        return result


def checked(docker, *args):
    result = docker(*args)
    if result.returncode:
        raise RuntimeError('DOCKER_COMMAND_FAILED')
    return result.stdout.decode().strip()


def cleanup(docker, effect):
    """No stale PID handling. Remove only this unique effect's labelled objects."""
    selector = f'label={LABEL}={effect}'
    try:
        for query, remove in ((('ps', '-aq'), ('rm', '-f')),
                              (('network', 'ls', '-q'), ('network', 'rm')),
                              (('image', 'ls', '-q'), ('image', 'rm', '-f'))):
            ids = checked(docker, *query, '--filter', selector).split()
            if ids:
                checked(docker, *remove, *sorted(set(ids)))
        # Successful empty daemon queries, not rm exit status alone, prove absence.
        remaining = [checked(docker, *query, '--filter', selector)
                     for query in (('ps', '-aq'), ('network', 'ls', '-q'), ('image', 'ls', '-q'))]
        return not any(remaining)
    except (Exception, KeyboardInterrupt):
        return False


def candidate_error(scratch, reason):
    value = {'execution': 'ERROR', 'checks': [{'id': 'execution', 'status': 'ERROR'}],
             'findings': [], 'artifacts': [], 'error': reason, 'visual_review': 'NOT_PERFORMED'}
    # Placeholder is private; not a declared broker report.
    path = scratch / 'error-placeholder.json'
    if not path.exists():
        atomic_json(path, value)
    return value


def publish(output, scratch, report, *, revision, effect, axe, flow, app_image, runner_observed=None, network_effect=True):
    regular_tree(scratch)
    checks = report['checks']
    report['summary'] = summarize(checks)
    if report.get('execution') not in {'COMPLETED', 'ERROR'}:
        raise ValueError('EXECUTION_INVALID')
    if not isinstance(report.get('findings'), list) or not isinstance(report.get('artifacts'), list):
        raise ValueError('REPORT_COLLECTIONS_INVALID')
    if report['execution'] == 'COMPLETED' and report['summary']['applicable']:
        if not isinstance(report.get('tools'), dict) or not isinstance(report.get('viewports'), list):
            raise ValueError('REPORT_METADATA_INVALID')
        if report['tools'].get('playwright') != PLAYWRIGHT_VERSION or report['tools'].get('axe_runtime') != axe['version']:
            raise ValueError('RUNTIME_TOOL_PIN_MISMATCH')
    report.setdefault('viewports', [])
    report.setdefault('tools', {'playwright': 'NOT_EXECUTED_OR_UNKNOWN'})
    report['tools']['playwright_pin'] = PLAYWRIGHT_VERSION
    report['tools']['timeouts'] = 'Unmodified Playwright API defaults; no checker or broker deadline'
    for artifact in report['artifacts']:
        p = relative_file(scratch, artifact['file'])
        if sha256(p) != artifact['sha256']:
            raise ValueError('ARTIFACT_HASH_MISMATCH')
        artifact['path'] = str(p)
    report.update(revision=revision, runner_image_pin=RUNNER_IMAGE, runner_image_observed=runner_observed, app_image=app_image,
                  checker_version='external-uiqa-1', checker_sha256=sha256(Path(__file__)),
                  axe=axe, flow=flow, cleanup={'verified': True, 'effect_label': f'{LABEL}={effect}',
                  'proof': 'Successful empty labelled container/network/image daemon queries'},
                  effects={'docker': True, 'network': network_effect}, visual_review='NOT_PERFORMED')
    regular_tree(output.parent)
    if output.exists() or output.is_symlink():
        raise ValueError('REPORT_OUTPUT_ALREADY_EXISTS')
    output.mkdir(mode=0o700)
    atomic_json(output / REPORT_FILE, report)


def execute(args, docker_factory=Docker):
    checkout, output = Path(args.checkout).absolute(), Path(args.output).absolute()
    regular_tree(checkout)
    if output.exists() or output.is_symlink():
        raise ValueError('REPORT_OUTPUT_ALREADY_EXISTS')
    regular_tree(output.parent)
    private = output.parent / 'private'
    private.mkdir(mode=0o700, exist_ok=True)
    effect = uuid.uuid4().hex
    scratch = private / ('uiqa-' + effect)
    scratch.mkdir(mode=0o700)
    (scratch / 'home').mkdir()
    (scratch / 'tmp').mkdir()
    atomic_json(scratch / 'effect.json', {'effect': effect, 'label': f'{LABEL}={effect}', 'state': 'INTENT', 'kill_without_cleanup': 'PARTIAL'})
    docker = docker_factory(scratch)
    revision = subprocess.check_output(['git', '-C', str(checkout), 'rev-parse', 'HEAD'], stdin=subprocess.DEVNULL).decode().strip()
    report = candidate_error(scratch, 'HOST_OR_CONTAINER_ERROR')
    axe_info = {'version': args.axe_version, 'sha256': args.axe_sha256,
                'source': f'https://registry.npmjs.org/axe-core/-/axe-core-{args.axe_version}.tgz', 'license': 'MPL-2.0'}
    flow_info = {'file': args.flow, 'sha256': None}
    app_image = None
    runner_observed = None
    code = 1
    stopped = False
    def stop(signum, frame):
        nonlocal stopped
        stopped = True
        raise KeyboardInterrupt
    previous = {}
    if args.handle_signals:
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous[sig] = signal.signal(sig, stop)
    try:
        if args.api_only_spec:
            if args.flow:
                raise ValueError('API_ONLY_WITH_FLOW')
            report = {'execution': 'COMPLETED', 'checks': [{'id': 'ui-applicability', 'status': 'N/A', 'spec_reference': args.api_only_spec}], 'findings': [], 'artifacts': [], 'viewports': [], 'tools': {'playwright': 'NOT_EXECUTED'}}
            code = 0  # Broker criteria deliberately reject all-N/A; not UI PASS.
        else:
            stage = checkout / f'stage-{args.stage}'
            regular_tree(stage)
            relative_file(stage, 'Dockerfile')
            flow = relative_file(checkout, args.flow)
            # Only generic committed flow data is consumed; no subprocess/eval from flow.
            from browser_worker import validate_flow
            validate_flow(json.loads(flow.read_text(encoding='utf-8')))
            flow_info['sha256'] = sha256(flow)
            axe = Path(args.axe).absolute()
            regular_tree(axe)
            if sha256(axe) != args.axe_sha256:
                raise ValueError('AXE_PIN_MISMATCH')
            runner_observed = checked(docker, 'image', 'inspect', '--format', '{{.Id}}', RUNNER_IMAGE)
            if runner_observed != RUNNER_IMAGE:
                raise ValueError('RUNNER_PIN_MISMATCH')
            network = 'uiqa-' + effect
            label = f'{LABEL}={effect}'
            iid = scratch / 'app-image-id'
            checked(docker, 'build', '--force-rm', '--label', label, '--iidfile', str(iid), str(stage))
            app_image = iid.read_text().strip()
            checked(docker, 'network', 'create', '--internal', '--label', label, network)
            checked(docker, 'run', '-d', '--name', network + '-app', '--label', label,
                    '--network', network, '--network-alias', 'app', app_image)
            # Output is NEVER mounted. Checker, individual flow and fixed axe read-only.
            mounts = []
            for src, dst, ro in ((Path(__file__).parent.absolute(), '/checker', True),
                                 (flow, '/flow.json', True), (axe, '/axe.min.js', True),
                                 (scratch, '/scratch', False)):
                if ',' in str(src):
                    raise ValueError('MOUNT_PATH_INVALID')
                mounts += ['--mount', f'type=bind,src={src},dst={dst}' + (',readonly' if ro else '')]
            result = docker('run', '--name', network + '-browser', '--label', label,
                            '--network', network, '--read-only', '--cap-drop', 'ALL',
                            '--security-opt', 'no-new-privileges', '--user', f'{os.getuid()}:{os.getgid()}',
                            '-e', 'HOME=/scratch/home', '-e', 'TMPDIR=/scratch/tmp',
                            '-e', 'PYTHONDONTWRITEBYTECODE=1', *mounts, RUNNER_IMAGE,
                            'python3', '-B', '/checker/browser_worker.py', '/flow.json', '/axe.min.js', '/scratch', f'http://app:{args.port}')
            code = result.returncode
            regular_tree(scratch)
            candidate = scratch / 'candidate.json'
            if candidate.exists():
                report = json.loads(candidate.read_text(encoding='utf-8'))
                if code != 0:
                    report['execution'] = 'ERROR'
            else:
                code = code or 1
        if stopped:
            code = 1
    except KeyboardInterrupt:
        code = 1
        report = candidate_error(scratch, 'STOP_OR_CANCEL')
    except Exception:
        code = 1
        # No exception text: app/flow data could contain secrets.
        report = candidate_error(scratch, 'HOST_OR_CONTAINER_ERROR')
    finally:
        verified = cleanup(docker, effect)
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    if not verified:
        atomic_json(scratch / 'cleanup-unknown.json', {'execution': 'PARTIAL', 'cleanup': 'UNKNOWN', 'effect': effect})
        return 1  # No declared report: existing broker UNKNOWN fence.
    try:
        publish(output, scratch, report, revision=revision, effect=effect, axe=axe_info, flow=flow_info, app_image=app_image, runner_observed=runner_observed, network_effect=not bool(args.api_only_spec))
    except Exception:
        return 1  # Untrusted scratch / publish failure: no usable report.
    return code


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('checkout')
    p.add_argument('output')
    p.add_argument('--stage', type=int, required=True)
    p.add_argument('--port', type=int, required=True)
    p.add_argument('--flow', default='')
    p.add_argument('--api-only-spec', default='')
    p.add_argument('--axe', required=True)
    p.add_argument('--axe-version', required=True)
    p.add_argument('--axe-sha256', required=True)
    p.set_defaults(handle_signals=True)
    return p


if __name__ == '__main__':
    try:
        args = parser().parse_args()
        if args.stage < 1 or not 1 <= args.port <= 65535:
            raise ValueError('STAGE_PORT_INVALID')
        sys.exit(execute(args))
    except Exception:
        sys.exit(1)
