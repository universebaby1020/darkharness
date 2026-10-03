"""Core-owned, controller-Grant-bound local Git plumbing; never a shell escape.

Does not generate source. Commits byte snapshots of existing seat-requested files.
Same-UID filesystem tampering is not an OS isolation boundary. The broker serializes
its effects and checks identities/readback, but does not claim isolation from a
malicious concurrent same-UID writer. Uncertain effects stay UNKNOWN.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import tempfile

from .mailbox import IntegrationError, Mailbox, digest, encode

OID = re.compile(r'[0-9a-f]{40}')


def literal_path(value):
    if not isinstance(value, str) or not value or '\\' in value or '\x00' in value or '\n' in value or '\r' in value:
        raise IntegrationError('LITERAL_PATH_REQUIRED')
    p = PurePosixPath(value)
    if p.is_absolute() or str(p) != value or any(x in {'.', '..', '.git'} for x in p.parts) or value.startswith(':'):
        raise IntegrationError('LITERAL_PATH_REQUIRED')
    return value


def regular_path(root, rel):
    literal_path(rel)
    p = root
    for part in PurePosixPath(rel).parts:
        p = p / part
        if p.is_symlink() or (p != root and p.is_dir() and (p / '.git').exists()):
            raise IntegrationError('SYMLINK_OR_NESTED_REPO_DENIED')
    if not p.resolve().is_relative_to(root) or not stat.S_ISREG(p.lstat().st_mode):
        raise IntegrationError('REGULAR_FILE_REQUIRED')
    return p


def result_repo_boundary(workspace, result_repo=None):
    """Explicit repository below the existing workspace ceiling; None is legacy.

    No filesystem creation or repository discovery. Sibling repositories are not
    authority for this broker. Reject links and an enclosing Git repository.
    """
    ceiling = Path(workspace)
    if result_repo is None:
        return ceiling
    if not isinstance(result_repo, str) or not result_repo or '\0' in result_repo:
        raise IntegrationError('RESULT_REPO_CANONICAL_REQUIRED')
    root = Path(result_repo)
    if (not ceiling.is_absolute() or ceiling != ceiling.resolve() or
            not root.is_absolute() or str(root) != result_repo or '..' in root.parts or
            root != root.resolve() or root == ceiling or not root.is_relative_to(ceiling)):
        raise IntegrationError('RESULT_REPO_OUTSIDE_WORKSPACE_OR_NONCANONICAL')
    for p in [root, *root.parents]:
        if p.is_symlink():
            raise IntegrationError('RESULT_REPO_SYMLINK_DENIED')
        if p != root and p.is_relative_to(ceiling) and (p / '.git').exists():
            raise IntegrationError('RESULT_REPO_NESTED_DENIED')
    return root


class LocalGitBroker:
    def __init__(self, mailbox, router, actor, email, assigned_workspace):
        self.box, self.router, self.owner = mailbox, router, mailbox.owner
        self.root = result_repo_boundary(router.workspace, getattr(router, 'configured_result_repo', None))
        self.assigned = Path(assigned_workspace).resolve()
        if not self.root.is_relative_to(self.assigned) or not actor or any(c in actor for c in '\n\r\x00<>') or not re.fullmatch(r'[a-zA-Z0-9._+-]+@[a-zA-Z0-9.-]+\.invalid', email):
            raise IntegrationError('ACTOR_WORKSPACE_INVALID')
        self.actor, self.email = actor, email
        with self.owner.transaction(self.owner.epoch) as db:
            db.execute('CREATE TABLE IF NOT EXISTS c_git_effect(id TEXT PRIMARY KEY,operation TEXT,attempt TEXT,request TEXT,state TEXT,candidate TEXT,receipt TEXT)')

    def _env(self, extra=None):
        # Do not inherit credentials, Git injection variables, login or global config.
        env = {'PATH': '/usr/bin:/bin', 'HOME': '/nonexistent', 'LC_ALL': 'C',
               'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null',
               'GIT_TERMINAL_PROMPT': '0', 'GIT_NO_REPLACE_OBJECTS': '1',
               'GIT_AUTHOR_NAME': self.actor, 'GIT_COMMITTER_NAME': self.actor,
               'GIT_AUTHOR_EMAIL': self.email, 'GIT_COMMITTER_EMAIL': self.email}
        env.update(extra or {})
        return env

    def _git(self, *argv, data=None, extra=None, gitdir=None):
        # Only private fixed plumbing argv calls. No seat options/executables.
        cmd = ['/usr/bin/git', '--no-optional-locks', '--git-dir=' + str(gitdir or self.root / '.git'),
               '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false',
               '-c', 'commit.gpgsign=false', '-c', 'core.logAllRefUpdates=true',
               '-c', 'gc.auto=0', '-c', 'maintenance.auto=false', *argv]
        try:
            run = subprocess.run(cmd, cwd=self.root, env=self._env(extra), input=data,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            raise IntegrationError('GIT_PROCESS_UNCERTAIN') from None
        if run.returncode:
            # Raw stderr may contain path/config/credential data; never return it.
            raise IntegrationError('GIT_PLUMBING_FAILED')
        return run.stdout

    def _identity(self, cwd):
        if os.name != 'posix' or not isinstance(cwd, str) or not Path(cwd).is_absolute() or Path(cwd).resolve() != self.root or Path(cwd) != self.root:
            raise IntegrationError('CANONICAL_REPO_CWD_REQUIRED')
        for p in [self.root, *self.root.parents]:
            if p.is_symlink():
                raise IntegrationError('REPO_SYMLINK_DENIED')
        gd = self.root / '.git'
        if gd.is_symlink() or not gd.is_dir():
            raise IntegrationError('DIRECT_GIT_DIRECTORY_REQUIRED')
        # No linked worktrees, alternate object stores, grafts or replacements.
        for rel in ['commondir', 'objects/info/alternates', 'info/grafts', 'refs/replace']:
            p = gd / rel
            if p.exists() and (not p.is_dir() or any(p.iterdir())):
                raise IntegrationError('INDIRECT_GIT_STORE_DENIED')
        for p in gd.rglob('*'):
            if p.is_symlink():
                raise IntegrationError('GIT_SYMLINK_DENIED')
        if self._git('rev-parse', '--show-toplevel').decode().strip() != str(self.root):
            raise IntegrationError('REPO_ROOT_MISMATCH')
        if self._git('rev-parse', '--is-bare-repository').strip() != b'false':
            raise IntegrationError('BARE_REPO_DENIED')
        # Include config can redirect objects/refs or introduce runtime hooks.
        names = self._git('config', '--local', '--no-includes', '--name-only', '--list').decode().splitlines()
        if any(n.startswith(('include.', 'includeif.', 'extensions.')) or n in {'core.worktree', 'core.bare'} and self._git('config', '--local', '--get', n).strip() not in {b'false'} for n in names):
            raise IntegrationError('INDIRECT_REPO_CONFIG_DENIED')
        rootst, gitst = self.root.stat(), gd.stat()
        return [str(self.root), rootst.st_dev, rootst.st_ino, str(gd), gitst.st_dev, gitst.st_ino]

    def _head(self):
        ref = self._git('symbolic-ref', '-q', 'HEAD').decode().strip()
        if not re.fullmatch(r'refs/heads/[A-Za-z0-9_./-]+', ref) or '..' in ref or ref.endswith('/'):
            raise IntegrationError('BRANCH_REF_REQUIRED')
        oid = self._git('rev-parse', '--verify', 'HEAD^{commit}').decode().strip()
        if not OID.fullmatch(oid):
            raise IntegrationError('SHA1_REPO_REQUIRED')
        return ref, oid

    def _tree(self, revision):
        if not isinstance(revision, str) or not OID.fullmatch(revision):
            raise IntegrationError('EXACT_REVISION_REQUIRED')
        if self._git('rev-parse', '--verify', revision + '^{commit}').decode().strip() != revision:
            raise IntegrationError('EXACT_COMMIT_REQUIRED')
        entries = []
        for entry in self._git('ls-tree', '-rz', '--full-tree', revision).split(b'\x00'):
            if not entry:
                continue
            meta, path = entry.split(b'\t', 1)
            mode, typ, oid = meta.decode().split()
            if typ != 'blob' or mode not in {'100644', '100755'}:
                raise IntegrationError('SUBMODULE_OR_SYMLINK_TREE_DENIED')
            try:
                rel = literal_path(path.decode('utf-8'))
            except UnicodeError:
                raise IntegrationError('UTF8_PATH_REQUIRED') from None
            entries.append((mode, oid, rel))
        return entries

    def _authorize(self, db, operation, attempt, capability):
        scope = self.router._scope(db)
        cap = scope.get(capability) if scope else None
        work = db.execute('SELECT * FROM c_work WHERE id=?', (operation,)).fetchone()
        if not isinstance(cap, dict) or not work or work['attempt'] != attempt or work['seat'] != self.router.seat or work['room'] != self.router.room or work['state'] != 'RUNNING' or work['delivery'] not in {'STARTED', 'DISPATCHING'}:
            raise IntegrationError('TYPED_GIT_GRANT_REQUIRED')
        return cap

    @contextmanager
    def _lock(self):
        p = self.root / '.git/dh-broker.lock'
        try:
            fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except OSError:
            raise IntegrationError('GIT_BROKER_BUSY') from None
        try:
            yield
        finally:
            os.close(fd)
            p.unlink()

    def _intent(self, operation, attempt, identifier, request, capability):
        if not isinstance(identifier, str) or not identifier:
            raise IntegrationError('EFFECT_ID_REQUIRED')
        with self.owner.transaction(self.owner.epoch) as db:
            cap = self._authorize(db, operation, attempt, capability)
            old = db.execute('SELECT * FROM c_git_effect WHERE id=?', (identifier,)).fetchone()
            if old:
                if old['operation'] != operation or old['attempt'] != attempt or old['request'] != encode(request):
                    raise IntegrationError('GIT_EFFECT_CONFLICT')
                if old['state'] != 'ACKED':
                    raise IntegrationError('GIT_EFFECT_UNKNOWN')
                return cap, json.loads(old['receipt'])
            db.execute("INSERT INTO c_git_effect VALUES(?,?,?,?,'UNKNOWN',NULL,NULL)", (identifier, operation, attempt, encode(request)))
            Mailbox.event(db, operation, 'LOCAL_GIT_INTENT', {'attempt': attempt, 'id': identifier, 'request': request})
            return cap, None

    def _ack(self, identifier, receipt):
        with self.owner.transaction(self.owner.epoch) as db:
            row = db.execute('SELECT * FROM c_git_effect WHERE id=?', (identifier,)).fetchone()
            db.execute("UPDATE c_git_effect SET state='ACKED',receipt=? WHERE id=?", (encode(receipt), identifier))
            Mailbox.event(db, row['operation'], 'LOCAL_GIT_RECEIPT', {'attempt': row['attempt'], 'id': identifier, 'receipt': receipt})
        return receipt

    @contextmanager
    def _index_lock(self):
        path = self.root / '.git/index.lock'
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except OSError:
            raise IntegrationError('GIT_INDEX_BUSY') from None
        try:
            yield fd, path
        finally:
            os.close(fd)
            if path.exists():
                path.unlink()

    def _index_bytes(self):
        p = self.root / '.git/index'
        return p.read_bytes() if p.exists() else b''

    def _install_index(self, fd, lockpath, raw):
        with os.fdopen(os.dup(fd), 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(lockpath, self.root / '.git/index')
        directory = os.open(self.root / '.git', os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def commit(self, operation, attempt, identifier, *, cwd, paths, message, expected_head):
        if not isinstance(paths, list) or not paths or len(paths) != len(set(paths)) or not isinstance(message, str) or not message.strip() or len(message.encode()) > 8192 or '\x00' in message or not isinstance(expected_head, str) or not OID.fullmatch(expected_head):
            raise IntegrationError('COMMIT_INPUT_INVALID')
        paths = sorted(literal_path(p) for p in paths)
        identity = self._identity(cwd)
        request = {'kind': 'commit', 'identity': identity, 'paths': paths, 'message': message, 'expected_head': expected_head}
        # Validate rights/scope before creating intent (invalid paths cause no effect).
        with self.owner.transaction(self.owner.epoch) as db:
            cap = self._authorize(db, operation, attempt, 'local_git_write')
            directories = cap.get('directories', [])
            if not isinstance(directories, list):
                raise IntegrationError('GIT_SCOPE_INVALID')
            for directory in directories:
                if directory != '.':
                    literal_path(directory)
            if any(p not in cap.get('paths', []) and not any(d == '.' or p.startswith(d + '/') for d in directories) for p in paths):
                raise IntegrationError('GIT_PATH_NOT_GRANTED')
            review = self.router._scope(db).get('review_snapshot', {})
            if isinstance(review, dict) and review.get('scratch') and any((self.root / p).is_relative_to(Path(review['scratch'])) for p in paths):
                raise IntegrationError('REVIEW_SCRATCH_NOT_SOURCE')
            old = db.execute('SELECT id FROM c_git_effect WHERE id=?', (identifier,)).fetchone()
        if old:
            return self._intent(operation, attempt, identifier, request, 'local_git_write')[1]
        with self._lock(), self._index_lock() as (indexfd, indexlock):
            index_before = self._index_bytes()
            ref, oldhead = self._head()
            if oldhead != expected_head:
                raise IntegrationError('HEAD_CHANGED')
            self._tree(oldhead)
            files = [(p, regular_path(self.root, p).read_bytes(), regular_path(self.root, p).stat().st_mode) for p in paths]
            _, previous = self._intent(operation, attempt, identifier, request, 'local_git_write')
            if previous:
                return previous
            with tempfile.TemporaryDirectory(prefix='dh-index-') as td:
                env = {'GIT_INDEX_FILE': str(Path(td) / 'index')}
                self._git('read-tree', oldhead, extra=env)
                indexenv = {'GIT_INDEX_FILE': str(Path(td) / 'selected-index')}
                if index_before:
                    Path(indexenv['GIT_INDEX_FILE']).write_bytes(index_before)
                else:
                    self._git('read-tree', oldhead, extra=indexenv)
                for p, raw, mode in files:
                    blob = self._git('hash-object', '-w', '--stdin', data=raw).decode().strip()
                    entry = ('100755' if mode & 0o111 else '100644') + ' ' + blob + '\t' + p + '\x00'
                    self._git('update-index', '-z', '--index-info', data=entry.encode(), extra=env)
                    # Clear selected conflict stages, then install the committed blob.
                    remove = '0 ' + '0' * 40 + '\t' + p + '\x00'
                    self._git('update-index', '-z', '--index-info', data=(remove + entry).encode(), extra=indexenv)
                index_after = Path(indexenv['GIT_INDEX_FILE']).read_bytes()
                tree = self._git('write-tree', extra=env).decode().strip()
                if tree == self._git('rev-parse', oldhead + '^{tree}').decode().strip():
                    raise IntegrationError('NO_SCOPED_CHANGE')
                commit = self._git('commit-tree', tree, '-p', oldhead, data=message.encode()).decode().strip()
                candidate = {'state': 'COMMITTED', 'repo': str(self.root), 'git_identity': identity, 'ref': ref, 'parent': oldhead, 'commit': commit, 'tree': tree, 'paths': paths}
                with self.owner.transaction(self.owner.epoch) as db:
                    self._authorize(db, operation, attempt, 'local_git_write')
                    candidate.update(index_before=Mailbox.artifact(db, index_before), index_after=Mailbox.artifact(db, index_after))
                    db.execute('UPDATE c_git_effect SET candidate=? WHERE id=?', (encode(candidate), identifier))
                    Mailbox.event(db, operation, 'LOCAL_GIT_CANDIDATE', {'attempt': attempt, 'id': identifier, 'candidate': candidate})
                if self._identity(cwd) != identity or self._head() != (ref, oldhead) or self._index_bytes() != index_before or any(regular_path(self.root, p).read_bytes() != raw for p, raw, mode in files):
                    raise IntegrationError('GIT_SOURCE_CHANGED_UNKNOWN')
                # CAS is the sole canonical effect. No add/commit/checkout hooks.
                with self.owner.transaction(self.owner.epoch) as db:
                    self._authorize(db, operation, attempt, 'local_git_write')
                    self._git('update-ref', '--no-deref', '-m', 'DarkHarness local Git broker', ref, commit, oldhead)
                    self._install_index(indexfd, indexlock, index_after)
                if self._head() != (ref, commit) or self._identity(cwd) != identity:
                    raise IntegrationError('GIT_RECEIPT_UNKNOWN')
                return self._ack(identifier, candidate)

    def reconcile(self, identifier):
        """Controller-side exact current-ref readback; never repeats an effect."""
        with self.owner.transaction(self.owner.epoch) as db:
            row = db.execute('SELECT * FROM c_git_effect WHERE id=?', (identifier,)).fetchone()
            if not row or not row['candidate']:
                raise IntegrationError('GIT_EFFECT_UNKNOWN')
            candidate = json.loads(row['candidate'])
        if candidate.get('state') != 'COMMITTED' or self._identity(str(self.root)) != candidate['git_identity'] or self._head() != (candidate['ref'], candidate['commit']):
            raise IntegrationError('GIT_EFFECT_UNKNOWN')
        if self._git('rev-parse', candidate['commit'] + '^').decode().strip() != candidate['parent'] or self._git('rev-parse', candidate['commit'] + '^{tree}').decode().strip() != candidate['tree']:
            raise IntegrationError('GIT_EFFECT_UNKNOWN')
        with self._lock(), self._index_lock() as (fd, lockpath):
            current = digest(self._index_bytes())
            if current == candidate['index_before']:
                if not self.router.active():
                    raise IntegrationError('GRANT_INACTIVE')
                with self.owner.transaction(self.owner.epoch) as db:
                    raw = db.execute('SELECT body FROM c_artifact WHERE hash=?', (candidate['index_after'],)).fetchone()
                if raw is None or digest(raw[0]) != candidate['index_after']:
                    raise IntegrationError('GIT_INDEX_EVIDENCE_UNKNOWN')
                self._install_index(fd, lockpath, raw[0])
            elif current != candidate['index_after']:
                raise IntegrationError('GIT_INDEX_CHANGED_UNKNOWN')
        return self._ack(identifier, candidate)

    def snapshot(self, operation, attempt, identifier, *, cwd, revision, name):
        identity = self._identity(cwd)
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', name):
            raise IntegrationError('SNAPSHOT_NAME_INVALID')
        entries = self._tree(revision)
        with self.owner.transaction(self.owner.epoch) as db:
            cap = self._authorize(db, operation, attempt, 'review_snapshot')
        scratch = Path(cap.get('scratch', ''))
        if not scratch.is_absolute() or scratch != scratch.resolve() or not scratch.is_relative_to(self.assigned):
            raise IntegrationError('BOUNDED_REVIEW_SCRATCH_REQUIRED')
        for p in [scratch, *scratch.parents]:
            if p.is_symlink():
                raise IntegrationError('SCRATCH_SYMLINK_DENIED')
        max_bytes, max_files = cap.get('max_bytes'), cap.get('max_files')
        if (max_bytes is not None and (type(max_bytes) is not int or max_bytes < 1)) or (max_files is not None and (type(max_files) is not int or max_files < 1)):
            raise IntegrationError('SNAPSHOT_LIMIT_INVALID')
        if max_files is not None and len(entries) > max_files:
            raise IntegrationError('SNAPSHOT_LIMIT_EXCEEDED')
        files, size = [], 0
        for mode, oid, rel in entries:
            length = int(self._git('cat-file', '-s', oid))
            size += length
            if max_bytes is not None and size > max_bytes:
                raise IntegrationError('SNAPSHOT_LIMIT_EXCEEDED')
            files.append((mode, oid, rel, self._git('cat-file', 'blob', oid)))
        objects = {oid: ('blob', raw) for mode, oid, rel, raw in files}
        trees = {self._git('rev-parse', revision + '^{tree}').decode().strip()}
        for entry in self._git('ls-tree', '-rtz', revision).split(b'\x00'):
            if entry and entry.split(b'\t', 1)[0].split()[1] == b'tree':
                trees.add(entry.split(b'\t', 1)[0].split()[2].decode())
        for oid in trees | {revision}:
            typ = 'commit' if oid == revision else 'tree'
            length = int(self._git('cat-file', '-s', oid))
            if max_bytes is not None and size + sum(len(raw) for typ, raw in objects.values()) + length > max_bytes:
                raise IntegrationError('SNAPSHOT_LIMIT_EXCEEDED')
            objects[oid] = (typ, self._git('cat-file', typ, oid))
        if max_bytes is not None and size + sum(len(raw) for typ, raw in objects.values()) > max_bytes:
            raise IntegrationError('SNAPSHOT_LIMIT_EXCEEDED')
        request = {'kind': 'snapshot', 'identity': identity, 'revision': revision, 'path': str(scratch / name)}
        _, previous = self._intent(operation, attempt, identifier, request, 'review_snapshot')
        if previous:
            return previous
        target = scratch / name
        scratch.mkdir(parents=True, exist_ok=True)
        try:
            target.mkdir()  # exclusive, never overwrite or remove an old checkout
        except OSError:
            raise IntegrationError('SNAPSHOT_EXISTS_OR_UNKNOWN') from None
        # Independent bare plumbing initialized with no templates or hardlinks.
        self._git('init', '--quiet', '--template=', str(target), gitdir=target / '.git')
        dest = target / '.git'
        for mode, oid, rel, raw in files:
            actual = self._git('hash-object', '-w', '--stdin', data=raw, gitdir=dest).decode().strip()
            if actual != oid:
                raise IntegrationError('SNAPSHOT_OBJECT_UNKNOWN')
            p = target / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open('xb') as stream:
                stream.write(raw)
            p.chmod(0o755 if mode == '100755' else 0o644)
        # Exact shallow snapshot: copy canonical commit/tree bytes, not an
        # unbounded parent history. No clone, hardlinks, alternate or remote.
        for oid, (typ, raw) in objects.items():
            if self._git('hash-object', '-t', typ, '-w', '--stdin', data=raw, gitdir=dest).decode().strip() != oid:
                raise IntegrationError('SNAPSHOT_OBJECT_UNKNOWN')
        (dest / 'shallow').write_text(revision + '\n')
        (dest / 'HEAD').write_text(revision + '\n')
        self._git('update-ref', '--no-deref', 'HEAD', revision, gitdir=dest)
        self._git('read-tree', revision, gitdir=dest)
        if self._git('rev-parse', 'HEAD', gitdir=dest).decode().strip() != revision or self._identity(cwd) != identity or self._git('ls-tree', '-rz', '--full-tree', revision, gitdir=dest) != self._git('ls-tree', '-rz', '--full-tree', revision):
            raise IntegrationError('SNAPSHOT_RECEIPT_UNKNOWN')
        if any(regular_path(target, rel).read_bytes() != raw for mode, oid, rel, raw in files) or sum(regular_path(target, rel).stat().st_size for mode, oid, rel, raw in files) != size:
            raise IntegrationError('SNAPSHOT_READBACK_UNKNOWN')
        return self._ack(identifier, {'state': 'SNAPSHOT', 'revision': revision, 'path': str(target), 'files': len(entries), 'bytes': size, 'object_bytes': sum(len(raw) for typ, raw in objects.values()), 'independent': True, 'shallow': True})
