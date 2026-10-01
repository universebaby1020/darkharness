"""Linux COMPONENT/LOCAL_PROCESS: owner + framed gateway, never Band startup."""
import base64
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from darkharness.ipc import envelope, read_frame, write_frame
from test_integration_artifacts import OFFICIAL


@unittest.skipUnless(sys.platform == "linux", "Linux owner process test")
class GatewayTests(unittest.TestCase):
    def test_foreground_gateway_shared_store_and_evidence_pages(self):
        with tempfile.TemporaryDirectory() as td:
            p = subprocess.Popen([sys.executable, "-B", "-m", "darkharness.integration.gateway",
                                  "--state-root", str(Path(td) / "state"), "--official-root", str(OFFICIAL)],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                def call(action, payload=None, environment=None, revision=None):
                    req = envelope(action, str(time.monotonic_ns()), payload=payload or {}, environment_id=environment, expected_revision=revision)
                    write_frame(p.stdin, req)
                    return read_frame(p.stdout)
                hello = call("hello")
                self.assertEqual(hello["execution_status"], "SUCCEEDED")
                environment = hello["data"]["backend"]["environment"]["environment_id"]
                self.assertEqual(hello["data"]["backend"]["epoch"], 1)
                status = call("integration.status", environment=environment)
                self.assertEqual(status["data"]["agent_count"], 0)
                self.assertEqual(status["data"]["works"], [])
                grant = call("grant.record", {"id": "g", "source": "controller fixture", "end_condition": "STOP/revoke", "scope": {}}, environment, 0)
                self.assertEqual(grant["data"]["revision"], 1)
                page = call("integration.events", {"after": 0}, environment)
                self.assertEqual(page["data"]["events"], [])
                call("shutdown", environment=environment)
                p.wait(timeout=10)
                self.assertEqual(p.returncode, 0)
                self.assertNotIn(b"Traceback", p.stderr.read())
            finally:
                if p.poll() is None:
                    p.kill()
                    p.wait()
                p.stdin.close()
                p.stdout.close()
                p.stderr.close()

    def test_async_job_returns_before_model_and_shared_transaction(self):
        from darkharness.core import Store
        from darkharness.integration.gateway import IntegrationService, IntegrationSession
        from darkharness.integration.mailbox import Mailbox
        with tempfile.TemporaryDirectory() as td:
            owner = Store(td)
            service = IntegrationService(owner, 1024 * 1024, 16384, OFFICIAL, sys.executable)
            try:
                async def component():
                    with service.manager.owner.transaction(owner.epoch) as db:
                        Mailbox.event(db, "component-op", "COMPONENT_ONLY", {"large": "x" * 40000})
                    return {"component": True, "live": False}
                with service.mutex:
                    job = service.submit(component())
                    self.assertIn(service.job(job)["state"], {"RUNNING", "RETURNED"})
                for _ in range(100):
                    result = service.job(job)
                    if result["state"] == "RETURNED":
                        break
                    time.sleep(.01)
                self.assertEqual(result["result"], {"component": True, "live": False})
                session = IntegrationSession(service)
                session.hello = True
                req = envelope("integration.events", "page", payload={"after": 0}, environment_id=service.environment_id)
                with service.mutex:
                    page = session.handle(req)["data"]["events"]
                self.assertEqual(page[0]["operation"], "component-op")
                chunks, cursor = [], 0
                while cursor is not None:
                    req = envelope("integration.artifact.read", "part-" + str(cursor), payload={"artifact_id": page[0]["artifact_id"], "cursor": cursor}, environment_id=service.environment_id)
                    with service.mutex:
                        part = session.handle(req)["data"]
                    chunks.append(base64.b64decode(part["base64"]))
                    cursor = part["next_cursor"]
                self.assertEqual(json.loads(b"".join(chunks))["large"], "x" * 40000)
                self.assertIs(service.manager.owner.owner, owner)
                self.assertEqual(owner.epoch, 1)
            finally:
                service.close()
                owner.close()
