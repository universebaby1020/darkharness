"""Public index/tree guard. Never prints matched values. No credential reads.

Official credential shapes: dark-factory harness/check.py revision 803560d2a678.
This is an independent guard, not the contest layout checker.
"""
import argparse
from pathlib import Path, PurePosixPath
import json
import os
import re
import subprocess
import sys

SHAPES = {
    "bearer-token": r"(?i)\bbearer\s+(?=[A-Za-z0-9._\-]*\d)[A-Za-z0-9._\-]{20,}",
    "api-key": r"\bsk-[A-Za-z0-9._\-]{16,}",
    "aws-access-key": r"\bAKIA[0-9A-Z]{16}\b",
    "github-token": r"\bgh[pousr]_[A-Za-z0-9]{20,}\b",
    "env-assignment": r"(?i)\b[A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD)\s*=\s*\S+",
    "url-credentials": r'''(?<=://)[^/\s:@"'\\]+:[^/\s@"'\\]+(?=@)''',
    "band-key": r'''(?i)\b(?:band[_-]?(?:agent[_-]?)?key|agent[_-]key)\s*[:=]\s*["']?[A-Za-z0-9._-]{16,}''',
    "private-windows-path": r"(?i)[a-z]:[\\/]+Users[\\/]+[A-Za-z0-9_.-]+[\\/]",
    "private-linux-path": r"/home/[A-Za-z0-9_.-]+/",
}
EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
CONFIG_NAMES = {"Dockerfile", "Makefile", ".envrc"}
CONFIG_SUFFIXES = {".env", ".json", ".toml", ".ini", ".cfg", ".yml", ".yaml"}


def findings(path, data):
    p = PurePosixPath(path)
    reasons = []
    if any(part.lower() in {".env", ".envrc", "auth.json", "credentials", "runtime-profiles", "state", ".venv", "__pycache__"} or part.lower().startswith(".env.") for part in p.parts) or p.suffix.lower() in {".db", ".sqlite", ".sqlite3", ".pem", ".key", ".pfx", ".pyc"}:
        reasons.append("private-file")
        return reasons  # No credential body is needed to reject a credential path.
    text = data.decode("utf-8", errors="replace")
    for name, expression in SHAPES.items():
        if name == "env-assignment" and (path == "room.json" or (p.suffix.lower() not in CONFIG_SUFFIXES and p.name not in CONFIG_NAMES)):
            continue
        if re.search(expression, text):
            reasons.append(name)
    for match in EMAIL.findall(text):
        domain = match.rsplit("@", 1)[1].lower()
        if not domain.endswith(".invalid") and domain != "users.noreply.github.com":
            reasons.append("personal-email")
            break
    return reasons


def git(*argv):
    # Capture errors; never echo Git stderr (it can contain private identity/paths).
    return subprocess.check_output(['git', *argv], stderr=subprocess.PIPE)


