import base64
import concurrent.futures
import hashlib
import io
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from darkharness.host_bridge import Bridge, launcher_argv
from darkharness.ipc import envelope, frame, read_frame, write_frame
from tools.public_guard import findings


class WireTests(unittest.TestCase):
    def test_corrupt_and_truncated_resync(self):
        stream = io.BytesIO(b"log noise\n" + b"DH1:broken" + frame({"ok": "한글"}))
        self.assertEqual(read_frame(stream)["_frame_error"], "MALFORMED_FRAME")
        self.assertEqual(read_frame(stream), {"ok": "한글"})
        self.assertIsNone(read_frame(stream))

    def test_limits(self):
        self.assertEqual(read_frame(io.BytesIO(b"x" * 1000 + b"\n"), 64)["_frame_error"], "FRAME_TOO_LARGE")
        with self.assertRaises(ValueError):
            frame({"x": "a" * 100}, 10)

    def test_launcher(self):
        cwd = "/tmp/한글 공백 ' $(echo no)"
        argv = launcher_argv("Observed-Distro", "observed", cwd, "/usr/bin/python3", "/tmp/state")
        self.assertEqual(argv[argv.index("--cd") + 1], cwd)
        self.assertEqual(argv[argv.index("--exec") + 1], "/usr/bin/python3")
        self.assertNotIn("sh", argv)
        with self.assertRaisesRegex(ValueError, "DOUBLE_QUOTE_REQUIRES_FRAMED_PAYLOAD"):
            launcher_argv("Observed-Distro", "observed", cwd + '"', "/usr/bin/python3", "/tmp/state")
        with self.assertRaises(ValueError):
            launcher_argv("", "observed", "/tmp", "/usr/bin/python3", "/tmp/state")

    def test_public_guard_patterns(self):
        fake = ["sk" + "-" + "a" * 20, "AK" + "IA" + "A" * 16,
                "gh" + "p_" + "b" * 24, "bearer " + "x" * 22 + "1",
                "https://" + "user:" + "fake@host.invalid", "BAND_" + "KEY=" + "x" * 20]
        for value in fake:
            self.assertTrue(findings("fixture.json", value.encode()))
        self.assertTrue(findings("auth.json", b""))
        self.assertTrue(findings("file.py", ("/" + "home/" + "private/" + "file").encode()))
        self.assertTrue(findings("file.py", ("person" + "@" + "mail.example").encode()))
        self.assertFalse(findings("file.py", b"actor@worker.invalid"))

    def test_stderr_flood_process(self):
        # Actual child emits 3 MiB concurrently with framed stdout. No runtime/provider fixture.
        code = "import sys; from darkharness.ipc import read_frame,write_frame; sys.stderr.buffer.write(b'd'*3145728); sys.stderr.flush(); r=read_frame(sys.stdin.buffer); write_frame(sys.stdout.buffer,dict(request_id=r['request_id'],execution_status='SUCCEEDED')); read_frame(sys.stdin.buffer)"
        bridge = Bridge([sys.executable, "-B", "-c", code])
        try:
            self.assertEqual(bridge.request("core.status", timeout=10)["execution_status"], "SUCCEEDED")
            bridge.process.stdin.close()
            bridge.process.wait(timeout=5)
            bridge.err_reader.join(timeout=2)
            self.assertEqual(bridge.diagnostic_bytes, 3145728)
            self.assertLessEqual(bridge.diagnostic_retained_bytes, 262144)
        finally:
            if bridge.process.poll() is None:
                bridge.process.kill()
                bridge.process.wait()
            bridge.process.stdout.close()
            bridge.process.stderr.close()


