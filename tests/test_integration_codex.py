"""COMPONENT against installed Band SDK4 turn runner; no inference/Band traffic."""
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from importlib.util import find_spec
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from test_integration_mailbox import Owner
from darkharness.integration.mailbox import Mailbox, IntegrationError
from darkharness.integration.policy import ApprovalRouter
from darkharness.integration.artifacts import SecretGuard


HAS_SDK = find_spec("band") is not None
if HAS_SDK:
    from band.adapters.codex import CodexAdapterConfig
    from band.core.types import PlatformMessage
    from band.integrations.codex.types import CodexSessionState
    from darkharness.integration.codex import DurableCodexAdapter, GuardedTools, CodexRuntime


class Client:
    def __init__(self, events=None, gate=None):
        self.requests, self.responses = [], []
        self.queue = asyncio.Queue()
        self.events = events or []
        self.gate = gate
        self.closed = False
        self.turns = 0

    async def connect(self):
        self.closed = False

    async def initialize(self, **kwargs):
        return {"userAgent": "component"}

    async def request(self, method, params=None, **kwargs):
        self.requests.append((str(method), params))
        if method == "thread/start":
            return {"thread": {"id": "session"}}
        if method == "thread/resume":
            return {"thread": {"id": params["threadId"]}}
        if method == "model/list":
            return {"data": [{"id": "test-model", "supportedReasoningEfforts": [{"reasoningEffort": "high"}]}]}
        if method == "account/read":
            return {"account": {"type": "chatgpt"}}
        if method == "turn/start":
            self.turns += 1
            tid = str(self.turns)
            for event in self.events:
                self.queue.put_nowait(event)
            self.queue.put_nowait(SimpleNamespace(kind="notification", method="turn/completed", id=None,
                            params={"turn": {"id": tid, "status": "completed"}}))
            return {"turn": {"id": tid}}
        return {}

    async def recv_event(self, timeout_s=None):
        if self.gate:
            await self.gate.wait()
        return await self.queue.get()

    async def respond(self, identifier, body):
        self.responses.append((identifier, body))

    async def respond_error(self, identifier, **body):
        self.responses.append((identifier, body))

    async def close(self):
        self.closed = True


class Tools:
    def __init__(self):
        self.sent, self.events = [], []

    def get_openai_tool_schemas(self, **kwargs):
        return [{"type": "function", "function": {"name": "band_send_message", "parameters": {"type": "object"}}}]

    async def send_message(self, content, mentions=None):
        self.sent.append((content, mentions))
        return {"data": {"id": "msg-" + str(len(self.sent))}}

    async def send_event(self, content, message_type, metadata=None):
        self.events.append((content, message_type, metadata))
        return {"data": {"id": "event-" + str(len(self.events))}}

    async def no_reply(self, **kwargs):
        return {"status": "no_reply"}


