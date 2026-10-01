"""COMPONENT: durable receipts/owner seam; no provider or Band calls."""
import asyncio
from contextlib import contextmanager
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from darkharness.integration.mailbox import Mailbox, HandoffPart, IntegrationError, digest
from darkharness.integration.contract import PermissionRequest
from darkharness.integration.policy import ApprovalRouter


class Owner:
    def __init__(self, path=":memory:"):
        self.epoch = 1
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("CREATE TABLE IF NOT EXISTS controls(kind TEXT,id TEXT,body TEXT,revision INTEGER,PRIMARY KEY(kind,id))")

    @contextmanager
    def transaction(self, epoch=None):
        if epoch != self.epoch and epoch is not None:
            raise IntegrationError("OWNER_ATTEMPT_FENCE")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield self.db
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def grant(self, root, **scope):
        body = {"source": "authenticated controller fixture", "end_condition": "run completed or STOP/revoke", "scope": {"workspace": str(root), "run_id": "run", "seats": ["s"], "rooms": ["r"], **scope}}
        self.db.execute("INSERT OR REPLACE INTO controls VALUES('grant','g',?,1)", (json.dumps(body),))


class MailboxTests(unittest.TestCase):
    def setUp(self):
        self.owner = Owner()
        self.box = Mailbox(self.owner)

    def tearDown(self):
        self.owner.db.close()

    def receive(self, mid="m", body="task", **kwargs):
        return self.box.receive("s", "r", "peer", mid, body, **kwargs)

    def test_receipt_idempotence_not_content_dedup(self):
        a = self.receive()
        self.assertEqual(a, self.receive())
        b = self.receive("m2")
        self.assertNotEqual(a["work"], b["work"])
        self.assertEqual(a["hash"], b["hash"])

    def test_conflicting_platform_id(self):
        self.receive()
        with self.assertRaisesRegex(IntegrationError, "MESSAGE_ID_CONFLICT"):
            self.receive(body="changed")

    def test_multipart_missing_then_complete_out_of_order(self):
        whole = digest(b"hello world")
        p1 = HandoffPart("h", 1, 2, 5, digest(b"world"), whole)
        p0 = HandoffPart("h", 0, 2, 6, digest(b"hello "), whole)
        first = self.receive("p1", "world", part=p1)
        self.assertEqual(first["life"], "RECEIVED")
        self.assertIsNone(self.box.next_ready("s"))
        last = self.receive("p0", "hello ", part=p0)
        work = self.box.next_ready("s")
        self.assertEqual(json.loads(work["input"])["content"], "hello world")
        self.assertEqual(last["work"], work["id"])
        self.assertEqual(self.receive("p1", "world", part=p1)["life"], "READY")

    def test_duplicate_part_new_platform_id_is_one_handoff_only(self):
        normal = self.receive("ordinary", "same")
        p = HandoffPart("h", 0, 1, 4, digest(b"same"), digest(b"same"))
        a = self.receive("p0", "same", part=p)
        b = self.receive("p0-repeat", "same", part=p)
        self.assertEqual(a["work"], b["work"])
        self.assertNotEqual(normal["work"], a["work"])
        self.assertEqual(self.owner.db.execute("SELECT COUNT(*) FROM c_work").fetchone()[0], 2)

    def test_part_conflict(self):
        p = HandoffPart("h", 0, 2, 4, digest(b"same"), digest(b"sameother"))
        self.receive("p", "same", part=p)
        with self.assertRaisesRegex(IntegrationError, "PART_CONFLICT"):
            self.receive("p2", "else", part=replace(p, sha256=digest(b"else")))

    def test_same_id_metadata_conflict(self):
        p = HandoffPart("h", 0, 2, 4, digest(b"same"), digest(b"sameother"))
        self.receive("p", "same", part=p)
        with self.assertRaisesRegex(IntegrationError, "HANDOFF_CONFLICT"):
            self.receive("p", "same", part=replace(p, count=3))

    def test_bad_part_integrity(self):
        p = HandoffPart("h", 0, 1, 3, digest(b"a"), digest(b"a"))
        with self.assertRaisesRegex(IntegrationError, "PART_INTEGRITY"):
            self.receive("p", "a", part=p)
        self.assertIsNone(self.box.next_ready("s"))

    def test_bad_whole_hash(self):
        p = HandoffPart("h", 0, 1, 1, digest(b"a"), digest(b"b"))
        with self.assertRaisesRegex(IntegrationError, "HANDOFF_INTEGRITY"):
            self.receive("p", "a", part=p)
        self.assertIsNone(self.box.next_ready("s"))

    def test_terminal_monotonic_and_stale_attempt(self):
        op = self.receive()["work"]
        self.box.claim(op, "a")
        self.box.update(op, "a", state="SUCCEEDED", delivery="RETURNED")
        self.box.update(op, "a", state="RUNNING")
        self.box.update(op, "a", state="CANCELLED")
        self.assertEqual(self.box.read_work(op)["state"], "SUCCEEDED")
        with self.assertRaisesRegex(IntegrationError, "OWNER_ATTEMPT_FENCE"):
            self.box.update(op, "old", state="FAILED")

    def test_serial_work_control_lane(self):
        op = self.receive()["work"]
        self.receive("m2")
        self.box.claim(op, "a")
        self.assertIsNone(self.box.next_ready("s"))
        self.box.control("cancel", op, "a", "cancel", {})
        self.assertEqual(self.box.drain_controls(op, "a")[0]["kind"], "cancel")
        self.box.update(op, "a", state="CANCELLED", delivery="RETURNED")
        self.assertIsNotNone(self.box.next_ready("s"))
        with self.assertRaisesRegex(IntegrationError, "STALE_CONTROL"):
            self.box.control("late", op, "a", "cancel", {})

    def test_unknown_fence_restart_no_replay(self):
        op = self.receive()["work"]
        self.box.claim(op, "a")
        self.assertEqual(self.box.recover(), [op])
        self.assertIsNone(self.box.next_ready("s"))
        self.assertEqual(self.box.read_work(op)["delivery"], "DELIVERY_UNKNOWN")
        with self.assertRaisesRegex(IntegrationError, "DUPLICATE_EXECUTION_FENCE"):
            self.box.claim(op, "new")

    def test_outbox_unknown_ack_lost_and_conflict(self):
        self.assertIsNone(self.box.prepare_send("send", "op", {"content": "hello"}))
        with self.assertRaisesRegex(IntegrationError, "DELIVERY_UNKNOWN_FENCE"):
            self.box.prepare_send("send", "op", {"content": "hello"})
        with self.assertRaisesRegex(IntegrationError, "OUTBOX_ID_CONFLICT"):
            self.box.prepare_send("send", "op", {"content": "changed"})
        self.box.sent("send", {"message_id": "platform"})
        self.assertEqual(self.box.prepare_send("send", "op", {"content": "hello"}), {"message_id": "platform"})

    def test_peer_answer_continuation_no_synchronous_wait(self):
        op = self.receive()["work"]
        self.box.claim(op, "a")
        self.box.question("q", op, "coordinator", {"question": "which?", "task": "full"})
        self.box.update(op, "a", state="PAUSED", delivery="YIELDED", thread="session")
        with self.assertRaisesRegex(IntegrationError, "PEER_BINDING_DENIED"):
            self.box.answer("q", "pretend coordinator", "answer")
        cid = self.box.answer("q", "coordinator", "answer")
        self.assertEqual(self.box.answer("q", "coordinator", "answer"), cid)
        self.assertEqual(self.box.read_work(cid)["thread"], "session")
        self.assertEqual(self.box.next_ready("s")["id"], cid)
        with self.assertRaisesRegex(IntegrationError, "ANSWER_CONFLICT"):
            self.box.answer("q", "coordinator", "changed")

    def test_disk_restart_preserves_full_input_and_artifact(self):
        with tempfile.TemporaryDirectory() as td:
            path = str(Path(td) / "test.sqlite")
            owner = Owner(path)
            box = Mailbox(owner)
            row = box.receive("s", "r", "peer", "id", "전체 과제\ncomplete")
            owner.db.close()
            owner = Owner(path)
            box = Mailbox(owner)
            self.assertEqual(box.read_work(row["work"])["input"], '{"content":"전체 과제\\ncomplete"}')
            self.assertEqual(owner.db.execute("SELECT body FROM c_artifact WHERE hash=?", (row["hash"],)).fetchone()[0], "전체 과제\ncomplete".encode())
            owner.db.close()


class RouterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.owner = Owner()
        Mailbox(self.owner)
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name).resolve()
        self.router = ApprovalRouter(self.owner, "g", "s", "r", "run", self.root)
        self.req = PermissionRequest("op", "a", "cb", "command", str(self.root), ("git", "status"))

    async def asyncTearDown(self):
        self.owner.db.close()
        self.td.cleanup()

    async def test_missing_grant_and_payload_role_do_not_authorize(self):
        self.assertFalse(await self.router.decide(self.req))
        self.owner.grant(self.root, trusted=True, role="controller", approved_commands=[])
        self.assertFalse(await self.router.decide(self.req))

    async def test_run_end_grant_without_expiry(self):
        self.owner.grant(self.root, approved_commands=[["git", "status"]])
        self.assertTrue(await self.router.decide(self.req))
        self.assertFalse(await self.router.decide(replace(self.req, argv=("git", "push"))))

    async def test_expired_malformed_and_revoked_grants(self):
        for expiry in ("2000-01-01T00:00:00Z", "bad", "2030-01-01"):
            self.owner.grant(self.root, expires_at=expiry, approved_commands=[["git", "status"]])
            self.assertFalse(await self.router.decide(self.req))
        self.owner.db.execute("UPDATE controls SET body=?", (json.dumps({"revoked": True}),))
        self.assertFalse(await self.router.decide(self.req))

    async def test_stop_run_cannot_authorize(self):
        self.owner.grant(self.root, approved_commands=[["git", "status"]])
        self.owner.db.execute("INSERT INTO controls VALUES('run','run',?,1)", (json.dumps({"state": "STOPPED"}),))
        self.assertFalse(await self.router.decide(self.req))

    async def test_shell_interpreter_and_privilege_denied_even_exact(self):
        for argv in [("bash", "-c", "echo task"), ("python3", "arbitrary.py"), ("sudo", "git", "status")]:
            self.owner.grant(self.root, approved_commands=[list(argv)])
            self.assertFalse(await self.router.decide(replace(self.req, argv=argv)))
        self.owner.grant(self.root, approved_commands=[["git", "status"]])
        self.assertFalse(await self.router.decide(replace(self.req, privilege=True)))

    async def test_scoped_paths_symlink_escape_and_cwd(self):
        self.owner.grant(self.root, file_write=True)
        request = replace(self.req, action="file_write", argv=(), paths=(str(self.root / "new.txt"),))
        self.assertTrue(await self.router.decide(request))
        self.assertFalse(await self.router.decide(replace(request, paths=(str(self.root / "../outside"),))))
        self.assertFalse(await self.router.decide(replace(request, paths=())))
        self.assertFalse(await self.router.decide(replace(request, workspace="/")))