def load_private(path, repo):
    if not path or not Path(path).is_absolute():
        raise ValueError("PRIVATE_CONFIG_OUTSIDE_REPO_REQUIRED")
    path, repo = Path(path).resolve(), Path(repo).resolve()
    if path.is_relative_to(repo):
        raise ValueError('PRIVATE_CONFIG_OUTSIDE_REPO_REQUIRED')
    config = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(config, dict) or set(config) != {'schema_version', 'account_handles', 'band_uuids'} or config['schema_version'] != 'darkharness-publication-private-1':
        raise ValueError('PRIVATE_CONFIG_SCHEMA_INVALID')
    for key in ('account_handles', 'band_uuids'):
        if not isinstance(config[key], list) or not config[key] or not all(isinstance(v, str) and v.strip() == v and v and '\n' not in v and '\r' not in v for v in config[key]):
            raise ValueError('PRIVATE_CONFIG_VALUES_REQUIRED')
    if not all(re.fullmatch(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', v) for v in config['band_uuids']):
        raise ValueError('PRIVATE_CONFIG_UUID_INVALID')
    return config


def safe_path(path, private):
    text = path
    for values in (private.get('account_handles', []), private.get('band_uuids', [])):
        for value in values:
            text = text.replace(value, '[REDACTED]')
    for expression in SHAPES.values():
        text = re.sub(expression, '[REDACTED]', text)
    return EMAIL.sub('[REDACTED]', text)


def records(path, data, private, commit='INDEX'):
    rows = []
    text = data.decode('utf-8', errors='replace')
    for number, line in enumerate(text.splitlines(), 1):
        reasons = findings(path, line.encode('utf-8'))
        for key, reason in (('account_handles', 'account-handle'), ('band_uuids', 'real-band-uuid')):
            if any(value in line for value in private.get(key, [])):
                reasons.append(reason)
        for reason in dict.fromkeys(reasons):
            rows.append({'type': reason, 'commit': commit, 'file': safe_path(path, private), 'line': number})
    return rows


def scan_tree(revision, private):
    result = []
    entries = git('ls-tree', '-r', '-z', revision) if revision else git('ls-files', '--stage', '-z')
    for entry in entries.split(b'\0'):
        if not entry:
            continue
        metadata, name = entry.split(b'\t', 1)
        fields = metadata.split()
        mode, oid = fields[0], fields[2] if revision else fields[1]
        path = name.decode('utf-8', 'replace')
        commit = revision or 'INDEX'
        result.extend(records(path, name, private, commit))
        reasons = findings(path, b'')
        if mode not in {b'100644', b'100755'}:
            reasons.append('nonregular-index-entry')
        if not revision and fields[2] != b'0':
            reasons.append('unmerged-index-entry')
        if reasons:
            result.extend({'type': reason, 'commit': commit, 'file': safe_path(path, private), 'line': 0} for reason in reasons)
            continue  # Never open credential paths or symlink bodies.
        result.extend(records(path, git('cat-file', 'blob', oid.decode()), private, commit))
    return result


def scan_commit(commit, private):
    rows = scan_tree(commit, private)
    raw = git('cat-file', 'commit', commit)
    headers, _, message = raw.partition(b'\n\n')
    for line in headers.splitlines():
        for field in (b'author ', b'committer '):
            if line.startswith(field):
                rows.extend(records('<' + field.decode().strip() + '>', line[len(field):], private, commit))
    rows.extend(records('<commit-message>', message, private, commit))
    return rows


def resolve(ref):
    oid = git('rev-parse', '--verify', ref + '^{commit}').decode().strip()
    if not re.fullmatch(r'[0-9a-f]{40,64}', oid):
        raise ValueError('COMMIT_REQUIRED')
    return oid


def scan_ref(base, target, private):
    base, target = resolve(base), resolve(target)
    if git('merge-base', base, target).decode().strip() != base:
        raise ValueError('BASE_NOT_ANCESTOR')
    rows = []
    # No --first-parent: inspect all commits, including merged branches.
    for commit in git('rev-list', '--reverse', base + '..' + target).decode().splitlines():
        rows.extend(scan_commit(commit, private))
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--private-config', type=Path, default=os.environ.get("DH_PUBLIC_PRIVATE_CONFIG"))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--ref', help='deny if any commit in base..ref fails')
    mode.add_argument('--revision', help='scan one committed tree and all metadata')
    mode.add_argument('--published-history', help='record-only entire reachable ancestry')
    parser.add_argument('--base', default='d965945c50ab370cd979eefa8be1536f657007d1')
    parser.add_argument('--message-file', type=Path, default=os.environ.get("DH_PUBLIC_MESSAGE_FILE"), help='required precommit message; index mode only')
    args = parser.parse_args()
    try:
        repo = Path(git('rev-parse', '--show-toplevel').decode().strip())
        private = load_private(args.private_config, repo)
        if args.ref:
            rows = scan_ref(args.base, args.ref, private)
        elif args.revision:
            rows = scan_commit(resolve(args.revision), private)
        elif args.published_history:
            rows = []
            for commit in git('rev-list', '--reverse', resolve(args.published_history)).decode().splitlines():
                rows.extend(scan_commit(commit, private))
        else:
            if not args.message_file:
                raise ValueError('PRECOMMIT_MESSAGE_REQUIRED')
            rows = scan_tree(None, private)
            for field in ('AUTHOR', 'COMMITTER'):
                rows.extend(records('<' + field.lower() + '>', git('var', 'GIT_' + field + '_IDENT'), private))
            rows.extend(records('<commit-message>', args.message_file.read_bytes(), private))
        print(json.dumps({'status': 'EXPOSURE_RECORD_ONLY' if args.published_history else ('PUBLIC_GUARD_DENY' if rows else 'PUBLIC_GUARD_PASS'), 'findings': rows}, indent=2))
        return 0 if args.published_history else int(bool(rows))
    except (OSError, ValueError, subprocess.SubprocessError):
        print('PUBLIC_GUARD_ERROR: configuration or Git input invalid; values suppressed')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
