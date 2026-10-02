"""Controller-only offline submission hygiene. No native API/auth/runtime access.

Uses trusted organizer SECRETS and config logic, scans every readable repo text
file (not only tracked/changed files), and checks full native-export boundaries.
Never prints matched values; room env-assignment is the sole room exception.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

try:
    from tools.public_guard import findings as private_findings, safe_path
except ModuleNotFoundError:
    from public_guard import findings as private_findings, safe_path


def official_policy(root, python=sys.executable):
    source = Path(root) / 'harness/check.py'
    code = ('import json; from harness.check import SECRETS,CONFIG_ONLY,CONFIG_SUFFIXES,TEXT_NAMES; '
            'print(json.dumps([SECRETS,sorted(CONFIG_ONLY),sorted(CONFIG_SUFFIXES),sorted(TEXT_NAMES)]))')
    p = subprocess.run([python, '-X', 'utf8', '-B', '-c', code], cwd=root, capture_output=True, check=False)
    if p.returncode:
        raise ValueError('OFFICIAL_POLICY_UNAVAILABLE')
    patterns, only, suffixes, names = json.loads(p.stdout)
    return {'patterns': patterns, 'config_only': only, 'config_suffixes': suffixes,
            'config_names': names, 'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest()}


def scan_text(path, data, policy, room=False):
    text = data.decode('utf-8', 'replace')
    p = Path(path)
    config = not room and (p.suffix.lower() in policy['config_suffixes'] or p.name in policy['config_names'])
    rows = []
    for number, line in enumerate(text.splitlines(), 1):
        for name, pattern in policy['patterns']:
            if name in policy['config_only'] and not config:
                continue
            if re.search(pattern, line):
                rows.append({'type': name, 'file': safe_path(path, {}), 'line': number})
    return rows


def scan_repo(root, policy):
    root = Path(root)
    rows, counts = [], {'text_files': 0, 'binary_files': 0, 'unreadable_files': 0, 'private_paths_not_read': 0}
    def visit(folder):
        for p in sorted(folder.iterdir()):
            rel = p.relative_to(root).as_posix()
            if p.name == '.git':
                continue  # Git object store is not repository text; public_guard handles history.
            if p.is_symlink() or (hasattr(p, 'is_junction') and p.is_junction()) or not p.resolve().is_relative_to(root.resolve()):
                rows.append({'type': 'symlink-not-read', 'file': safe_path(rel, {}), 'line': 0})
                continue
            private = private_findings(rel, b'')
            if p.name.lower() in {'agents.json', 'credentials.json', 'secrets.json'}:
                private.append('private-file')
            if private:
                counts['private_paths_not_read'] += 1
                rows.extend({'type': r, 'file': safe_path(rel, {}), 'line': 0} for r in private)
                continue  # No auth/credential/state bodies are read.
            if p.is_dir():
                visit(p)
            elif p.is_file():
                try:
                    data = p.read_bytes()
                except OSError:
                    counts['unreadable_files'] += 1
                    rows.append({'type': 'unreadable-file', 'file': safe_path(rel, {}), 'line': 0})
                    continue
                if b'\0' in data:
                    counts['binary_files'] += 1
                    continue
                counts['text_files'] += 1
                rows.extend(scan_text(rel, data, policy, room=(rel == 'room.json')))
    visit(root)
    return rows, counts


def message_id(message):
    return message.get('id') or message.get('messageId')


def boundary(room, initial_id, final_id, api=None):
    result = {'full_scope': False, 'initial_present': False, 'final_present': False,
              'ordered': False, 'unique_ids': False, 'api_compare': 'NOT_RUN'}
    if not isinstance(room, dict) or not isinstance(room.get('messages'), list) or not all(isinstance(m, dict) for m in room['messages']):
        return result
    result['full_scope'] = room.get('scope', 'full') == 'full'  # Same native default as official check.
    ids = [message_id(m) for m in room['messages']]
    result['unique_ids'] = all(isinstance(i, str) and i for i in ids) and len(ids) == len(set(ids))
    result['initial_present'], result['final_present'] = initial_id in ids, final_id in ids
    if result['initial_present'] and result['final_present']:
        result['ordered'] = ids.index(initial_id) < ids.index(final_id)
    if api is not None:
        # Caller supplies retained full API messages, not a network endpoint.
        messages = api.get('messages') if isinstance(api, dict) else api
        result['api_compare'] = 'MATCH' if isinstance(messages, list) and messages == room['messages'] else 'MISMATCH'
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--official-root', type=Path, required=True)
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--room', type=Path, required=True)
    parser.add_argument('--initial-id', required=True)
    parser.add_argument('--final-id', required=True)
    parser.add_argument('--api-messages', type=Path)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    try:
        policy = official_policy(args.official_root)
        raw = args.room.read_bytes()
        native = json.loads(raw)
        api = json.loads(args.api_messages.read_bytes()) if args.api_messages else None
        rows, counts = scan_repo(args.repo, policy)
        rows += scan_text('<native-room>', raw, policy, room=True)
        bounds = boundary(native, args.initial_id, args.final_id, api)
        passed = not rows and all(bounds[k] for k in ('full_scope', 'initial_present', 'final_present', 'ordered', 'unique_ids')) and bounds['api_compare'] != 'MISMATCH'
        report = {'status': 'PASS' if passed else 'FAILED', 'scope': 'offline hygiene only, not official acceptance',
                  'room_sha256': hashlib.sha256(raw).hexdigest(), 'official_source_sha256': policy['source_sha256'],
                  'boundary': bounds, 'counts': counts, 'findings': rows}
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8', newline='\n')
        print(json.dumps(report, indent=2))
        return int(not passed)
    except (OSError, ValueError, subprocess.SubprocessError, TypeError):
        print('HYGIENE_ERROR: input unavailable or invalid; values suppressed')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
