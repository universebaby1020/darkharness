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


def local_rejection_trace(box, operation, attempt):
    """Synthetic canonical SDK4 trace, anonymous values; never a live send."""
    from darkharness.integration.mailbox import Mailbox, digest, encode
    body = {'content': 'original reply', 'mentions': ['@example-account/dh-builder']}
    params = {'tool': 'band_send_message', 'callId': 'tool-call', 'arguments': body, 'threadId': 'session', 'turnId': 'turn'}
    request = {'id': 1, 'method': 'item/tool/call', 'params': params}
    failed = {'success': False, 'contentItems': [{'type': 'inputText', 'text': "Error: Unknown participant 'example-account/dh-builder'. Available handles: []"}]}
    def rpc(kind, payload):
        box.observe(operation, attempt, kind, {'client_id': 'client', 'payload': payload})
    rpc('STDOUT_RPC', request)
    box.callback(operation, attempt, '1', {'method': request['method'], 'params': params})
    box.prepare_send('old-send', operation, body)
    box.observe(operation, attempt, 'DELIVERY_UNKNOWN', {'outbox_id': 'old-send'})
    rpc('STDIN_RPC', {'id': 1, 'result': failed})
    rpc('STDOUT_RPC', {'method': 'item/completed', 'params': {'threadId': 'session', 'turnId': 'turn', 'item': {'type': 'dynamicToolCall', 'id': 'tool-call', 'tool': 'band_send_message', 'arguments': body, 'status': 'failed', **failed}}})
    names = ['request', 'callback', 'intent', 'unknown', 'result', 'completed']
    with box.owner.transaction(box.owner.epoch) as db:
        rows = db.execute('SELECT * FROM c_event WHERE operation=? ORDER BY seq DESC LIMIT 6', (operation,)).fetchall()[::-1]
        refs = {name: {'seq': row['seq'], 'artifact_id': Mailbox.artifact(db, row['body'].encode())} for name, row in zip(names, rows)}
    return {'attempt': attempt, 'outbox_id': 'old-send', 'body_sha256': digest(encode(body).encode()), 'evidence_refs': refs}


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

    def test_controller_continuation_proof_api_and_seat_denial(self):
        from darkharness.core import Store
        from darkharness.integration.gateway import IntegrationService, IntegrationSession
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / 'result'
            subprocess.run(['git', 'init', '-q', str(repo)], check=True)
            (repo / 'base').write_text('seat source')
            subprocess.run(['git', '-C', str(repo), 'add', 'base'], check=True)
            subprocess.run(['git', '-C', str(repo), '-c', 'user.name=f', '-c', 'user.email=f@actors.invalid', 'commit', '-qm', 'base'], check=True)
            store = Store(str(Path(td) / 'state'))
            service = IntegrationService(store, 1024 * 1024, 16384, OFFICIAL, sys.executable)
            try:
                box = service.manager.mailbox
                op = box.receive('s', 'r', 'peer', 'original', 'full original task')['work']
                box.claim(op, 'a')
                box.update(op, 'a', state='FAILED', delivery='RETURNED', thread='legacy-session')
                session = IntegrationSession(service)
                session.hello = True
                def call(action, payload, revision=None):
                    req = envelope(action, str(time.monotonic_ns()), payload=payload, environment_id=service.environment_id, expected_revision=revision)
                    req['operation_id'] = op
                    with service.mutex:
                        return session.handle(req)
                grant = call('grant.record', {'id': 'g', 'source': 'controller fixture', 'end_condition': 'user STOP/revoke or run completion', 'scope': {'run_id': 'run', 'workspace': str(repo), 'seats': ['s'], 'rooms': ['r'], 'continuation': {'operations': [op]}}}, 0)
                self.assertEqual(grant['execution_status'], 'SUCCEEDED')
                denied = call('integration.continue', {'attempt': 'a', 'grant_id': 'g', 'safe': True})
                self.assertEqual(denied['public_reason_code'], 'INVALID_RECOVERY_PAYLOAD')
                observed = call('integration.recovery.observe', {'attempt': 'a', 'grant_id': 'g'})
                self.assertEqual(observed['execution_status'], 'SUCCEEDED')
                resumed = call('integration.continue', {'attempt': 'a', 'grant_id': 'g', 'evidence_id': observed['data']['evidence_id']})
                self.assertEqual(resumed['execution_status'], 'SUCCEEDED')
                child = box.read_work(resumed['data']['id'])
                self.assertEqual(child['thread'], 'legacy-session')
                self.assertEqual(json.loads(child['input'])['content'], 'full original task')
                self.assertEqual(store.db.execute('SELECT COUNT(*) FROM c_inbox').fetchone()[0], 1)
                seat = IntegrationSession(service, binding={'credential': 'seat-fixture'})
                seat.hello = True
                req = envelope('integration.continue', 'seat', payload={'attempt': 'a', 'grant_id': 'g', 'evidence_id': observed['data']['evidence_id']}, environment_id=service.environment_id)
                from darkharness.core import Rejected
                with self.assertRaisesRegex(Rejected, 'SEAT_ACTION_DENIED'):
                    seat.dispatch(req)
                self.assertIs(service.manager.owner.mutex, service.mutex)
                self.assertIs(service.manager.owner.owner, store)
            finally:
                service.close()
                store.close()

    def test_controller_local_rejection_proof_binding_and_terminal_no_resend(self):
        from copy import deepcopy
        from darkharness.core import Store, Rejected
        from darkharness.integration.gateway import IntegrationService, IntegrationSession
        from darkharness.integration.mailbox import IntegrationError
        with tempfile.TemporaryDirectory() as td:
            store = Store(str(Path(td) / 'state'))
            service = IntegrationService(store, 1024 * 1024, 16384, OFFICIAL, sys.executable)
            try:
                box = service.manager.mailbox
                op = box.receive('s', 'r', 'peer', 'original', 'original full task')['work']
                box.claim(op, 'a')
                box.update(op, 'a', state='SUCCEEDED', delivery='RETURNED', thread='session')
                args = local_rejection_trace(box, op, 'a')
                other = box.receive('s', 'r', 'peer', 'queued', 'original coordinator queue')['work']
                box.prepare_send('other-unknown', op, {'content': 'unproved network effect'})
                session = IntegrationSession(service)
                session.hello = True
                def call(payload, operation=op, action='integration.outbox.reject_local', revision=None):
                    req = envelope(action, str(time.monotonic_ns()), payload=payload, environment_id=service.environment_id, expected_revision=revision)
                    req['operation_id'] = operation
                    with service.mutex:
                        return session.handle(req)
                grant = {'id': 'g', 'source': 'controller fixture', 'end_condition': 'STOP/revoke or completion', 'scope': {'run_id': 'run', 'workspace': td, 'seats': ['s'], 'rooms': ['r'], 'continuation': {'operations': [op, other]}}}
                self.assertEqual(call(grant, action='grant.record', revision=0)['execution_status'], 'SUCCEEDED')
                payload = {'grant_id': 'g', **args}
                forged = deepcopy(payload)
                forged['safe'] = True
                self.assertEqual(call(forged)['public_reason_code'], 'INVALID_RECOVERY_PAYLOAD')
                for path, value in [(('attempt',), 'other-attempt'), (('body_sha256',), '0' * 64), (('outbox_id',), 'other-unknown'), (('evidence_refs', 'result', 'artifact_id'), '0' * 64), (('evidence_refs', 'completed', 'seq'), args['evidence_refs']['request']['seq'])]:
                    forged = deepcopy(payload)
                    target = forged
                    for key in path[:-1]:
                        target = target[key]
                    target[path[-1]] = value
                    self.assertEqual(call(forged)['execution_status'], 'FAILED')
                # Canonical-looking but forged traces still fail closed, even
                # when their artifact hash is internally consistent.
                from darkharness.integration.mailbox import Mailbox, encode
                for name, mutate in [
                    ('result', lambda p: p['result'].update(success=True)),
                    ('completed', lambda p: p['params']['item'].update(id='different-call')),
                    ('completed', lambda p: p['params'].update(turnId='different-turn')),
                    ('request', lambda p: p['params']['arguments'].update(content='different-body')),
                    ('result', lambda p: p['result']['contentItems'][0].update(text='Error: network failure')),
                ]:
                    forged = deepcopy(payload)
                    ref = forged['evidence_refs'][name]
                    old = store.db.execute('SELECT body FROM c_event WHERE seq=?', (ref['seq'],)).fetchone()[0]
                    changed = json.loads(old)
                    mutate(changed['data']['payload'])
                    with service.manager.owner.transaction(store.epoch) as db:
                        raw = encode(changed).encode()
                        ref['artifact_id'] = Mailbox.artifact(db, raw)
                        db.execute('UPDATE c_event SET body=? WHERE seq=?', (raw.decode(), ref['seq']))
                    try:
                        self.assertEqual(call(forged)['public_reason_code'], 'LOCAL_REJECTION_EVIDENCE_MISMATCH')
                    finally:
                        with service.manager.owner.transaction(store.epoch) as db:
                            db.execute('UPDATE c_event SET body=? WHERE seq=?', (old, ref['seq']))
                self.assertEqual(call(payload, operation=other)['public_reason_code'], 'OWNER_ATTEMPT_FENCE')
                self.assertEqual(store.db.execute("SELECT state FROM c_outbox WHERE id='old-send'").fetchone()[0], 'DELIVERY_UNKNOWN')
                seat = IntegrationSession(service, binding={'credential': 'seat-fixture'})
                seat.hello = True
                req = envelope('integration.outbox.reject_local', 'seat', payload=payload, environment_id=service.environment_id)
                with self.assertRaisesRegex(Rejected, 'SEAT_ACTION_DENIED'):
                    seat.dispatch(req)
                result = call(payload)
                self.assertEqual(result['execution_status'], 'SUCCEEDED')
                self.assertEqual(result['data']['effect'], 'NOT_SENT')
                self.assertEqual(call(payload)['execution_status'], 'SUCCEEDED')
                self.assertEqual(store.db.execute("SELECT COUNT(*) FROM c_event WHERE kind='SEND_REJECTION_RECONCILED'").fetchone()[0], 1)
                self.assertEqual(store.db.execute("SELECT COUNT(*) FROM c_event WHERE kind='SEND_INTENT'").fetchone()[0], 2)
                self.assertEqual(store.db.execute("SELECT state FROM c_outbox WHERE id='other-unknown'").fetchone()[0], 'DELIVERY_UNKNOWN')
                self.assertIsNone(box.next_ready('s'))  # never clear another UNKNOWN
                with self.assertRaisesRegex(IntegrationError, 'LOCAL_SEND_VALIDATION_REJECTED'):
                    box.prepare_send('old-send', op, {'content': 'original reply', 'mentions': ['@example-account/dh-builder']})
                with self.assertRaisesRegex(IntegrationError, 'LOCAL_SEND_VALIDATION_REJECTED'):
                    box.sent('old-send', {'success': True})
                with self.assertRaisesRegex(IntegrationError, 'OUTBOX_ID_CONFLICT'):
                    box.prepare_send('old-send', op, {'content': 'changed'})
                self.assertEqual(len(box.read_work(other)['input']) > 0, True)
                self.assertEqual(store.db.execute('SELECT COUNT(*) FROM c_inbox').fetchone()[0], 2)
            finally:
                service.close()
                store.close()

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