@unittest.skipUnless(sys.platform == "linux", "requires real Linux flock/proc and Linux FS state")
class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dh-w1-")
        self.root = Path(self.temp.name) / "state"
        self.bridges = []
        self.b = self.launch()
        self.hello = self.b.request("hello", timeout=10)["data"]["backend"]
        self.epoch = self.hello["epoch"]

    def launch(self):
        b = Bridge([sys.executable, "-B", "-m", "darkharness.gateway", "--state-root", str(self.root), "--frame-bytes", str(8 * 1024 * 1024)], frame_bytes=8 * 1024 * 1024)
        self.bridges.append(b)
        return b

    def tearDown(self):
        for b in reversed(self.bridges):
            b.close()
        self.temp.cleanup()

    def ok(self, action, payload=None, operation="op", revision=None, bridge=None, request_id=None):
        response = (bridge or self.b).request(action, payload, operation, revision, request_id, timeout=10)
        self.assertEqual(response["execution_status"], "SUCCEEDED", response)
        return response["data"]

    def intent(self, attempt="a", payload=None):
        return self.ok("operation.intent", {"attempt": attempt, "intent": payload or {"goal": "test"}, "epoch": self.epoch})

    def test_T42_empty_bootstrap(self):
        self.assertTrue((self.root / "state.sqlite3").is_file())
        self.assertTrue((self.root / "owner.lock").is_file())
        status = self.ok("core.status")
        self.assertEqual(status["epoch"], 1)
        self.assertEqual(status["pending_intents"], [])
        self.assertTrue(status["recovery_subject"]["alive"])
        self.assertNotIn(status["environment"]["state_filesystem"], {"9p", "drvfs"})

    def test_state_root_required(self):
        r = subprocess.run([sys.executable, "-B", "-m", "darkharness.gateway"], capture_output=True)
        self.assertEqual(r.returncode, 2)
        self.assertEqual(r.stdout, b"")

    def test_T43_simultaneous_N8(self):
        others = [self.launch() for _ in range(8)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            identities = list(pool.map(lambda b: b.request("hello", timeout=10)["data"]["backend"], others))
        self.assertTrue(all(x["instance"] == self.hello["instance"] and x["epoch"] == self.epoch for x in identities))
        self.assertEqual(self.b.process.pid, self.hello["instance"]["pid"])
        self.assertEqual(len(self.ok("core.status")["pending_intents"]), 0)

    def test_T43_empty_concurrent_election(self):
        self.b.close()
        self.root = Path(self.temp.name) / "empty-race"
        import threading
        barrier = threading.Barrier(8)
        def start(_):
            barrier.wait(timeout=10)
            bridge = self.launch()
            return bridge, bridge.request("hello", timeout=10)["data"]["backend"]
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(start, range(8)))
        identities = [h["instance"] for _, h in results]
        self.assertTrue(all(i == identities[0] for i in identities))
        self.assertTrue(all(h["epoch"] == 1 for _, h in results))
        self.assertEqual(sum(b.process.pid == identities[0]["pid"] for b, _ in results), 1)

    def test_atomic_crash_before_commit(self):
        self.b.close()
        code = '''import sys,time
from darkharness.core import Store
s=Store(sys.argv[1])
with s.transaction():
 s.db.execute("INSERT INTO operations VALUES('crash','{}','a','QUEUED',1,1)")
 s.db.execute("INSERT INTO events(operation,kind,body) VALUES('crash','PENDING_INTENT','{}')")
 print('UNCOMMITTED',flush=True)
 time.sleep(60)
'''
        p = subprocess.Popen([sys.executable, "-B", "-c", code, str(self.root)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            self.assertEqual(p.stdout.readline(), b"UNCOMMITTED\n")
            p.kill()
            p.wait(timeout=5)
        finally:
            if p.poll() is None:
                p.kill()
                p.wait()
            p.stdout.close()
            p.stderr.close()
        new = self.launch()
        status = new.request("hello", timeout=10)["data"]["backend"]
        self.assertEqual(status["pending_intents"], [])
        with sqlite3.connect(self.root / "state.sqlite3") as db:
            self.assertEqual(db.execute("SELECT count(*) FROM events WHERE operation='crash'").fetchone()[0], 0)

    def test_T43_kill_and_stale_callback(self):
        self.intent()
        self.b.process.kill()  # Only a disposable test owner process.
        self.b.process.wait(timeout=5)
        replacement = self.launch()
        hello = replacement.request("hello", timeout=10)["data"]["backend"]
        self.assertEqual(hello["epoch"], self.epoch + 1)
        self.assertEqual(hello["pending_intents"][0]["id"], "op")
        bad = replacement.request("operation.transition", {"epoch": self.epoch, "attempt": "a", "state": "SUCCEEDED"}, "op", 1, timeout=10)
        self.assertEqual(bad["public_reason_code"], "OWNER_ATTEMPT_FENCE")
        bad = replacement.request("operation.transition", {"epoch": hello["epoch"], "attempt": "old", "state": "SUCCEEDED"}, "op", 1, timeout=10)
        self.assertEqual(bad["public_reason_code"], "OWNER_ATTEMPT_FENCE")
        self.assertEqual(self.ok("operation.status", bridge=replacement)["state"], "QUEUED")

    def test_T43_unlinked_file_no_transfer(self):
        (self.root / "owner.lock").unlink()
        challenger = self.launch()
        hello = challenger.request("hello", timeout=10)["data"]["backend"]
        self.assertEqual(hello["instance"], self.hello["instance"])
        self.assertEqual(self.intent()["state"], "QUEUED")

    def test_T43_epoch_tamper_no_transfer(self):
        with sqlite3.connect(self.root / "state.sqlite3") as db:
            db.execute("UPDATE owner SET epoch=epoch+100")
        challenger = self.launch()
        hello = challenger.request("hello", timeout=10)["data"]["backend"]
        self.assertEqual(hello["instance"], self.hello["instance"])
        denied = self.b.request("operation.intent", {"epoch": self.epoch, "attempt": "a", "intent": {}}, "op", timeout=10)
        self.assertEqual(denied["public_reason_code"], "OWNER_ATTEMPT_FENCE")

    def test_stale_lock_file_without_holder(self):
        self.b.close()
        (self.root / "owner.lock").write_text("stale PID", encoding="utf-8")
        new = self.launch()
        hello = new.request("hello", timeout=10)["data"]["backend"]
        self.assertEqual(hello["epoch"], 2)
        self.assertNotEqual(hello["instance"], self.hello["instance"])

    def test_atomic_intent_idempotent_conflict(self):
        first = self.intent()
        self.assertEqual(self.intent(), first)
        conflict = self.b.request("operation.intent", {"epoch": self.epoch, "attempt": "a", "intent": {"goal": "other"}}, "op", timeout=10)
        self.assertEqual(conflict["public_reason_code"], "OPERATION_PAYLOAD_CONFLICT")
        with sqlite3.connect(self.root / "state.sqlite3") as db:
            self.assertEqual(db.execute("SELECT count(*) FROM events WHERE kind='PENDING_INTENT'").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT pending FROM operations WHERE id='op'").fetchone()[0], 1)
        self.b.close()
        new = self.launch()
        self.assertEqual(new.request("hello", timeout=10)["data"]["backend"]["pending_intents"][0]["id"], "op")

    def test_terminal_monotonic_and_revision(self):
        self.intent()
        final = self.ok("operation.transition", {"epoch": self.epoch, "attempt": "a", "state": "SUCCEEDED"}, revision=1)
        late = self.ok("operation.transition", {"epoch": self.epoch, "attempt": "a", "state": "RUNNING"}, revision=final["revision"])
        self.assertEqual(late["state"], "SUCCEEDED")
        race = self.ok("operation.transition", {"epoch": self.epoch, "attempt": "a", "state": "CANCELLED"}, revision=final["revision"])
        self.assertEqual(race["state"], "SUCCEEDED")
        bad = self.b.request("operation.transition", {"epoch": self.epoch, "attempt": "a", "state": "RUNNING"}, "op", 1, timeout=10)
        self.assertEqual(bad["public_reason_code"], "REVISION_CONFLICT")

    def test_framing_duplicate_reverse_corrupt(self):
        first = self.ok("core.status", request_id="z")
        self.assertEqual(self.ok("core.status", request_id="z"), first)
        self.ok("core.status", request_id="a")
        conflict = self.b.request("environment.inspect", request_id="z", timeout=10)
        self.assertEqual(conflict["public_reason_code"], "REQUEST_ID_CONFLICT")
        self.b.process.stdin.write(b"stderr-looking garbage\n")
        self.b.process.stdin.flush()
        self.assertEqual(self.b.responses.get(timeout=5)["public_reason_code"], "MALFORMED_FRAME")
        self.b.process.stdin.write(b"DH1:truncated" + frame(envelope("core.status", "recover", environment_id=self.b.environment_id)))
        self.b.process.stdin.flush()
        self.assertEqual(self.b.responses.get(timeout=5)["request_id"], "recover")
        self.ok("core.status")

    def test_multi_megabyte_artifact_pages(self):
        self.intent()
        raw = ("한글 artifact\n".encode() * 250000)
        ref = self.ok("artifact.record", {"epoch": self.epoch, "attempt": "a", "base64": base64.b64encode(raw).decode()})
        result = bytearray()
        cursor = 0
        while cursor is not None:
            page = self.ok("artifact.read", {"artifact_id": ref["artifact_id"], "cursor": cursor})
            result += base64.b64decode(page["base64"])
            cursor = page["next_cursor"]
        self.assertGreater(len(result), 3 * 1024 * 1024)
        self.assertEqual(hashlib.sha256(result).hexdigest(), ref["artifact_id"])
        self.assertEqual(result, raw)
        bad = self.b.request("artifact.read", {"artifact_id": ref["artifact_id"], "cursor": -1}, timeout=10)
        self.assertEqual(bad["public_reason_code"], "INVALID_CURSOR")

    def test_T44_real_seat_channel(self):
        self.intent()
        control = self.ok("grant.record", {"id": "g", "source": "test controller decision", "scope": {"read": ["op"]}, "end_condition": "test end"}, revision=0)
        self.assertEqual(control["revision"], 1)
        binding = self.ok("seat.bind", {"seat_id": "s", "operations": ["op"], "artifacts": []})
        with socket.socket(socket.AF_UNIX) as client:
            client.connect(binding["endpoint"])
            with client.makefile("rwb", buffering=0) as stream:
                write_frame(stream, {"credential": binding["credential"]})
                def call(action, payload=None, operation="op"):
                    write_frame(stream, envelope(action, action, operation, self.b.environment_id, payload))
                    return read_frame(stream)
                self.assertEqual(call("hello")["execution_status"], "SUCCEEDED")
                self.assertEqual(call("operation.status")["execution_status"], "SUCCEEDED")
                for action in ["grant.record", "grant.create", "grant.extend", "settings.save", "shutdown", "seat.bind", "operation.transition"]:
                    self.assertEqual(call(action, {"role": "controller", "trusted": True, "approved": True, "grant_id": "g"})["public_reason_code"], "SEAT_ACTION_DENIED")
                self.assertEqual(call("operation.status", operation="outside")["public_reason_code"], "REQUEST_ID_CONFLICT")
                write_frame(stream, envelope("operation.status", "outside", "outside", self.b.environment_id))
                self.assertEqual(read_frame(stream)["public_reason_code"], "SEAT_BINDING_DENIED")
                self.ok("seat.revoke", {"seat_id": "s"})
                # Exact cached replays must revalidate authority, even hello.
                for action in ["operation.status", "hello"]:
                    self.assertEqual(call(action)["public_reason_code"], "BINDING_REVOKED")
                write_frame(stream, envelope("core.status", "revoked", environment_id=self.b.environment_id))
                self.assertEqual(read_frame(stream)["public_reason_code"], "BINDING_REVOKED")
        with socket.socket(socket.AF_UNIX) as client:
            client.connect(binding["endpoint"])
            with client.makefile("rwb", buffering=0) as stream:
                write_frame(stream, {"credential": "wrong", "role": "controller"})
                self.assertEqual(read_frame(stream)["public_reason_code"], "SEAT_AUTH_FAILED")

    def test_probe_and_fifo_safety(self):
        sentinel = Path(self.temp.name) / '한글 space \' " $(echo no)'
        sentinel.write_bytes(b"sentinel")
        data = self.ok("path.probe", {"sentinel": str(sentinel)})
        self.assertEqual(data["sha256"], hashlib.sha256(b"sentinel").hexdigest())
        fifo = Path(self.temp.name) / "fifo"
        os.mkfifo(fifo)
        denied = self.b.request("path.probe", {"sentinel": str(fifo)}, timeout=5)
        self.assertEqual(denied["public_reason_code"], "PROBE_NOT_SMALL_REGULAR_FILE")
        self.ok("core.status")

    def test_run_supervisor_binding(self):
        self.ok("run.bind", {"id": "run1"}, revision=0)
        status = self.ok("run.status", {"id": "run1"})
        self.assertTrue(status["recovery_alive"])
        self.assertTrue(status["current_binding"])
        self.b.process.kill()
        self.b.process.wait(timeout=5)
        new = self.launch()
        new.request("hello", timeout=10)
        old = self.ok("run.status", {"id": "run1"}, bridge=new)
        self.assertFalse(old["current_binding"])
        self.assertFalse(old["recovery_alive"])
        self.ok("run.bind", {"id": "run1"}, revision=old["revision"], bridge=new)
        self.assertTrue(self.ok("run.status", {"id": "run1"}, bridge=new)["current_binding"])


if __name__ == "__main__":
    unittest.main()
