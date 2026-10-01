"""Controller-Grant-bound trusted verification, not a general command escape.

Controller configuration lives in ApprovalRouter's authenticated Grant, never in
seat tool arguments. Supply the shared SerializedOwner (thread-safe transactions)
and one broker per router. Linux process groups are owned only by this instance;
restart uncertainty is fenced, not retried or signalled by stale PID. Trusted
checker effects are NOT OS isolation against concurrent malicious same-UID writes
or checker daemonization. Docker/network effects must be declared by controller.
Raw logs and report artifacts are private: only public_result is an SDK projection.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import threading
import time
import uuid
from types import MappingProxyType


def _immutable(value):
    if isinstance(value, dict):
        return MappingProxyType({k: _immutable(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(_immutable(v) for v in value)
    return value

from .mailbox import IntegrationError, Mailbox, digest, encode

OID = re.compile(r'[0-9a-f]{40}')
TERMINAL = {'SUCCEEDED', 'FAILED', 'CANCELLED', 'TIMED_OUT', 'UNKNOWN'}


def _path(value, *, exists=True):
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        raise IntegrationError('CANONICAL_PATH_REQUIRED')
    p = Path(value)
    for q in [p, *p.parents]:
        if q.is_symlink():
            raise IntegrationError('PATH_LINK_DENIED')
    if p != p.resolve() or '..' in p.parts:
        raise IntegrationError('CANONICAL_PATH_REQUIRED')
    if exists:
        st = p.lstat()
        if stat.S_ISREG(st.st_mode) and st.st_nlink != 1:
            raise IntegrationError('PATH_LINK_DENIED')
        if not (stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode)):
            raise IntegrationError('REGULAR_PATH_REQUIRED')
    return p


def _env():
    # No inherited provider credentials, proxies, PYTHONPATH or Git configuration.
    return {'PATH': '/usr/bin:/bin', 'HOME': '/nonexistent', 'LC_ALL': 'C',
            'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null',
            'GIT_TERMINAL_PROMPT': '0', 'GIT_NO_REPLACE_OBJECTS': '1',
            'PYTHONNOUSERSITE': '1', 'PYTHONDONTWRITEBYTECODE': '1'}


def _git(root, *args):
    try:
        p = subprocess.run(['/usr/bin/git', '--no-optional-locks', '--git-dir=' + str(root / '.git'),
                            '--work-tree=' + str(root), '-c', 'core.hooksPath=/dev/null',
                            '-c', 'core.fsmonitor=false', '-c', 'gc.auto=0', *args],
                           cwd=root, env=_env(), stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        raise IntegrationError('GIT_READ_UNKNOWN') from None
    if p.returncode:
        raise IntegrationError('GIT_READ_UNKNOWN')
    return p.stdout


def _identity(root):
    _path(str(root))
    gd = _path(str(root / '.git'))
    if not gd.is_dir():
        raise IntegrationError('DIRECT_GIT_DIRECTORY_REQUIRED')
    for rel in ('commondir', 'objects/info/alternates', 'info/grafts', 'refs/replace'):
        p = gd / rel
        if p.exists() and (not p.is_dir() or any(p.iterdir())):
            raise IntegrationError('INDIRECT_GIT_STORE_DENIED')
    for p in gd.rglob('*'):
        _path(str(p))
    names = _git(root, 'config', '--local', '--no-includes', '--name-only', '--list').decode().splitlines()
    if any(n.startswith(('include.', 'includeif.', 'extensions.')) or n == 'core.worktree' for n in names):
        raise IntegrationError('INDIRECT_GIT_CONFIG_DENIED')
    if _git(root, 'rev-parse', '--show-toplevel').decode().strip() != str(root) or _git(root, 'rev-parse', '--is-bare-repository').strip() != b'false':
        raise IntegrationError('SOURCE_IDENTITY_MISMATCH')
    a, b = root.stat(), gd.stat()
    return [str(root), a.st_dev, a.st_ino, str(gd), b.st_dev, b.st_ino]


def _oid(value):
    if not isinstance(value, str) or not OID.fullmatch(value):
        raise IntegrationError('EXACT_REVISION_REQUIRED')
    return value


def _hash_file(p):
    _path(str(p))
    if not p.is_file():
        raise IntegrationError('REGULAR_FILE_REQUIRED')
    h, size = hashlib.sha256(), 0
    with p.open('rb') as f:
        for block in iter(lambda: f.read(65536), b''):
            size += len(block)
            h.update(block)
    return {'path': str(p), 'sha256': h.hexdigest(), 'bytes': size}


def _tree(root, revision, label, *, exact=False):
    """Compare actual bytes to Git blobs, without filters/index stat shortcuts."""
    _oid(revision)
    paths = set()
    for entry in _git(root, 'ls-tree', '-rz', '--full-tree', revision).split(b'\0'):
        if not entry:
            continue
        meta, raw = entry.split(b'\t', 1)
        mode, typ, oid = meta.decode().split()
        rel = raw.decode('utf-8')
        parts = Path(rel).parts
        if typ != 'blob' or mode not in {'100644', '100755'} or not parts or Path(rel).is_absolute() or any(x in {'.', '..', '.git'} for x in parts):
            raise IntegrationError('TREE_PATH_DENIED')
        p = _path(str(root / rel))
        if not p.is_file():
            raise IntegrationError(label + '_DIRTY')
        st = p.stat()
        h = hashlib.sha1(('blob ' + str(st.st_size) + '\0').encode())
        with p.open('rb') as f:
            for block in iter(lambda: f.read(65536), b''):
                h.update(block)
        if h.hexdigest() != oid or bool(st.st_mode & 0o111) != (mode == '100755'):
            raise IntegrationError(label + '_DIRTY')
        paths.add(rel)
    if _git(root, 'diff-index', '--cached', '--raw', '--no-ext-diff', '--no-textconv', revision, '--'):
        raise IntegrationError(label + '_DIRTY')
    if exact:
        for base, dirs, files in os.walk(root, followlinks=False):
            if Path(base) == root:
                dirs[:] = [d for d in dirs if d != '.git']
            for name in dirs + files:
                p = _path(str(Path(base) / name))
                if p.is_file() and str(p.relative_to(root)) not in paths:
                    raise IntegrationError(label + '_DIRTY')
    return _git(root, 'rev-parse', revision + '^{tree}').decode().strip()


@dataclass
class _Job:
    cancel: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    process: subprocess.Popen | None = None


class VerificationBroker:
    def __init__(self, mailbox, router, *, report_parsers, receipt_run_resolver=None):
        self.box, self.router, self.owner = mailbox, router, mailbox.owner
        self.parsers = dict(report_parsers)  # controller code, not model plugins
        self.receipt_run_resolver = receipt_run_resolver  # authenticated coordinator seam
        self.token = uuid.uuid4().hex
        self.lock = threading.RLock()
        self.jobs = {}
        self.closed = False
        with self.owner.transaction(self.owner.epoch) as db:
            db.execute('CREATE TABLE IF NOT EXISTS c_verification_effect(id TEXT PRIMARY KEY, operation TEXT, attempt TEXT, run_id TEXT, request TEXT, config TEXT, state TEXT, owner TEXT, output TEXT, process TEXT, result TEXT)')

    @staticmethod
    def _effect_selector(db, selector):
        if isinstance(selector, str) and selector:
            row = db.execute('SELECT * FROM c_git_effect WHERE id=?', (selector,)).fetchone()
            if row:
                return row
        elif isinstance(selector, dict):
            # Exact canonical returned receipt, not an arbitrary model path.
            rows = db.execute("SELECT * FROM c_git_effect WHERE state='ACKED' AND receipt=?", (encode(selector),)).fetchall()
            if len(rows) == 1:
                return rows[0]
        raise IntegrationError('CHECKOUT_RECEIPT_UNKNOWN_OR_AMBIGUOUS')

    @staticmethod
    def record_git_origin(mailbox, router, selector):
        """Controller adapter seam: mechanically stamp an authenticated Git call.

        Invoke with the actual creating peer's router after the Git ACK. Never
        expose this as a seat tool or infer a legacy run from current Grant alone.
        No new Grant entry or operator action is needed for each receipt.
        """
        with mailbox.owner.transaction(mailbox.owner.epoch) as db:
            row = VerificationBroker._effect_selector(db, selector)
            work = db.execute('SELECT * FROM c_work WHERE id=?', (row['operation'],)).fetchone()
            if not router._scope(db) or not work or work['seat'] != router.seat or work['room'] != router.room or work['attempt'] != row['attempt'] or row['state'] != 'ACKED':
                raise IntegrationError('AUTHENTICATED_GIT_ORIGIN_REQUIRED')
            body = {'id': row['id'], 'attempt': row['attempt'], 'run_id': router.run_id,
                    'workspace': router.workspace, 'seat': router.seat, 'room': router.room,
                    'request_sha256': digest(row['request'].encode()),
                    'receipt_sha256': digest(row['receipt'].encode())}
            old = db.execute("SELECT body FROM c_event WHERE operation=? AND kind='GIT_RUN_ORIGIN'", (row['operation'],)).fetchall()
            matching = [json.loads(r[0]) for r in old if json.loads(r[0]).get('id') == row['id']]
            if matching and any(v != body for v in matching):
                raise IntegrationError('GIT_RUN_ORIGIN_CONFLICT')
            if not matching:
                Mailbox.event(db, row['operation'], 'GIT_RUN_ORIGIN', body)
            return row['id']

    def _same_run(self, db, row, scope):
        work = db.execute('SELECT * FROM c_work WHERE id=?', (row['operation'],)).fetchone()
        if not work or work['attempt'] != row['attempt'] or work['seat'] not in scope.get('seats', []) or work['room'] != self.router.room:
            raise IntegrationError('AUTHORIZED_RECEIPT_SEAT_REQUIRED')
        rows = db.execute("SELECT body FROM c_event WHERE operation=? AND kind='GIT_RUN_ORIGIN'", (row['operation'],)).fetchall()
        origins = [json.loads(r[0]) for r in rows if json.loads(r[0]).get('id') == row['id']]
        if origins:
            expected = {'id': row['id'], 'attempt': row['attempt'], 'run_id': self.router.run_id,
                        'workspace': self.router.workspace, 'seat': work['seat'], 'room': work['room'],
                        'request_sha256': digest(row['request'].encode()),
                        'receipt_sha256': digest(row['receipt'].encode())}
            if any(origin != expected for origin in origins):
                raise IntegrationError('RECEIPT_RUN_MISMATCH')
        elif self.receipt_run_resolver is None or self.receipt_run_resolver(db, dict(row)) != self.router.run_id:
            raise IntegrationError('RECEIPT_RUN_PROVENANCE_REQUIRED')
        return work

    def _work(self, db, operation, attempt):
        work = db.execute('SELECT * FROM c_work WHERE id=?', (operation,)).fetchone()
        if not work or work['attempt'] != attempt or work['seat'] != self.router.seat or work['room'] != self.router.room:
            raise IntegrationError('OWNER_ATTEMPT_FENCE')
        return work

    def _authorize(self, db, operation, attempt, check_id, snapshot):
        work = self._work(db, operation, attempt)
        scope = self.router._scope(db)
        run = db.execute("SELECT body FROM controls WHERE kind='run' AND id=?", (self.router.run_id,)).fetchone()
        if run and json.loads(run[0]).get('state') in {'UNKNOWN', 'DELIVERY_UNKNOWN', 'EFFECT_UNKNOWN', 'CLOSED_UNRESOLVED'}:
            raise IntegrationError('EXTERNAL_UNKNOWN_FENCE')
        cap = scope.get('verification') if scope else None
        if not isinstance(cap, dict) or work['state'] != 'RUNNING' or work['delivery'] not in {'STARTED', 'DISPATCHING'}:
            raise IntegrationError('TYPED_VERIFICATION_GRANT_REQUIRED')
        cfg = cap.get('checks', {}).get(check_id)
        if not isinstance(cfg, dict):
            raise IntegrationError('CHECK_NOT_GRANTED')
        binding = None
        if 'receipts' in cap:  # optional narrowing restriction, never per-receipt approval
            restrictions = cap['receipts']
            if not isinstance(restrictions, dict) or not isinstance(restrictions.get(snapshot), dict):
                raise IntegrationError('CHECKOUT_RECEIPT_NOT_GRANTED')
            binding = restrictions[snapshot]
        return cfg, binding

    def _configuration(self, cfg):
        if os.name != 'posix' or not Path('/proc/self/stat').exists():
            raise IntegrationError('LINUX_PROCESS_OWNERSHIP_REQUIRED')
        source = _path(cfg.get('source_root'))
        _identity(source)
        head, tree = _oid(cfg.get('source_head')), _oid(cfg.get('source_tree'))
        if _git(source, 'rev-parse', 'HEAD').decode().strip() != head or _git(source, 'rev-parse', 'HEAD^{tree}').decode().strip() != tree:
            raise IntegrationError('CHECKER_PIN_MISMATCH')
        if _tree(source, head, 'SOURCE') != tree:
            raise IntegrationError('CHECKER_PIN_MISMATCH')
        cwd = _path(cfg.get('cwd'))
        if not cwd.is_dir() or not cwd.is_relative_to(source):
            raise IntegrationError('CHECKER_CWD_ESCAPE')
        exe = cfg.get('executable')
        if not isinstance(exe, str) or not Path(exe).is_absolute():
            raise IntegrationError('ABSOLUTE_EXECUTABLE_REQUIRED')
        # Preserve argv[0] semantics (notably venv pyvenv.cfg discovery). A
        # controller-configured final symlink is allowed, its target is pinned
        # separately; resolving it must NOT switch execution to system Python.
        exe = Path(exe)
        if '..' in exe.parts:
            raise IntegrationError('ABSOLUTE_EXECUTABLE_REQUIRED')
        _path(str(exe.parent))
        target = _path(str(exe.resolve()))
        forbidden = {'sh', 'bash', 'zsh', 'fish', 'sudo', 'su', 'doas', 'docker', 'env'}
        if not target.is_file() or not os.access(exe, os.X_OK) or exe.name in forbidden or target.name in forbidden:
            raise IntegrationError('TRUSTED_EXECUTABLE_REQUIRED')
        st = target.stat()
        executable_identity = {'configured': str(exe), 'target': str(target), 'device': st.st_dev,
                               'inode': st.st_ino, 'sha256': _hash_file(target)['sha256']}
        argv = cfg.get('argv')
        if not isinstance(argv, list) or not argv or not all(isinstance(x, str) and '\0' not in x for x in argv):
            raise IntegrationError('FIXED_ARGV_REQUIRED')
        if not {'{checkout}', '{output}'}.issubset(argv) or any(('{' in x or '}' in x) and x not in {'{checkout}', '{output}'} for x in argv):
            raise IntegrationError('EXACT_PLACEHOLDERS_REQUIRED')
        output = _path(cfg.get('output_root'), exists=False)
        checkout = _path(cfg.get('checkout_root'), exists=False)
        if output.is_relative_to(source) or output.is_relative_to(checkout) or source.is_relative_to(output) or checkout.is_relative_to(output):
            raise IntegrationError('OUTPUT_ROOT_OVERLAP')
        if cfg.get('report_contract') not in self.parsers:
            raise IntegrationError('DECLARED_REPORT_CONTRACT_REQUIRED')
        reports = cfg.get('report_files')
        if not isinstance(reports, list) or not reports or any(not isinstance(x, str) or not x or Path(x).is_absolute() or '..' in Path(x).parts or '.git' in Path(x).parts or x in {'stdout', 'stderr'} for x in reports):
            raise IntegrationError('BOUNDED_REPORT_PATH_REQUIRED')
        effects = cfg.get('effects')
        if not isinstance(effects, dict) or set(effects) != {'docker', 'network'} or any(type(v) is not bool for v in effects.values()):
            raise IntegrationError('DECLARED_CHECKER_EFFECTS_REQUIRED')
        timeout = cfg.get('timeout_seconds')
        maximum = cfg.get('max_log_bytes')
        if timeout is not None and (type(timeout) not in {int, float} or not math.isfinite(timeout) or timeout <= 0):
            raise IntegrationError('TIMEOUT_INVALID')
        if maximum is not None and (type(maximum) is not int or maximum < 1):
            raise IntegrationError('LOG_LIMIT_INVALID')
        if (timeout is not None or maximum is not None) and not cfg.get('limit_source'):
            raise IntegrationError('LIMIT_SOURCE_REQUIRED')
        # Only a local Docker socket endpoint may be explicitly configured.
        env = cfg.get('environment', {})
        if not isinstance(env, dict) or set(env) - {'PATH', 'DOCKER_HOST'} or any(not isinstance(v, str) or '\0' in v for v in env.values()):
            raise IntegrationError('CLEAN_ENVIRONMENT_REQUIRED')
        if 'DOCKER_HOST' in env and (not effects['docker'] or not env['DOCKER_HOST'].startswith('unix:///')):
            raise IntegrationError('LOCAL_DOCKER_ENDPOINT_REQUIRED')
        return source, cwd, exe, output, executable_identity

    def _receipt(self, db, operation, attempt, snapshot, revision, binding, cfg):
        snap = self._effect_selector(db, snapshot)
        if snap['state'] != 'ACKED' or not snap['receipt']:
            raise IntegrationError('CHECKOUT_RECEIPT_UNKNOWN')
        request, receipt = json.loads(snap['request']), json.loads(snap['receipt'])
        if request.get('kind') != 'snapshot' or receipt.get('state') != 'SNAPSHOT' or receipt.get('independent') is not True or request.get('revision') != revision or receipt.get('revision') != revision or request.get('path') != receipt.get('path'):
            raise IntegrationError('CHECKOUT_RECEIPT_MISMATCH')
        source = _path(self.router.workspace)
        identity = _identity(source)
        ref = _git(source, 'symbolic-ref', '-q', 'HEAD').decode().strip()
        if identity != request.get('identity') or binding is not None and binding.get('source_ref') != ref:
            raise IntegrationError('SOURCE_IDENTITY_MISMATCH')
        scope = self.router._scope(db)
        self._same_run(db, snap, scope)
        if binding is not None:
            candidates = [self._effect_selector(db, binding.get('commit_effect'))]
        else:
            candidates = db.execute("SELECT * FROM c_git_effect WHERE state='ACKED' AND receipt IS NOT NULL ORDER BY id").fetchall()
        selected, commit = None, None
        failure = 'INDEPENDENT_PEER_REVISION_REQUIRED'
        for row in candidates:
            candidate_request, candidate = json.loads(row['request']), json.loads(row['receipt'])
            if row['state'] != 'ACKED' or candidate_request.get('kind') != 'commit' or candidate.get('state') != 'COMMITTED' or candidate.get('commit') != revision:
                continue
            if candidate.get('git_identity') != identity or candidate_request.get('identity') != identity or candidate.get('repo') != str(source) or candidate.get('ref') != ref:
                failure = 'SOURCE_IDENTITY_MISMATCH'
                continue
            peer = db.execute('SELECT * FROM c_work WHERE id=?', (row['operation'],)).fetchone()
            if not peer or peer['seat'] == self.router.seat:
                continue
            try:
                self._same_run(db, row, scope)
            except IntegrationError as error:
                failure = str(error)
                continue
            selected, commit = row, candidate
            break
        if selected is None:
            raise IntegrationError(failure)
        tree = _git(source, 'rev-parse', revision + '^{tree}').decode().strip()
        if tree != commit.get('tree') or _git(source, 'rev-parse', revision + '^').decode().strip() != commit.get('parent'):
            raise IntegrationError('PEER_REVISION_MISMATCH')
        checkout = _path(receipt.get('path'))
        bound = _path(cfg.get('checkout_root'), exists=False)
        if not checkout.is_relative_to(bound) or checkout == bound or checkout == source:
            raise IntegrationError('CHECKOUT_ESCAPE')
        _identity(checkout)
        if _git(checkout, 'rev-parse', 'HEAD').decode().strip() != revision or _tree(checkout, revision, 'CHECKOUT', exact=True) != tree:
            raise IntegrationError('CHECKOUT_DIRTY')
        return checkout, {'commit_effect': selected['id'], 'source_ref': ref}

    def _view(self, db, row):
        state = row['state']
        if state not in TERMINAL and (row['owner'] != self.token or row['id'] not in self.jobs or self.jobs[row['id']].done.is_set()):
            state = 'UNKNOWN'
            db.execute("UPDATE c_verification_effect SET state='UNKNOWN' WHERE id=?", (row['id'],))
            Mailbox.event(db, row['operation'], 'VERIFICATION_UNKNOWN', {'id': row['id'], 'cause': 'ownership_or_result_uncertain'})
        return {'id': row['id'], 'state': state, 'result': json.loads(row['result']) if row['result'] else None}

    def start(self, operation, attempt, effect_id, *, check_id, checkout_receipt, revision):
        """Nonblocking job after validation. Use start_async in an SDK loop."""
        try:
            return self._start(operation, attempt, effect_id, check_id=check_id, checkout_receipt=checkout_receipt, revision=revision)
        except IntegrationError:
            raise
        except Exception:
            raise IntegrationError('VERIFICATION_VALIDATION_UNKNOWN') from None

    def _start(self, operation, attempt, effect_id, *, check_id, checkout_receipt, revision):
        if not all(isinstance(x, str) and x for x in (effect_id, check_id)) or not isinstance(checkout_receipt, (str, dict)):
            raise IntegrationError('VERIFICATION_ID_REQUIRED')
        _oid(revision)
        request = {'check_id': check_id, 'checkout_receipt': checkout_receipt, 'revision': revision, 'grant': self.router.grant_id, 'run': self.router.run_id}
        with self.lock:
            if self.closed:
                raise IntegrationError('BROKER_CLOSED')
            with self.owner.transaction(self.owner.epoch) as db:
                snapshot = self._effect_selector(db, checkout_receipt)['id']
                request['checkout_receipt'] = snapshot
                cfg, binding = self._authorize(db, operation, attempt, check_id, snapshot)
                row = db.execute('SELECT * FROM c_verification_effect WHERE id=?', (effect_id,)).fetchone()
                if row:
                    if row['operation'] != operation or row['attempt'] != attempt or row['request'] != encode(request):
                        raise IntegrationError('VERIFICATION_EFFECT_CONFLICT')
                    return self._view(db, row)
                if db.execute("SELECT 1 FROM c_verification_effect WHERE run_id=? AND state='UNKNOWN'", (self.router.run_id,)).fetchone():
                    raise IntegrationError('VERIFICATION_UNKNOWN_FENCE')
                # Durable intents from another instance cannot be bypassed with a new ID.
                pending = db.execute("SELECT v.* FROM c_verification_effect v JOIN c_work w ON w.id=v.operation WHERE v.run_id=? AND w.seat=? AND v.state NOT IN ('SUCCEEDED','FAILED','CANCELLED','TIMED_OUT','UNKNOWN')", (self.router.run_id, self.router.seat)).fetchall()
                if any(self._view(db, r)['state'] == 'UNKNOWN' for r in pending):
                    raise IntegrationError('VERIFICATION_UNKNOWN_FENCE')
                executable_identity = self._configuration(cfg)[4]
                checkout, provenance = self._receipt(db, operation, attempt, snapshot, revision, binding, cfg)
                config = encode({'check': cfg, 'restriction': binding, 'provenance': provenance, 'executable': executable_identity})
                output = str(Path(cfg['output_root']) / uuid.uuid4().hex)
                db.execute("INSERT INTO c_verification_effect VALUES(?,?,?,?,?,?,'INTENT',?,?,NULL,NULL)", (effect_id, operation, attempt, self.router.run_id, encode(request), config, self.token, output))
                Mailbox.event(db, operation, 'VERIFICATION_INTENT', {'attempt': attempt, 'id': effect_id, 'request': request, 'config_sha256': digest(config.encode()), 'output': output, 'effects': cfg['effects'], 'limit_source': cfg.get('limit_source')})
            job = _Job()
            self.jobs[effect_id] = job
            job.thread = threading.Thread(target=self._run, args=(job, operation, attempt, effect_id, request, cfg, binding, provenance, executable_identity, checkout, Path(output)), daemon=True)
            try:
                job.thread.start()
            except BaseException:
                job.done.set()
                raise IntegrationError('VERIFICATION_START_UNKNOWN') from None
            return {'id': effect_id, 'state': 'INTENT', 'result': None}

    async def start_async(self, *args, **kwargs):
        return await asyncio.to_thread(self.start, *args, **kwargs)

    def _live(self, db, operation, attempt, request, cfg, binding):
        try:
            current, current_binding = self._authorize(db, operation, attempt, request['check_id'], request['checkout_receipt'])
            return current == cfg and current_binding == binding
        except IntegrationError:
            return False

    @staticmethod
    def _kill(job):
        # Child remains ours while unreaped. Never signal persisted/stale PID.
        p = job.process
        if p is not None and p.returncode is None:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def _run(self, job, operation, attempt, identifier, request, cfg, binding, provenance, executable_identity, checkout, output):
        state, reason, code = 'UNKNOWN', 'PROCESS_OR_RESULT_UNCERTAIN', None
        artifacts = {}
        try:
            _path(str(output.parent), exists=False).mkdir(mode=0o700, parents=True, exist_ok=True)
            _path(str(output.parent))
            output.mkdir(mode=0o700)  # fresh exclusive directory, never reused
            for name in ('home', 'tmp', 'docker-config'):
                (output / name).mkdir(mode=0o700)
            source, cwd, exe, root, current_executable = self._configuration(cfg)
            if current_executable != executable_identity:
                raise IntegrationError('EXECUTABLE_TARGET_CHANGED')
            argv = [str(exe), *[str(checkout) if x == '{checkout}' else str(output) if x == '{output}' else x for x in cfg['argv']]]
            env = _env()
            env.update(cfg.get('environment', {}))
            env.update(HOME=str(output / 'home'), TMPDIR=str(output / 'tmp'), DOCKER_CONFIG=str(output / 'docker-config'))
            with (output / 'stdout').open('xb') as stdout, (output / 'stderr').open('xb') as stderr:
                os.chmod(output / 'stdout', 0o600)
                os.chmod(output / 'stderr', 0o600)
                with self.owner.transaction(self.owner.epoch) as db:
                    if job.cancel.is_set() or not self._live(db, operation, attempt, request, cfg, binding):
                        state, reason = 'CANCELLED', 'STOP_REVOKE_OR_CANCEL'
                    else:
                        # Revalidate exact receipt and source immediately before effect.
                        self._receipt(db, operation, attempt, request['checkout_receipt'], request['revision'], provenance, cfg)
                        job.process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, start_new_session=True, close_fds=True)
                        pid = job.process.pid
                        proc = {'pid': pid, 'pgid': pid, 'start_ticks': Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19], 'owner': self.token}
                        db.execute("UPDATE c_verification_effect SET state='RUNNING',process=? WHERE id=?", (encode(proc), identifier))
                        Mailbox.event(db, operation, 'VERIFICATION_SPAWN', {'id': identifier, 'process': proc, 'argv': argv, 'cwd': str(cwd), 'source_head': cfg['source_head'], 'source_tree': cfg['source_tree'], 'executable': executable_identity})
                began = time.monotonic()
                if job.process is not None:
                    while True:
                        with self.owner.transaction(self.owner.epoch) as db:
                            live = self._live(db, operation, attempt, request, cfg, binding)
                        if job.cancel.is_set() or not live:
                            state, reason = 'CANCELLED', 'STOP_REVOKE_OR_CANCEL'
                            self._kill(job)
                        elif cfg.get('timeout_seconds') is not None and time.monotonic() - began >= cfg['timeout_seconds']:
                            state, reason = 'TIMED_OUT', 'EXPLICIT_CONTROLLER_TIMEOUT'
                            self._kill(job)
                        elif cfg.get('max_log_bytes') is not None and sum((output / n).stat().st_size for n in ('stdout', 'stderr')) > cfg['max_log_bytes']:
                            state, reason = 'FAILED', 'EXPLICIT_CONTROLLER_LOG_LIMIT'
                            self._kill(job)
                        # Observe without reaping: the leader PID still pins our
                        # group identity while cleaning up checker descendants.
                        exited = os.waitid(os.P_PID, job.process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                        if exited is not None:
                            self._kill(job)
                            code = job.process.wait(timeout=3)
                            break
                        job.cancel.wait(.05)
                    if state not in {'CANCELLED', 'TIMED_OUT', 'FAILED'}:
                        state, reason = ('FAILED', 'CHECKER_NONZERO') if code else ('UNKNOWN', 'REPORT_UNVERIFIED')
            for name in ('stdout', 'stderr'):
                artifacts[name] = _hash_file(output / name)
            if code is not None and state not in {'CANCELLED', 'TIMED_OUT'}:
                for name in cfg['report_files']:
                    artifacts[name] = _hash_file(output / name)
                parser = self.parsers[cfg['report_contract']]
                if hasattr(parser, 'parse_request'):
                    accepted = parser.parse_request(output, code, _immutable({'request': request, 'config': cfg}))
                else:
                    accepted = parser(output, code)  # legacy trusted two-argument parser
                if type(accepted) is not bool:
                    raise IntegrationError('REPORT_CONTRACT_UNKNOWN')
                if code == 0 and reason != 'EXPLICIT_CONTROLLER_LOG_LIMIT':
                    state, reason = ('SUCCEEDED', 'DECLARED_REPORT_ACCEPTED') if accepted else ('FAILED', 'DECLARED_REPORT_REJECTED')
        except BaseException:
            # Exceptions and checker stdout can contain credentials. Safe reason only.
            self._kill(job)
            if job.process is not None:
                try:
                    code = job.process.wait(timeout=3)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            if state not in {'CANCELLED', 'TIMED_OUT'}:
                state, reason = 'UNKNOWN', 'PROCESS_OR_REPORT_UNCERTAIN'
            for name in ('stdout', 'stderr'):
                try:
                    artifacts[name] = _hash_file(output / name)
                except (OSError, IntegrationError):
                    pass
        finally:
            result = {'state': state, 'reason': reason, 'accepted': state == 'SUCCEEDED', 'exit_code': code, 'output': str(output), 'artifacts': artifacts, 'report_contract': cfg['report_contract'], 'limit_source': cfg.get('limit_source'), 'effects': cfg['effects'], 'revision': request['revision'], 'checkout_receipt': request['checkout_receipt'], 'source_commit_effect': provenance['commit_effect'], 'run_id': self.router.run_id}
            try:
                with self.owner.transaction(self.owner.epoch) as db:
                    raw = encode(result).encode()
                    h = Mailbox.artifact(db, raw)
                    db.execute('UPDATE c_verification_effect SET state=?,result=? WHERE id=? AND owner=?', (state, encode(result), identifier, self.token))
                    Mailbox.event(db, operation, 'VERIFICATION_RESULT', {'attempt': attempt, 'id': identifier, 'result_ref': h, 'state': state})
            except Exception:
                # A missing durable result leaves the intent fenced UNKNOWN on
                # status. Never print transaction exceptions or blindly replay.
                pass
            finally:
                job.done.set()

    def status(self, operation, attempt, effect_id):
        with self.lock, self.owner.transaction(self.owner.epoch) as db:
            self._work(db, operation, attempt)
            row = db.execute('SELECT * FROM c_verification_effect WHERE id=?', (effect_id,)).fetchone()
            if not row or row['operation'] != operation or row['attempt'] != attempt:
                raise IntegrationError('VERIFICATION_EFFECT_NOT_FOUND')
            return self._view(db, row)

    async def wait(self, operation, attempt, effect_id):
        """Cancelling the await requests cancellation of only this owned job."""
        try:
            while True:
                value = await asyncio.to_thread(self.status, operation, attempt, effect_id)
                if value['state'] in TERMINAL:
                    return value['result'] or {'state': 'UNKNOWN', 'accepted': False, 'reason': 'RESULT_UNCERTAIN'}
                await asyncio.sleep(.05)
        except asyncio.CancelledError:
            await asyncio.to_thread(self.cancel, operation, attempt, effect_id)
            raise

    def cancel(self, operation, attempt, effect_id):
        with self.lock:
            value = self.status(operation, attempt, effect_id)
            if value['state'] in TERMINAL:
                return value
            job = self.jobs.get(effect_id)
            if not job:
                raise IntegrationError('PROCESS_OWNERSHIP_UNKNOWN')
            with self.owner.transaction(self.owner.epoch) as db:
                Mailbox.event(db, operation, 'VERIFICATION_CANCEL_INTENT', {'attempt': attempt, 'id': effect_id})
            job.cancel.set()
            return value

    def public_result(self, operation, attempt, effect_id):
        """Guarded fixed projection; never expose raw logs, paths or parser text."""
        value = self.status(operation, attempt, effect_id)
        result = value['result'] or {}
        return {'id': effect_id, 'state': value['state'], 'accepted': result.get('accepted', False), 'exit_code': result.get('exit_code'), 'reason': result.get('reason', 'RESULT_PENDING'), 'revision': result.get('revision')}

    def close(self):
        """Controller shutdown; no persisted process identities are signalled."""
        with self.lock:
            self.closed = True
            jobs = list(self.jobs.values())
            for job in jobs:
                if not job.done.is_set():
                    job.cancel.set()
        for job in jobs:
            if job.thread:
                job.thread.join(timeout=5)
                if job.thread.is_alive():
                    raise IntegrationError('VERIFICATION_SHUTDOWN_UNKNOWN')
