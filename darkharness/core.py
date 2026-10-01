"""Linux-only owner election and canonical ledger. SQLite is not OS isolation."""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import platform
import sqlite3
import subprocess
import sys
import time

from .ipc import canonical

TERMINAL = frozenset({"SUCCEEDED", "FAILED", "CANCELLED", "CLOSED_UNRESOLVED"})
STATES = TERMINAL | {"QUEUED", "RUNNING", "PAUSED"}


class Rejected(Exception):
    pass


def process_identity(pid=None):
    pid = os.getpid() if pid is None else int(pid)
    # comm may contain spaces and closing parentheses; fields follow the last ')'.
    stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return {"pid": pid, "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "start_marker": stat[19]}


def is_alive(identity):
    try:
        stat = Path(f"/proc/{int(identity['pid'])}/stat").read_text().rsplit(")", 1)[1].split()
        return stat[0] != "Z" and process_identity(identity["pid"]) == identity
    except (OSError, ValueError, KeyError, TypeError):
        return False


def environment(root):
    mount_type = subprocess.run(["/usr/bin/stat", "-f", "-c", "%T", str(root)], capture_output=True, text=True, check=True).stdout.strip()
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    return {"environment_id": hashlib.sha256(canonical([boot, os.getuid(), str(root)])).hexdigest(),
            "uid": os.getuid(), "kernel": platform.release(), "python": platform.python_version(),
            "interpreter": sys.executable, "boot_id": boot, "state_filesystem": mount_type,
            "lock_validation": "UNVERIFIED" if mount_type in {"9p", "drvfs"} else "LOCAL_OS_LOCK"}


class Store:
    def __init__(self, state_root):
        if sys.platform != "linux":
            raise Rejected("LINUX_REQUIRED")
        import fcntl
        self.root = Path(state_root)
        if not self.root.is_absolute():
            raise Rejected("ABSOLUTE_STATE_ROOT_REQUIRED")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.root.is_symlink():
            raise Rejected("STATE_ROOT_LINK")
        self.root = self.root.resolve()
        # The directory inode is the canonical lock, so deleting owner.lock does
        # not split election. A same-UID actor can still rename/replace a root;
        # canonical writes verify the original inode and reject that condition.
        self.lock_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        self.inode = (os.fstat(self.lock_fd).st_dev, os.fstat(self.lock_fd).st_ino)
        self.owner = False
        self.db = None
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.owner = True
        except BlockingIOError:
            return
        try:
            self.instance = process_identity()
            self.env = environment(self.root)
            for name in ["artifacts", "operations", "settings"]:
                (self.root / name).mkdir(mode=0o700, exist_ok=True)
            (self.root / "owner.lock").touch(mode=0o600, exist_ok=True)
            self.db = sqlite3.connect(self.root / "state.sqlite3", timeout=5, isolation_level=None, check_same_thread=False)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.executescript('''
                CREATE TABLE IF NOT EXISTS owner (id INTEGER PRIMARY KEY CHECK(id=1), epoch INTEGER NOT NULL, instance TEXT NOT NULL, environment TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS operations (id TEXT PRIMARY KEY, payload TEXT NOT NULL, attempt TEXT NOT NULL, state TEXT NOT NULL, revision INTEGER NOT NULL, pending INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT, operation TEXT, kind TEXT NOT NULL, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS controls (kind TEXT, id TEXT, body TEXT NOT NULL, revision INTEGER NOT NULL, PRIMARY KEY(kind,id));
                CREATE TABLE IF NOT EXISTS artifacts (id TEXT PRIMARY KEY, body BLOB NOT NULL);
            ''')
            self.db.execute("BEGIN IMMEDIATE")
            old = self.db.execute("SELECT epoch FROM owner WHERE id=1").fetchone()
            self.epoch = (old[0] if old else 0) + 1
            self.db.execute("INSERT OR REPLACE INTO owner VALUES(1,?,?,?)", (self.epoch, canonical(self.instance).decode(), canonical(self.env).decode()))
            self.db.execute("INSERT INTO events(operation,kind,body) VALUES(NULL,'OWNER_ACQUIRED',?)", (canonical(self.identity()).decode(),))
            self.db.commit()
        except BaseException:
            self.close()
            raise

    def identity(self):
        return {"epoch": self.epoch, "instance": self.instance, "environment": self.env}

    def existing_owner(self):
        # A competing starter can win the OS lock just before schema creation.
        for _ in range(100):
            try:
                db = sqlite3.connect(f"{(self.root / 'state.sqlite3').as_uri()}?mode=ro", uri=True, timeout=1)
                with contextlib.closing(db):
                    row = db.execute("SELECT epoch,instance,environment FROM owner WHERE id=1").fetchone()
                if row:
                    return {"epoch": row[0], "instance": json.loads(row[1]), "environment": json.loads(row[2])}
            except sqlite3.Error:
                pass
            time.sleep(.02)
        raise Rejected("OWNER_INITIALIZING")

    @contextlib.contextmanager
    def transaction(self, epoch=None):
        if not self.owner or self.db is None:
            raise Rejected("NOT_OWNER")
        st = self.root.stat()
        if (st.st_dev, st.st_ino) != self.inode:
            raise Rejected("OWNER_ATTEMPT_FENCE")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            current = self.db.execute("SELECT epoch,instance FROM owner WHERE id=1").fetchone()
            if current[0] != self.epoch or (epoch is not None and epoch != self.epoch) or json.loads(current[1]) != self.instance or not is_alive(self.instance):
                raise Rejected("OWNER_ATTEMPT_FENCE")
            yield self.db
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def intent(self, operation, attempt, payload, epoch):
        if not isinstance(operation, str) or not operation or not isinstance(attempt, str) or not attempt:
            raise Rejected("INVALID_IDENTITY")
        text = canonical(payload).decode()
        with self.transaction(epoch) as db:
            row = db.execute("SELECT * FROM operations WHERE id=?", (operation,)).fetchone()
            if row:
                if row["payload"] != text:
                    raise Rejected("OPERATION_PAYLOAD_CONFLICT")
                if row["attempt"] != attempt:
                    raise Rejected("OWNER_ATTEMPT_FENCE")
                return dict(row)
            db.execute("INSERT OR IGNORE INTO artifacts VALUES(?,?)", (hashlib.sha256(text.encode()).hexdigest(), text.encode()))
            db.execute("INSERT INTO operations VALUES(?,?,?,'QUEUED',1,1)", (operation, text, attempt))
            db.execute("INSERT INTO events(operation,kind,body) VALUES(?,'PENDING_INTENT',?)", (operation, text))
            return dict(db.execute("SELECT * FROM operations WHERE id=?", (operation,)).fetchone())

    def transition(self, operation, attempt, state, epoch, revision):
        if state not in STATES:
            raise Rejected("INVALID_STATE")
        with self.transaction(epoch) as db:
            row = db.execute("SELECT * FROM operations WHERE id=?", (operation,)).fetchone()
            if not row or row["attempt"] != attempt:
                raise Rejected("OWNER_ATTEMPT_FENCE")
            if row["revision"] != revision:
                raise Rejected("REVISION_CONFLICT")
            db.execute("INSERT INTO events(operation,kind,body) VALUES(?,'STATE_OBSERVATION',?)", (operation, canonical({"attempt": attempt, "state": state}).decode()))
            if row["state"] not in TERMINAL:
                db.execute("UPDATE operations SET state=?,revision=revision+1,pending=? WHERE id=?", (state, int(state not in TERMINAL), operation))
            return dict(db.execute("SELECT * FROM operations WHERE id=?", (operation,)).fetchone())

    def control(self, kind, identifier, body, revision):
        with self.transaction() as db:
            row = db.execute("SELECT revision FROM controls WHERE kind=? AND id=?", (kind, identifier)).fetchone()
            actual = row[0] if row else 0
            if revision != actual:
                raise Rejected("REVISION_CONFLICT")
            db.execute("INSERT OR REPLACE INTO controls VALUES(?,?,?,?)", (kind, identifier, canonical(body).decode(), actual + 1))
            db.execute("INSERT INTO events(operation,kind,body) VALUES(NULL,?,?)", (kind, canonical({"id": identifier, "actor": "controller", "body": body}).decode()))
            return {"revision": actual + 1}

    def status(self):
        result = self.identity() if self.owner else self.existing_owner()
        if self.owner:
            result["pending_intents"] = [dict(r) for r in self.db.execute("SELECT id,attempt,state,revision FROM operations WHERE pending=1")]
        result["recovery_subject"] = {"instance": result["instance"], "alive": is_alive(result["instance"]), "kind": "foreground_gateway", "scope": "pending-reference-only"}
        return result

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None
        if getattr(self, "lock_fd", None) is not None:
            os.close(self.lock_fd)
            self.lock_fd = None
        self.owner = False
