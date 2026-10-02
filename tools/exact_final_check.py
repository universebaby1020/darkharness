"""Controller helper: plan or explicitly execute exact-final no-local clone checks.

No provider calls. --execute is for Main only after source is frozen. This tool
never pushes, commits, fetches from a network, or changes the product worktree.
"""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys


def plan(source, commit, destination, official, track, output, python=sys.executable):
    source, destination, official, output = map(lambda p: Path(p).resolve(), (source, destination, official, output))
    if not re.fullmatch(r'[0-9a-f]{40}', commit):
        raise ValueError('EXACT_FULL_SHA_REQUIRED')
    if track not in ('toy', 'tablekeeper', 'pocketful'):
        raise ValueError('OFFICIAL_TRACK_REQUIRED')
    if destination == source or destination.is_relative_to(source) or output.is_relative_to(source) or output.is_relative_to(destination):
        raise ValueError('EXTERNAL_NEW_DESTINATION_AND_OUTPUT_REQUIRED')
    if destination.exists() or output.exists():
        raise ValueError('DESTINATION_AND_OUTPUT_MUST_NOT_EXIST')
    return [
        {'argv': ['git', 'clone', '--no-local', '--no-checkout', str(source), str(destination)], 'cwd': str(source)},
        {'argv': ['git', '-C', str(destination), 'checkout', '--detach', commit], 'cwd': str(source)},
        {'argv': ['git', '-C', str(destination), 'rev-parse', 'HEAD'], 'cwd': str(source)},
        {'argv': [python, '-X', 'utf8', '-B', '-m', 'harness', 'check', str(destination), '--track', track], 'cwd': str(official)},
        {'argv': [python, '-X', 'utf8', '-B', '-m', 'harness', 'run', '--track', track, '--repo', str(destination), '--all', '--mode', 'isolated', '--out', str(output / 'isolated')], 'cwd': str(official)},
    ]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source', required=True, type=Path)
    p.add_argument('--commit', required=True)
    p.add_argument('--destination', required=True, type=Path)
    p.add_argument('--official-root', required=True, type=Path)
    p.add_argument('--track', required=True)
    p.add_argument('--output', required=True, type=Path)
    p.add_argument('--execute', action='store_true')
    args = p.parse_args()
    try:
        steps = plan(args.source, args.commit, args.destination, args.official_root, args.track, args.output)
        if not args.execute:
            # No local paths in console; inspect exact argv through function when needed.
            print(json.dumps({'status': 'NOT_RUN', 'commit': args.commit, 'steps': ['clone --no-local --no-checkout', 'checkout --detach exact SHA', 'verify HEAD', 'official check', 'official run --all --mode isolated']}))
            return 0
        result = []
        for number, step in enumerate(steps, 1):
            run = subprocess.run(step['argv'], cwd=step['cwd'], capture_output=True, check=False)
            # Never echo arbitrary stdout/stderr from source-under-test.
            ok = run.returncode == 0
            if number == 3:
                ok = ok and run.stdout.decode().strip() == args.commit
            result.append({'step': number, 'exit_code': run.returncode, 'passed': ok})
            if not ok:
                break
        args.output.mkdir(parents=True, exist_ok=True)
        report = {'status': 'CHECK_COMMANDS_COMPLETED' if len(result) == 5 and all(x['passed'] for x in result) else 'FAILED', 'commit': args.commit, 'checks': result,
                  'limit': 'Exit codes alone are not acceptance. Inspect official report counts, skips and stage coverage in private output; do not publish unsanitized source-under-test logs.'}
        (args.output / 'exact-final-summary.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8', newline='\n')
        print(json.dumps(report))
        return int(report['status'] != 'CHECK_COMMANDS_COMPLETED')
    except (ValueError, OSError):
        print('EXACT_FINAL_ERROR: invalid inputs or command unavailable; values suppressed')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