@unittest.skipUnless(HAS_SDK, "installed Band SDK4 required")
class CodexTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.owner = Owner()
        self.box = Mailbox(self.owner)
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name).resolve()
        self.owner.grant(self.root, file_write=True, approved_commands=[["git", "status"]])
        self.router = ApprovalRouter(self.owner, "g", "s", "r", "run", self.root)
        # Official pattern semantics are independently exercised in artifacts test.
        self.guard = SecretGuard([("fixture-pattern", r"\bsk-\w{16,}")], source_hash="component")
        self.tools = Tools()
        self.client = Client()
        self.adapter = self.make_adapter()
        await self.adapter.on_started("dh builder", "component")
        self.history = CodexSessionState()

    def make_adapter(self, client=None, **config_override):
        outer = client or self.client
        class Adapter(DurableCodexAdapter):
            def _build_client(self, config):
                return outer
        kwargs = {"model": "test-model", "reasoning_effort": "high", "workspace_for_room": lambda room: str(self.root),
                  "sandbox": "workspace-write", "approval_policy": "on-request", "system_prompt": "generic full mandate",
                  "inject_history_on_resume_failure": False}
        kwargs.update(config_override)
        return Adapter(mailbox=self.box, router=self.router, guard=self.guard, alias="s", display_name="dh builder",
                       room_id="r", workspace=self.root, coordinator_id="coordinator-id", config=CodexAdapterConfig(**kwargs))

    async def asyncTearDown(self):
        self.adapter.stopping = True
        if self.adapter.worker:
            if not self.adapter.worker.done():
                self.adapter.worker.cancel()
            await asyncio.wait_for(asyncio.gather(self.adapter.worker, return_exceptions=True), timeout=3)
        await asyncio.wait_for(self.adapter.on_cleanup("r"), timeout=3)
        self.owner.db.close()
        self.td.cleanup()

    def message(self, mid="m", content="full task", sender="peer"):
        return PlatformMessage(mid, "r", content, sender, "agent", "peer name", "text", {}, datetime.now(timezone.utc))

    async def deliver(self, msg):
        await self.adapter.on_message(msg, self.tools, self.history, "full roster", None, is_session_bootstrap=True, room_id="r")

    async def settle(self):
        if self.adapter.worker:
            await asyncio.wait_for(self.adapter.worker, timeout=5)

    async def test_real_sdk_runner_and_policy_params(self):
        await self.deliver(self.message())
        await self.settle()
        starts = [p for m, p in self.client.requests if m == "turn/start"]
        self.assertEqual(len(starts), 1)
        params = starts[0]
        self.assertEqual(params["cwd"], str(self.root))
        self.assertEqual(params["approvalPolicy"], "on-request")
        self.assertEqual(params["sandboxPolicy"], {"type": "workspaceWrite"})
        self.assertIn("full task", str(params["input"]))
        work = self.owner.db.execute("SELECT * FROM c_work").fetchone()
        self.assertEqual(work["delivery"], "RETURNED")
        self.assertEqual(work["state"], "SUCCEEDED")
        self.assertEqual(json.loads(work["result"])["acceptance"], "NOT_EVALUATED")

    async def test_busy_durable_queue_dedup_and_no_turn_wait(self):
        gate = asyncio.Event()
        self.client.gate = gate
        await self.deliver(self.message())
        await asyncio.sleep(0)
        await self.deliver(self.message("m2", "second full task"))
        await self.deliver(self.message("m2", "second full task"))
        self.assertFalse(self.adapter.worker.done())
        self.assertEqual(self.owner.db.execute("SELECT COUNT(*) FROM c_work").fetchone()[0], 2)
        gate.set()
        await self.settle()
        self.assertEqual(self.client.turns, 2)
        self.assertEqual(len([p for m,p in self.client.requests if m == "turn/start"]), 2)

    async def test_approval_single_callback_owner_not_manual_wait(self):
        event = SimpleNamespace(kind="request", method="item/commandExecution/requestApproval", id=7,
                    params={"command": "git status", "cwd": str(self.root)})
        self.client.events = [event, event]
        await self.deliver(self.message())
        await self.settle()
        self.assertEqual(self.client.responses, [(7, {"decision": "accept"})])
        self.assertFalse(self.adapter._pending_approvals)

    async def test_arbitrary_shell_unknown_method_denied(self):
        self.client.events = [SimpleNamespace(kind="request", method="item/commandExecution/requestApproval", id=8,
               params={"command": "bash -c arbitrary", "cwd": str(self.root)}),
               SimpleNamespace(kind="request", method="unknown/privilege", id=9, params={})]
        await self.deliver(self.message())
        await self.settle()
        self.assertEqual(self.client.responses[0], (8, {"decision": "decline"}))
        self.assertEqual(self.client.responses[1][1]["code"], -32601)

    async def test_peer_question_native_callback_yield_and_continuation(self):
        self.client.events = [SimpleNamespace(kind="request", method="item/tool/requestUserInput", id=10,
                params={"questions": [{"id": "choice", "question": "which option?"}]})]
        await self.deliver(self.message())
        await self.settle()
        parent = dict(self.owner.db.execute("SELECT * FROM c_work").fetchone())
        self.assertEqual(parent["delivery"], "YIELDED")
        self.assertEqual(parent["state"], "PAUSED")
        self.assertTrue(self.client.closed)
        q = self.owner.db.execute("SELECT * FROM c_question").fetchone()
        self.assertIn("full task", q["context"])
        self.assertEqual(self.tools.sent[-1][1], ["coordinator-id"])
        self.client.events = []
        # Closed client's old event stream belongs to old attempt, not continuation.
        while not self.client.queue.empty():
            self.client.queue.get_nowait()
        await self.deliver(self.message("answer-id", "/dh-answer " + q["id"] + "\ncomplete peer answer", "coordinator-id"))
        await self.settle()
        self.assertEqual(self.client.turns, 2)
        second = [p for m,p in self.client.requests if m == "turn/start"][-1]
        self.assertIn("complete peer answer", str(second["input"]))
        self.assertIn("full task", str(second["input"]))
        self.assertTrue(any(m == "thread/resume" for m,p in self.client.requests))

    async def test_dynamic_peer_tool_uses_same_yield_path(self):
        self.client.events = [SimpleNamespace(kind="request", method="item/tool/call", id=11,
                     params={"tool": "dh_peer_question", "arguments": {"question": "full question"}, "callId": "call"})]
        await self.deliver(self.message())
        await self.settle()
        self.assertEqual(self.owner.db.execute("SELECT delivery FROM c_work").fetchone()[0], "YIELDED")
        self.assertEqual(self.client.responses[0][1]["success"], True)

    async def test_outbox_guard_all_adapter_and_tool_paths(self):
        op = self.box.receive("s", "r", "peer", "m", "task")["work"]
        self.box.claim(op, "a")
        tools = GuardedTools(self.tools, self.adapter, op, "a")
        assembled = "sk" + "-" + "Z" * 20
        with self.assertRaisesRegex(IntegrationError, "OUTBOX_SECRET_BLOCKED"):
            await tools.send_message(assembled, ["peer"])
        with self.assertRaisesRegex(IntegrationError, "OUTBOX_SECRET_BLOCKED"):
            await tools.send_event("result", "tool_result", {"output": assembled})
        outcome = await tools.execute_tool_call_structured("band_send_message", {"content": assembled, "mentions": ["peer"]})
        self.assertFalse(outcome.ok)
        self.assertFalse(self.tools.sent)
        self.assertFalse(self.tools.events)
        self.assertEqual(self.owner.db.execute("SELECT COUNT(*) FROM c_outbox").fetchone()[0], 0)

    async def test_duplicate_dynamic_send_callback_not_replayed(self):
        event = SimpleNamespace(kind="request", method="item/tool/call", id=12,
                     params={"tool": "band_send_message", "arguments": {"content": "reply", "mentions": ["peer"]}, "callId": "same-call"})
        self.client.events = [event, event]
        await self.deliver(self.message())
        await self.settle()
        self.assertEqual(len(self.tools.sent), 1)
        self.assertEqual(len([r for r in self.client.responses if r[0] == 12]), 1)

    async def test_unknown_platform_effect_blocked(self):
        op = self.box.receive("s", "r", "peer", "m", "task")["work"]
        self.box.claim(op, "a")
        tools = GuardedTools(self.tools, self.adapter, op, "a")
        result = await tools.execute_tool_call_structured("band_create_chatroom", {})
        self.assertFalse(result.ok)
        self.assertEqual(result.value["error"], "PLATFORM_EFFECT_NOT_GRANTED")

    async def test_local_slash_cannot_escalate_sandbox_or_model(self):
        await self.deliver(self.message(content="/sandbox danger-full-access --confirm"))
        await self.settle()
        self.assertEqual(self.adapter.config.sandbox, "workspace-write")
        self.assertFalse(self.adapter._sandbox_overrides)
        starts = [p for m,p in self.client.requests if m == "turn/start"]
        self.assertEqual(starts[0]["sandboxPolicy"], {"type": "workspaceWrite"})

    async def test_runtime_contract_readiness_no_inference(self):
        result = await CodexRuntime(self.adapter).readiness()
        self.assertEqual(result["inference"], "NOT_PROBED")
        self.assertFalse(any(m == "turn/start" for m,p in self.client.requests))

    async def test_unsafe_config_fail_closed(self):
        with self.assertRaisesRegex(IntegrationError, "UNSAFE_RUNTIME_CONFIGURATION"):
            self.make_adapter(sandbox="danger-full-access")

    @unittest.skipUnless(__import__('sys').platform == 'linux', 'Linux owned process group')
    async def test_owned_group_cancel_observed_without_touching_unrelated_process(self):
        import sys
        from darkharness.integration.codex import OwnedStdioClient, group_members
        unrelated = await asyncio.create_subprocess_exec(sys.executable, '-B', '-c', 'import time; time.sleep(300)', start_new_session=True)
        records = []
        script = 'import subprocess,sys,time; subprocess.Popen([sys.executable,"-B","-c","import time; time.sleep(300)"]); print("ready", flush=True); time.sleep(300)'
        client = OwnedStdioClient(command=(sys.executable, '-B', '-c', script), cwd=str(self.root), env={},
                                  guard=self.guard, record=lambda kind,data: records.append((kind,data)))
        try:
            await client.connect()
            for _ in range(100):
                if len(group_members(client.group)) >= 2:
                    break
                await asyncio.sleep(.01)
            self.assertGreaterEqual(len(group_members(client.group)), 2)
            await client.close()
            self.assertFalse(group_members(client.group))
            self.assertIsNone(unrelated.returncode)
            self.assertTrue(any(kind == 'PROCESS_STOPPED' for kind,data in records))
        finally:
            if client._proc and client._proc.returncode is None:
                await client.close()
            unrelated.terminate()
            await unrelated.wait()

    async def test_sdk_stop_hook_reaches_detached_turn(self):
        self.client.gate = asyncio.Event()
        await self.deliver(self.message())
        await asyncio.sleep(0)
        await self.adapter.on_interrupt('r', 'stop')
        work = self.owner.db.execute('SELECT * FROM c_work').fetchone()
        self.assertEqual(work['state'], 'CANCELLED')
        self.assertTrue(self.client.closed)
        self.assertTrue(self.adapter.stopping)
        self.assertEqual(self.owner.db.execute("SELECT state FROM c_control").fetchone()[0], 'DRAINED')

    async def test_normalized_handle_peer_answer_while_busy_is_control_not_work(self):
        self.client.events = [SimpleNamespace(kind='request', method='item/tool/requestUserInput', id=61, params={'questions': [{'id': 'bytes', 'question': 'byte count?'}]})]
        await self.deliver(self.message(content='original full task'))
        await self.settle()
        q = self.owner.db.execute('SELECT * FROM c_question').fetchone()
        parent = self.box.read_work(q['operation'])
        self.client.events = []
        while not self.client.queue.empty():
            self.client.queue.get_nowait()
        gate = asyncio.Event()
        self.client.gate = gate
        await self.deliver(self.message('busy', 'other authorized task'))
        await asyncio.sleep(0)
        answer = '@kjuho0403/dh-builder /dh-answer ' + q['id'] + '\ncomplete peer answer: 21 bytes'
        with self.assertRaisesRegex(IntegrationError, 'PEER_BINDING_DENIED'):
            await self.deliver(self.message('spoof', answer, 'wrong-sender'))
        await self.deliver(self.message('995e6181-24a1-4115-880f-ed19dc73652b', answer, 'coordinator-id'))
        self.assertFalse(self.adapter.worker.done())
        canonical = self.owner.db.execute("SELECT * FROM c_event WHERE kind='PEER_ANSWER'").fetchall()
        self.assertEqual(len(canonical), 1)
        self.assertEqual(canonical[0]['operation'], parent['id'])
        q = self.owner.db.execute('SELECT * FROM c_question').fetchone()
        child = self.box.read_work(q['continuation'])
        self.assertEqual(child['thread'], parent['thread'])
        self.assertEqual(json.loads(child['input'])['parent_operation'], parent['id'])
        self.assertEqual(self.owner.db.execute('SELECT COUNT(*) FROM c_inbox').fetchone()[0], 2)
        gate.set()
        await self.settle()
        self.assertEqual(self.client.turns, 3)
        self.assertTrue(any(m == 'thread/resume' and p['threadId'] == parent['thread'] for m, p in self.client.requests))
        final = [p for m, p in self.client.requests if m == 'turn/start'][-1]
        self.assertIn('original full task', str(final['input']))
        self.assertIn('complete peer answer: 21 bytes', str(final['input']))

    async def test_unknown_legacy_thread_cutover_binds_new_dynamic_tools_and_lineage(self):
        self.history = CodexSessionState(thread_id='legacy-thread', room_id='r')
        await self.deliver(self.message(content='original maintenance task'))
        await self.settle()
        self.assertFalse(any(m == 'thread/resume' for m, p in self.client.requests))
        starts = [p for m, p in self.client.requests if m == 'thread/start']
        self.assertEqual(len(starts), 1)
        self.assertIn('dh_local_git_commit', [s['name'] for s in starts[0]['dynamicTools']])
        event = self.owner.db.execute("SELECT body FROM c_event WHERE kind='THREAD_TOOLSET_BOUND'").fetchone()
        body = json.loads(event[0])['data']
        self.assertEqual(body['old_thread'], 'legacy-thread')
        self.assertEqual(body['thread'], 'session')
        self.assertTrue(body['cutover'])
        self.assertIn('original maintenance task', str([p for m, p in self.client.requests if m == 'turn/start'][0]['input']))

    async def test_readiness_reader_owned_identity_and_redacted_diagnostic(self):
        import sys
        from darkharness.integration.codex import ClientEvidence, OwnedStdioClient
        evidence = ClientEvidence(self.adapter)
        assembled = 'sk' + '-' + 'R' * 20
        self.guard.register_known(assembled)
        script = 'import time,json,sys; time.sleep(.1); print(json.dumps({"method":"fixture/event","params":{"text":sys.argv[1]}}),flush=True); print(sys.argv[1],file=sys.stderr,flush=True); time.sleep(.2)'
        client = OwnedStdioClient(command=(sys.executable, '-B', '-c', script, assembled), cwd=str(self.root), env={}, guard=self.guard, record=evidence.record)
        # Reader tasks are created before any dispatch context is set.
        await client.connect()
        op = self.box.receive('s', 'r', 'peer', 'reader', 'task')['work']
        self.box.claim(op, 'reader-attempt')
        evidence.bind((op, 'reader-attempt'))
        token = self.adapter.current.set(('unrelated', 'new-attempt'))
        try:
            await asyncio.sleep(.15)
            evidence.record('LATE_EVENT', {'diagnostic': assembled})
            with self.assertRaisesRegex(IntegrationError, 'REBIND'):
                evidence.bind(('unrelated', 'new-attempt'))
            rows = self.owner.db.execute("SELECT * FROM c_event WHERE kind IN ('STDOUT_RPC','STDERR','LATE_EVENT')").fetchall()
            self.assertEqual({r['kind'] for r in rows}, {'STDOUT_RPC', 'STDERR', 'LATE_EVENT'})
            for row in rows:
                self.assertEqual(row['operation'], op)
                self.assertEqual(json.loads(row['body'])['attempt'], 'reader-attempt')
                self.assertNotIn(assembled, row['body'])
                self.assertIn('[REDACTED]', row['body'])
        finally:
            await client.close()
            self.adapter.current.reset(token)

    async def test_typed_git_tool_through_actual_sdk_dynamic_interface(self):
        import subprocess
        def git(*args):
            return subprocess.check_output(['git', *args], cwd=self.root, stderr=subprocess.PIPE).decode().strip()
        git('init', '-q')
        (self.root / 'base').write_text('base')
        git('add', 'base')
        git('-c', 'user.name=f', '-c', 'user.email=f@actors.invalid', 'commit', '-qm', 'base')
        head = git('rev-parse', 'HEAD')
        (self.root / 'smoke.txt').write_text('DarkHarness smoke ok\n')
        self.owner.grant(self.root, local_git_write={'directories': ['.']}, review_snapshot={'scratch': str(self.root / '.review')})
        event = SimpleNamespace(kind='request', method='item/tool/call', id=51, params={'tool': 'dh_local_git_commit', 'arguments': {'cwd': str(self.root), 'paths': ['smoke.txt'], 'message': 'smoke', 'expected_head': head}, 'callId': 'git-call'})
        self.client.events = [event, event]
        await self.deliver(self.message())
        await self.settle()
        self.assertEqual([r[1]['success'] for r in self.client.responses if r[0] == 51], [True])
        self.assertNotEqual(git('rev-parse', 'HEAD'), head)
        self.assertEqual(git('status', '--porcelain'), '')
        schemas = [p for m, p in self.client.requests if m == 'thread/start'][0]['dynamicTools']
        self.assertIn('dh_local_git_commit', [s['name'] for s in schemas])
        self.assertIn('dh_review_snapshot', [s['name'] for s in schemas])
        reports = [content for content, typ, meta in self.tools.events if typ in {'tool_call', 'tool_result'}]
        self.assertTrue(any('dh_local_git_commit' in content for content in reports))
        self.assertTrue(any('COMMITTED' in content for content in reports))

    async def test_stale_cancel_does_not_touch_other_attempt(self):
        await self.deliver(self.message())
        await self.settle()
        work = self.owner.db.execute("SELECT * FROM c_work").fetchone()
        with self.assertRaisesRegex(IntegrationError, "OWNER_ATTEMPT_FENCE"):
            await self.adapter.cancel_owned(work["id"], "stale")
