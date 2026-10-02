"""External UIQA data contract; no Core imports or executable profile loading."""
import hashlib
import json
import os
from pathlib import Path
import stat

RUNNER_IMAGE = 'sha256:6c82d0825613ef894eb2680ee61ffa23c5d0de6a8857525590b98c9d9663cf76'
PLAYWRIGHT_VERSION = '1.63.0'
LABEL = 'org.darkharness.uiqa.effect'
REPORT_FILE = 'uiqa.json'


def safe_exception_class(exc):
    # Do not serialize exception messages, URLs, DOM or arbitrary class names.
    name = type(exc).__name__
    return name if name in {'AssertionError', 'ValueError', 'RuntimeError',
                            'FileNotFoundError', 'PermissionError', 'OSError',
                            'KeyError', 'TypeError', 'JSONDecodeError',
                            'Error', 'TimeoutError', 'KeyboardInterrupt'} else 'Exception'


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def regular_tree(root):
    """Reject links (including ancestors), hardlinks, devices, pipes and sockets."""
    root = Path(root).absolute()
    for p in (root, *root.parents):
        if p.is_symlink():
            raise ValueError('LINK_DENIED')
    for p in (root, *root.rglob('*')):
        st = p.lstat()
        if not (stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode)):
            raise ValueError('SPECIAL_PATH_DENIED')
        if stat.S_ISREG(st.st_mode) and st.st_nlink != 1:
            raise ValueError('HARDLINK_DENIED')
    return root


def relative_file(root, rel):
    p = Path(rel)
    if not rel or p.is_absolute() or any(x in {'.git', '..'} for x in p.parts):
        raise ValueError('RELATIVE_PATH_REQUIRED')
    candidate = Path(root) / p
    regular_tree(candidate)
    if not candidate.is_file():
        raise ValueError('FILE_REQUIRED')
    return candidate


def atomic_json(path, value):
    path = Path(path)
    tmp = path.with_name(path.name + '.tmp')
    with tmp.open('x', encoding='utf-8') as f:
        os.chmod(tmp, 0o600)
        json.dump(value, f, ensure_ascii=True, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def summarize(checks):
    allowed = {'PASS', 'FAIL', 'PARTIAL', 'N/A', 'ERROR'}
    if not isinstance(checks, list) or any(not isinstance(c, dict) or c.get('status') not in allowed or not isinstance(c.get('id'), str) or not c['id'] or (c['status'] == 'N/A' and not isinstance(c.get('spec_reference'), str)) for c in checks):
        raise ValueError('CHECKS_INVALID')
    if len({c['id'] for c in checks}) != len(checks):
        raise ValueError('DUPLICATE_CHECK_ID')
    return {'total': len(checks), 'applicable': sum(c['status'] != 'N/A' for c in checks),
            'passed': sum(c['status'] == 'PASS' for c in checks),
            'failed': sum(c['status'] == 'FAIL' for c in checks),
            'partial': sum(c['status'] == 'PARTIAL' for c in checks),
            'errors': sum(c['status'] == 'ERROR' for c in checks)}


def criteria():
    """Existing json-criteria-v1; a completed scan is not overall visual/WCAG QA."""
    return [{'file': REPORT_FILE, 'revision_path': ['revision'],
             'equals': [{'path': ['execution'], 'value': 'COMPLETED'},
                        {'path': ['cleanup', 'verified'], 'value': True}],
             'positive': [['summary', 'applicable'], ['summary', 'total']],
             'zero': [['summary', 'failed'], ['summary', 'partial'], ['summary', 'errors']],
             'equal_paths': [{'left': ['summary', 'passed'], 'right': ['summary', 'applicable']}]}]
