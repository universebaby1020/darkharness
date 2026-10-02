"""Controller-code configuration helper. Never expose as model tool arguments."""
from pathlib import Path
import subprocess
from common import REPORT_FILE, criteria


def git_read(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL).decode().strip()


def configuration(source, executable, output_root, checkout_root, *, stage, port,
                  flow, axe, axe_version, axe_sha256, api_only_spec=''):
    source = Path(source).resolve()
    argv = ['-B', str(source / 'tools/uiqa/checker.py'), '{checkout}', '{output}',
            '--stage', str(stage), '--port', str(port), '--flow', flow,
            '--axe', str(Path(axe).absolute()), '--axe-version', axe_version,
            '--axe-sha256', axe_sha256]
    if api_only_spec:
        argv += ['--api-only-spec', api_only_spec]
    return {'source_root': str(source), 'source_head': git_read(source, 'rev-parse', 'HEAD'),
            'source_tree': git_read(source, 'rev-parse', 'HEAD^{tree}'), 'cwd': str(source),
            'executable': str(Path(executable).absolute()), 'argv': argv,
            'output_root': str(Path(output_root).absolute()), 'checkout_root': str(Path(checkout_root).absolute()),
            'output_layout': 'checker-owned-v1', 'effects': {'docker': True, 'network': not bool(api_only_spec)},
            'report_contract': 'json-criteria-v1', 'report_files': [REPORT_FILE],
            'report_criteria': criteria()}
