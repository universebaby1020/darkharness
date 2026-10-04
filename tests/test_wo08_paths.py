"""Additional SDK4/local-process WO08 paths; all providers/platforms are fake."""
import asyncio
import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock
import test_integration_codex as codex_fixture
import test_integration_wiring as wiring
from darkharness.integration.mailbox import IntegrationError
from darkharness.integration.protected_tools import GuardedTools, LocalSendRejected
from darkharness.integration.verification_bridge import VerificationYield


class ReplyTelemetryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = codex_fixture.CodexTests.asyncSetUp
    asyncTearDown = codex_fixture.CodexTests.asyncTearDown
    make_adapter = codex_fixture.CodexTests.make_adapter
    message = codex_fixture.CodexTests.message
    deliver = codex_fixture.CodexTests.deliver
    settle = codex_fixture.CodexTests.settle

    def facade(self):
        op = self.box.receive('s', 'r', 'peer', 'm', 'task')['work']
        self.box.claim(op, 'a')
        return GuardedTools(self.tools, self.adapter, op, 'a')

    async def test_sdk_telemetry_recording_failure_is_harmless(self):
        tools = self.facade()
        with patch.object(self.box, 'observe', side_effect=RuntimeError('fixture')):
            self.assertEqual(await tools.send_event('local', 'task'), {'ok': False})
            self.assertEqual(await tools.send_failure(SimpleNamespace()), {'ok': False})
        self.assertEqual(self.owner.db.execute('SELECT COUNT(*) FROM c_outbox').fetchone()[0], 0)
        self.assertEqual(self.tools.events, [])

    async def test_sdk_reply_redacted_model_secret_still_blocked(self):
        tools = self.facade()
        value = 'sk' + '-' + 'Q' * 20
        await tools.send_message(value, ['peer'])
        self.assertNotIn(value, str(self.tools.sent))
        self.assertIn('[REDACT', str(self.tools.sent))
        outcome = await tools.execute_tool_call_structured('band_send_message', {'content': value})
        self.assertFalse(outcome.ok)
        self.assertEqual(len(self.tools.sent), 1)
        with patch.object(self.router, 'active', return_value=False):
            result = await tools.send_message('safe reply')
        self.assertEqual(result['effect'], 'NOT_SENT')
        self.assertEqual(self.adapter._reply_not_sent['code'], 'GRANT_INACTIVE')

    async def test_real_sdk_send_disables_hidden_retries_and_typed_rejection(self):
        from band.runtime.tools import AgentTools
        from band_rest.core.api_error import ApiError
        tools = self.facade()
        endpoint = SimpleNamespace(create_agent_chat_message=AsyncMock(side_effect=ApiError(status_code=403, body={})))
        raw = AgentTools('r', SimpleNamespace(agent_api_messages=endpoint), participants=self.tools.participants)
        tools.raw = raw
        with self.assertRaises(LocalSendRejected):
            await tools._send('send_message', {'content': 'safe', 'mentions': ['peer']})
        self.assertEqual(endpoint.create_agent_chat_message.await_args.kwargs['request_options']['max_retries'], 0)
        self.assertEqual(self.owner.db.execute('SELECT state FROM c_outbox').fetchone()[0], 'REJECTED')
        # A retryable HTTP result is not proof of zero sends.
        endpoint.create_agent_chat_message.side_effect = ApiError(status_code=429, body={})
        with self.assertRaises(ApiError):
            await tools._send('send_message', {'content': 'safe second', 'mentions': ['peer']})
        self.assertEqual(self.owner.db.execute("SELECT COUNT(*) FROM c_outbox WHERE state='DELIVERY_UNKNOWN'").fetchone()[0], 1)

    async def test_locally_unsent_peer_question_is_terminal_not_orphaned_yield(self):
        self.client.events = [SimpleNamespace(kind='request', method='item/tool/requestUserInput', id=10, params={'questions': [{'id': 'choice', 'question': 'question'}]})]
        with patch('darkharness.integration.protected_tools.validate_local_send', side_effect=LocalSendRejected('LOCAL_SEND_VALIDATION_REJECTED')):
            await self.deliver(self.message())
            await self.settle()
        row = self.owner.db.execute('SELECT * FROM c_work').fetchone()
        self.assertEqual((row['state'], row['delivery']), ('FAILED', 'RETURNED'))
        self.assertEqual(json.loads(row['result'])['reply']['effect'], 'NOT_SENT')
        self.assertEqual(self.tools.sent, [])
        self.assertEqual(self.owner.db.execute("SELECT COUNT(*) FROM c_outbox WHERE state='DELIVERY_UNKNOWN'").fetchone()[0], 0)

    async def test_verification_yields_even_if_sdk_callback_reply_raises(self):
        op = self.box.receive('s', 'r', 'peer', 'm', 'task')['work']
        self.box.claim(op, 'a')
        token = self.adapter.current.set((op, 'a'))
        tools = GuardedTools(self.tools, self.adapter, op, 'a')
        async def broken(*args, **kwargs):
            self.adapter._verification_effect = 'fixture-effect'
            raise RuntimeError('fixture callback transport')
        try:
            with patch.object(codex_fixture.DurableCodexAdapter.__bases__[-1], '_handle_server_request', new=broken):
                with self.assertRaises(VerificationYield):
                    await self.adapter._handle_server_request(tools=tools, msg=self.message(), room_id='r', event=SimpleNamespace(id=3, method='item/tool/call', params={'tool': 'dh_verify'}))
        finally:
            self.adapter.current.reset(token)


class CheckerBoundaryTests(unittest.TestCase):
    setUp = wiring.BridgeTests.setUp
    tearDown = wiring.BridgeTests.tearDown
    run_check = wiring.BridgeTests.run_check

    def test_popen_failure_not_executed_continuation(self):
        import subprocess, sys
        original = subprocess.Popen
        def popen(argv, *args, **kwargs):
            if argv[0] == str(__import__('pathlib').Path(sys.executable).resolve()):
                raise OSError('fixture')
            return original(argv, *args, **kwargs)
        with patch('darkharness.integration.verification.subprocess.Popen', side_effect=popen):
            effect, result = self.run_check()
        self.assertEqual((result['state'], result['external_execution']), ('FAILED', 'NOT_EXECUTED'))
        self.assertIsNotNone(self.bridge.complete(self.fixture.op, 'a', effect))

    def test_thread_start_failure_not_executed_continuation(self):
        f = self.fixture
        with patch('darkharness.integration.verification.threading.Thread.start', side_effect=RuntimeError('fixture')):
            value = self.bridge.broker.start(f.op, 'a', 'thread-effect', check_id='check', checkout_receipt=f.snap, revision=f.revision)
        self.assertEqual(value['result']['external_execution'], 'NOT_EXECUTED')
        self.assertIsNotNone(self.bridge.complete(f.op, 'a', 'thread-effect'))

    def test_transient_authority_observation_does_not_kill_but_cancel_does(self):
        f = self.fixture
        f.mode('sleep')
        broker = self.bridge.broker
        effect, _ = asyncio.run(self.bridge.start(f.op, 'a', 'slow', {'check_id': 'check', 'checkout_receipt': f.snap, 'revision': f.revision}))
        end = time.monotonic() + 3
        while broker.jobs[effect].process is None and time.monotonic() < end:
            time.sleep(.01)
        self.assertIsNotNone(broker.jobs[effect].process)
        with patch.object(broker, '_kill', wraps=broker._kill) as kill:
            with patch.object(broker, '_live', return_value=False):
                time.sleep(.15)
                self.assertIsNone(broker.jobs[effect].process.poll())
                kill.assert_not_called()
                self.assertGreater(f.owner.db.execute("SELECT COUNT(*) FROM c_event WHERE kind='VERIFICATION_AUTHORITY_UNCERTAIN'").fetchone()[0], 0)
            broker.cancel(f.op, 'a', effect)
            result = asyncio.run(asyncio.wait_for(broker.wait(f.op, 'a', effect), 3))
            self.assertEqual(result['state'], 'CANCELLED')
            self.assertGreater(kill.call_count, 0)
        self.assertIsNone(self.bridge.complete(f.op, 'a', effect))

    def test_native_cleanup_unknown_cannot_publish_ready_child(self):
        effect, _ = self.run_check()
        f = self.fixture
        f.box.observe(f.op, 'a', 'PROCESS_STOP_UNKNOWN', {})
        self.assertIsNone(self.bridge.complete(f.op, 'a', effect))
        self.assertEqual(f.owner.db.execute('SELECT COUNT(*) FROM c_verification_continuation').fetchone()[0], 0)


class ProviderDispatchIntakeTests(unittest.IsolatedAsyncioTestCase):
    """Actual SDK types/formatter at durable intake; no network or model turn."""
    asyncSetUp = wiring.OwnedSdkTests.asyncSetUp
    asyncTearDown = wiring.OwnedSdkTests.asyncTearDown

    def prepare_dispatch(self, *, raw_hash=False):
        from datetime import datetime, timezone
        from band.core.types import PlatformMessage
        from band.runtime.formatters import replace_uuid_mentions
        from darkharness.integration.mailbox import encode, digest
        from darkharness.integration.provider_recovery import prepare_settled_provider_recovery, register_policy
        roster = [
            {'id': '00000000-0000-4000-8000-000000000001', 'handle': 'synthetic-seat', 'name': 'Synthetic Seat', 'type': 'Agent'},
            {'id': '00000000-0000-4000-8000-000000000002', 'handle': None, 'name': ' Synthetic   Reviewer ', 'type': 'Agent'},
        ]
        raw = '@[[00000000-0000-4000-8000-000000000001]] @[[00000000-0000-4000-8000-000000000002]] exact synthetic directive'
        content = replace_uuid_mentions(raw, roster)
        self.assertEqual(content, '@synthetic-seat @Synthetic-Reviewer exact synthetic directive')
        self.assertNotEqual(digest(raw.encode('utf-8')), digest(content.encode('utf-8')))
        f = self.fixture
        grant = json.loads(f.owner.db.execute("SELECT body FROM controls WHERE kind='grant'").fetchone()[0])
        grant['scope'] = prepare_settled_provider_recovery(grant['scope'], run_id='run', workspace=str(f.repo), rooms=['r'], seats=['s'], dispatch_sender='human', dispatch_room='r', dispatch_sha256=digest((raw if raw_hash else content).encode('utf-8')))
        f.owner.db.execute("UPDATE controls SET body=? WHERE kind='grant'", (encode(grant),))
        register_policy(f.box, self.adapter.router)
        self.grant_before = tuple(f.owner.db.execute("SELECT body,revision FROM controls WHERE kind='grant'").fetchone())
        return PlatformMessage('synthetic-dispatch', 'r', content, 'human', 'User', 'Synthetic Human', 'text', {}, datetime.now(timezone.utc))

    async def intake(self, msg):
        from band.integrations.codex.types import CodexSessionState
        # Exercise DurableSeatMixin.on_message, stopping only the worker wake.
        with patch.object(self.adapter, '_wake'), patch.object(self.adapter, '_hydrate_startup_tools', new=AsyncMock()):
            await self.adapter.on_message(msg, self.tools, CodexSessionState(), None, None, is_session_bootstrap=True, room_id='r')

    def receipts(self):
        return self.fixture.owner.db.execute('SELECT * FROM c_run_dispatch').fetchall()

    async def test_sdk_user_transformed_content_binds_immutable_receipt(self):
        from darkharness.integration.mailbox import digest
        msg = self.prepare_dispatch()
        await self.intake(msg)
        self.assertEqual(len(self.receipts()), 1)
        row = self.receipts()[0]
        self.assertEqual((row['platform_id'], row['dispatched_at']), (msg.id, msg.created_at.timestamp()))
        pinned = self.fixture.owner.db.execute('SELECT hash FROM c_provider_policy').fetchone()[0]
        self.assertEqual(row['policy_hash'], pinned)
        proof = json.loads(self.fixture.owner.db.execute('SELECT body FROM c_artifact WHERE hash=?', (row['evidence_ref'],)).fetchone()[0])
        self.assertEqual(proof['content_sha256'], digest(msg.content.encode('utf-8')))
        await self.intake(msg)  # identical delivery is idempotent
        self.assertEqual(len(self.receipts()), 1)
        self.assertEqual(tuple(row), tuple(self.receipts()[0]))
        self.assertEqual(self.grant_before, tuple(self.fixture.owner.db.execute("SELECT body,revision FROM controls WHERE kind='grant'").fetchone()))

    async def test_exact_sender_room_content_and_sender_class_fences(self):
        from dataclasses import replace
        msg = self.prepare_dispatch()
        cases = [{'sender_id': 'other-human'}, {'room_id': 'other-room'},
                 {'content': msg.content + ' '}, {'content': msg.content.lower()},
                 *({'sender_type': kind} for kind in ('Agent', 'agent', 'USER', 'Human', 'system', ''))]
        for n, changes in enumerate(cases):
            with self.subTest(changes=changes):
                await self.intake(replace(msg, id='wrong-' + str(n), **changes))
                self.assertEqual(len(self.receipts()), 0)
        await self.intake(msg)
        self.assertEqual(len(self.receipts()), 1)

    async def test_raw_wire_preregistration_does_not_match_sdk_intake(self):
        msg = self.prepare_dispatch(raw_hash=True)
        await self.intake(msg)
        self.assertEqual(len(self.receipts()), 0)

    async def test_timestamp_and_ambiguous_duplicate_fences(self):
        from dataclasses import replace
        from datetime import datetime, timezone, timedelta
        msg = self.prepare_dispatch()
        for n, at in enumerate((datetime.fromtimestamp(1, timezone.utc), datetime.now(timezone.utc) + timedelta(hours=1))):
            with self.assertRaisesRegex(IntegrationError, 'DISPATCH_POLICY_NOT_PREREGISTERED'):
                await self.intake(replace(msg, id='wrong-time-' + str(n), created_at=at))
            self.assertEqual(len(self.receipts()), 0)
        with self.assertRaisesRegex(IntegrationError, 'DISPATCH_UTC_TIMESTAMP_REQUIRED'):
            await self.intake(replace(msg, id='naive-time', created_at=msg.created_at.replace(tzinfo=None)))
        await self.intake(msg)
        before = tuple(self.receipts()[0])
        for changes in ({'id': 'ambiguous-second-dispatch'}, {'created_at': msg.created_at + timedelta(microseconds=1)}):
            with self.assertRaisesRegex(IntegrationError, 'DISPATCH_RECEIPT_CONFLICT'):
                await self.intake(replace(msg, **changes))
            self.assertEqual(tuple(self.receipts()[0]), before)

    async def test_changed_policy_cannot_bind(self):
        from darkharness.integration.mailbox import encode
        msg = self.prepare_dispatch()
        f = self.fixture
        grant = json.loads(self.grant_before[0])
        grant['scope']['provider_dispatch']['sender_id'] = 'changed-human'
        f.owner.db.execute("UPDATE controls SET body=? WHERE kind='grant'", (encode(grant),))
        await self.intake(msg)
        self.assertEqual(len(self.receipts()), 0)


class PreStartSdkTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = wiring.OwnedSdkTests.asyncSetUp
    async def asyncTearDown(self):
        if hasattr(self, 'coordinator_adapter'):
            await asyncio.wait_for(self.coordinator_adapter.on_cleanup('r'), 8)
        await wiring.OwnedSdkTests.asyncTearDown(self)

    deliver = wiring.OwnedSdkTests.deliver
    settle = wiring.OwnedSdkTests.settle
    events = wiring.OwnedSdkTests.events

    async def check_requeued(self):
        await self.deliver('failure', 'same original task')
        await self.settle()
        row = self.fixture.owner.db.execute("SELECT * FROM c_work WHERE delivery='READY'").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row['state'], 'QUEUED')
        self.assertGreater(self.fixture.owner.db.execute('SELECT not_before FROM c_retry_wait WHERE operation=?', (row['id'],)).fetchone()[0], time.time() + 55)
        self.assertTrue(self.events('ATTEMPT_PROCESS_CLEAN'))
        self.assertFalse(self.events('TURN_ACCEPTED'))

    async def test_fourth_prestart_failure_drains_next_work_and_schedules_coordinator(self):
        # Exercise actual SDK runner/owned process retirement, not classifier-only
        # rows. Only the clock is advanced; production backoff remains 60 seconds.
        from darkharness.integration.codex import OwnedStdioClient
        notifications = []
        from band.adapters.codex import CodexAdapterConfig
        f = self.fixture
        f.owner.db.execute("UPDATE c_work SET state='SUCCEEDED',delivery='RETURNED' WHERE seat='peer'")
        coordinator = type(self.adapter)(mailbox=f.box, router=f.peer_router, guard=self.guard, alias='peer', display_name='fixture coordinator', room_id='r', workspace=f.repo, coordinator_id='coordinator-id', config=CodexAdapterConfig(model='test-model', reasoning_effort='high', workspace_for_room=lambda _: str(f.repo), sandbox='workspace-write', approval_policy='on-request', system_prompt='generic coordinator mandate', inject_history_on_resume_failure=False))
        await coordinator.on_started('fixture coordinator', 'component')
        coordinator.raw_tools = self.tools
        self.coordinator_adapter = coordinator
        def notify(operation, attempt):
            child = f.box.notify_peer_blocker(operation, attempt, 'peer', 'r', 'run')
            if child:
                notifications.append('wake')
                coordinator._wake()
        self.adapter.coordinator_notify = notify
        original = OwnedStdioClient.request
        failures = 0
        async def request(client, method, params=None, **kwargs):
            nonlocal failures
            if method == 'thread/start' and failures < 4:
                failures += 1
                raise RuntimeError('fixture prestart')
            return await original(client, method, params, **kwargs)
        loop = asyncio.get_running_loop()
        with patch.object(OwnedStdioClient, 'request', new=request), patch.object(loop, 'call_later', wraps=loop.call_later) as scheduled:
            await self.deliver('blocked', 'task that cannot start')
            await self.settle()
            blocked = self.fixture.owner.db.execute("SELECT id FROM c_work WHERE seat='s' AND delivery='READY'").fetchone()[0]
            # Queue while the original is waiting. On the final failure this must
            # run without another incoming platform message or manual wake.
            next_op = self.fixture.box.receive('s', 'r', 'peer', 'next', 'next independent work')['work']
            for retry in range(3):
                self.fixture.owner.db.execute('UPDATE c_retry_wait SET not_before=0 WHERE operation=?', (blocked,))
                self.adapter._wake()
                await self.settle()
                if retry == 1:
                    row = f.box.read_work(blocked)
                    self.assertEqual((row['state'], row['delivery']), ('QUEUED', 'READY'))
                    self.assertEqual(json.loads(row['result'])['failures'], 3)
                    self.assertEqual(notifications, [])
        waits = [call.args[0] for call in scheduled.call_args_list if len(call.args) > 1 and call.args[1] == self.adapter._wake]
        self.assertEqual(waits, [60, 60, 60])
        self.assertEqual(failures, 4)
        self.assertEqual(json.loads(f.box.read_work(blocked)['result'])['failures'], 4)
        self.assertEqual(self.fixture.box.read_work(blocked)['state'], 'FAILED')
        self.assertEqual(self.fixture.box.read_work(next_op)['state'], 'SUCCEEDED')
        self.assertEqual(notifications, ['wake', 'wake'])  # finally also wakes; only one durable notice
        self.assertEqual(f.owner.db.execute("SELECT COUNT(*) FROM c_event WHERE kind='COORDINATOR_BLOCKER_QUEUED'").fetchone()[0], 1)
        row = self.fixture.owner.db.execute("SELECT * FROM c_work WHERE input LIKE '%peer_blocker%'").fetchone()
        self.assertIsNotNone(row)
        self.assertTrue(json.loads(row['input'])['internal_evidence'])
        await asyncio.wait_for(coordinator.worker, 8)
        self.assertEqual(f.box.read_work(row['id'])['state'], 'SUCCEEDED')
        accepted = [json.loads(r[0]) for r in f.owner.db.execute("SELECT body FROM c_event WHERE operation=? AND kind='TURN_ACCEPTED'", (row['id'],))]
        self.assertEqual(len(accepted), 1)
        self.assertEqual(self.fixture.owner.db.execute('SELECT COUNT(*) FROM c_outbox').fetchone()[0], 0)

    async def test_provider_first_dispatch_real_sdk_backoff_and_child_start(self):
        await self.provider_dispatch('serverOverloaded')

    async def test_provider_http_null_real_sdk_backoff_and_child_start(self):
        await self.provider_dispatch({'httpConnectionFailed': {'httpStatusCode': None}})

    async def test_provider_http_503_real_sdk_backoff_and_child_start(self):
        await self.provider_dispatch({'httpConnectionFailed': {'httpStatusCode': 503}})

    async def test_provider_failed_attempts_null_real_sdk_backoff_and_child_start(self):
        await self.provider_dispatch({'responseTooManyFailedAttempts': {'httpStatusCode': None}})

    async def test_provider_failed_attempts_503_real_sdk_backoff_and_child_start(self):
        await self.provider_dispatch({'responseTooManyFailedAttempts': {'httpStatusCode': 503}})

    async def test_provider_policy_refusal_real_sdk_never_creates_retry_or_fresh_thread(self):
        await self.provider_dispatch('cyberPolicy', refused=True)

    async def test_provider_object_policy_real_sdk_never_creates_retry_or_fresh_thread(self):
        await self.provider_dispatch({'cyberPolicy': {}}, refused=True)

    async def test_effectful_refusal_still_one_blocker_and_no_recovery(self):
        await self.provider_dispatch('cyberPolicy', refused=True, effects=True)

    async def provider_dispatch(self, info, *, refused=False, effects=False):
        from darkharness.integration.provider_recovery import prepare_settled_provider_recovery, register_policy
        from darkharness.integration.mailbox import encode, digest
        from datetime import datetime, timezone
        from band.core.types import PlatformMessage
        from band.integrations.codex.types import CodexSessionState
        f = self.fixture
        grant = json.loads(f.owner.db.execute("SELECT body FROM controls WHERE kind='grant'").fetchone()[0])
        from band.runtime.formatters import replace_uuid_mentions
        roster = [{'id': '00000000-0000-4000-8000-000000000003', 'handle': 'synthetic-builder', 'name': 'Synthetic Builder', 'type': 'Agent'}]
        content = replace_uuid_mentions('@[[00000000-0000-4000-8000-000000000003]] original provider task', roster)
        if refused:
            from darkharness.integration.codex_timeout import prepare_settled_timeout_recovery
            grant['scope'] = prepare_settled_timeout_recovery(grant['scope'], run_id='run', workspace=str(f.repo), rooms=['r'], seats=['s'])
            self.adapter.auto_recover_settled_timeouts = True
            self.adapter.coordinator_notify = lambda operation, attempt: f.box.notify_peer_blocker(operation, attempt, 'peer', 'r', 'run')
        grant['scope'] = prepare_settled_provider_recovery(grant['scope'], run_id='run', workspace=str(f.repo), rooms=['r'], seats=['s'], dispatch_sender='human', dispatch_room='r', dispatch_sha256=digest(content.encode('utf-8')))
        f.owner.db.execute("UPDATE controls SET body=? WHERE kind='grant'", (encode(grant),))
        register_policy(f.box, self.adapter.router)
        self.adapter.thread_ownership.binding = 'fixture-settings'
        body = encode({'room': 'r', 'seats': [{'alias': 's', 'settings_sha256': 'fixture-settings'}]})
        f.owner.db.execute('CREATE TABLE c_run_settings(run_id TEXT PRIMARY KEY,body TEXT,hash TEXT)')
        f.owner.db.execute('INSERT INTO c_run_settings VALUES(?,?,?)', ('run', body, digest(body.encode())))
        with f.owner.transaction(f.owner.epoch) as db:
            f.box.event(db, None, 'RUNTIME_BINDINGS', {'run_id': 'run', 'bindings': [{'settings_sha256': 'fixture-settings', 'runtime': 'codex', 'workspace': str(f.repo), 'model': 'test-model', 'effort': 'high'}]})
        error = {'message': 'fixture provider failure', 'codexErrorInfo': info}
        source = wiring.RPC_SERVER.replace("if args and not marker.exists():", "if not marker.exists():\n   marker.touch()\n   error=" + repr(error) + "\n   print(json.dumps({'method':'error','params':{'threadId':thread,'turnId':'turn','error':error,'willRetry':False}}),flush=True)\n   print(json.dumps({'method':'turn/completed','params':{'threadId':thread,'turn':{'id':'turn','status':'failed','error':error,'items':[]}}}),flush=True)\n  elif args and not marker.exists():")
        if effects:
            # Anonymous fixture command executes locally. It is not a model or a live safety test.
            source = source.replace('   error=', "   import subprocess\n   subprocess.run([sys.executable,'-c','pass'],check=True)\n   item={'id':'command-fixture','type':'commandExecution','command':'synthetic command','status':'completed','exitCode':0}\n   print(json.dumps({'method':'item/completed','params':{'threadId':thread,'turnId':'turn','item':item}}),flush=True)\n   print(json.dumps({'method':'safetyBuffering','params':{'threadId':thread,'turnId':'turn'}}),flush=True)\n   error=")
            original_start = self.adapter._start_turn
            async def start_with_ack(params):
                response = await original_start(params)
                operation, attempt = self.adapter.current.get()
                f.box.prepare_send('synthetic-ack', operation, {'content':'fixture acknowledged message'})
                f.box.sent('synthetic-ack', {'id':'synthetic-message-id'})
                return response
            self.adapter._start_turn = start_with_ack
        before = tuple(f.owner.db.execute("SELECT body,revision FROM controls WHERE kind='grant'").fetchone())
        with patch.object(wiring, 'RPC_SERVER', source):
            msg = PlatformMessage('dispatch', 'r', content, 'human', 'User', 'fixture human', 'text', {}, datetime.now(timezone.utc))
            await self.adapter.on_message(msg, self.tools, CodexSessionState(), None, None, is_session_bootstrap=True, room_id='r')
            await self.settle()
            rows = f.owner.db.execute("SELECT * FROM c_work WHERE seat='s' ORDER BY rowid").fetchall()
            if refused:
                self.assertEqual(len(rows), 2)  # initial completed fixture + fenced parent only
                self.assertEqual(rows[-1]['delivery'], 'DELIVERY_UNKNOWN')
                self.assertEqual(f.owner.db.execute('SELECT COUNT(*) FROM c_provider_retry').fetchone()[0], 0)
                self.assertTrue(self.events('PROVIDER_RECOVERY_BLOCKED'))
                self.assertIsNone(f.box.next_ready('s'))
                def counts():
                    db = f.owner.db
                    return (db.execute('SELECT COUNT(*) FROM c_recovery').fetchone()[0],
                            db.execute("SELECT COUNT(*) FROM c_work WHERE input LIKE '%peer_blocker%'").fetchone()[0],
                            db.execute("SELECT COUNT(*) FROM c_event WHERE kind='COORDINATOR_BLOCKER_QUEUED'").fetchone()[0],
                            len(self.events('SETTLED_NATIVE_PROVIDER')),
                            len(self.events('PROVIDER_RETRY_SCHEDULED')))
                self.assertEqual(counts(), (0,1,1,0,0))
                # A manual wake must not create a fresh thread or hide refusal.
                self.adapter._wake()
                await self.settle()
                starts = [e for e in self.events('STDIN_RPC') if e['payload'].get('method') == 'thread/start']
                self.assertEqual(len(starts), 1)
                self.assertEqual(len(self.events('TURN_ACCEPTED')), 1)
                self.assertEqual(counts(), (0,1,1,0,0))
                self.assertEqual(before, tuple(f.owner.db.execute("SELECT body,revision FROM controls WHERE kind='grant'").fetchone()))
                self.assertEqual(f.owner.db.execute('SELECT COUNT(*) FROM c_outbox').fetchone()[0], int(effects))
                if effects:
                    self.assertEqual(f.owner.db.execute('SELECT state FROM c_outbox').fetchone()[0], 'ACKED')
                    methods = [e['payload'].get('method') for e in self.events('STDOUT_RPC')]
                    self.assertIn('safetyBuffering', methods)
                    self.assertIn('item/completed', methods)
                return
            self.assertEqual(len(rows), 3, self.events('PROVIDER_RECOVERY_BLOCKED'))  # initial completed fixture + parent + child
            parent, child = rows[-2:]
            self.assertEqual(parent['delivery'], 'RECONCILED')
            self.assertEqual(child['delivery'], 'READY')
            self.assertGreater(f.owner.db.execute('SELECT not_before FROM c_retry_wait WHERE operation=?', (child['id'],)).fetchone()[0], time.time() + 55)
            self.assertIsNone(f.box.next_ready('s'))
            self.assertEqual(len(self.events('TURN_ACCEPTED')), 1)
            f.owner.db.execute('UPDATE c_retry_wait SET not_before=0 WHERE operation=?', (child['id'],))
            self.adapter._wake()
            await self.settle()
        self.assertEqual(f.box.read_work(child['id'])['state'], 'SUCCEEDED')
        self.assertEqual(len(self.events('TURN_ACCEPTED')), 2)
        self.assertEqual(parent['thread'], f.box.read_work(child['id'])['thread'])
        self.assertEqual(before, tuple(f.owner.db.execute("SELECT body,revision FROM controls WHERE kind='grant'").fetchone()))
        self.assertEqual(f.owner.db.execute('SELECT COUNT(*) FROM c_outbox').fetchone()[0], 0)

    async def test_grant_failure_before_intent(self):
        with patch.object(self.adapter.router, 'active', side_effect=[True, False]):
            await self.check_requeued()
        self.assertFalse(self.events('TURN_START_INTENT'))

    async def test_thread_binding_failure_before_intent(self):
        with patch.object(self.adapter.thread_ownership, 'bind', side_effect=IntegrationError('fixture binding')):
            await self.check_requeued()
        self.assertFalse(self.events('TURN_START_INTENT'))

    async def test_thread_start_rpc_error_before_intent(self):
        source = wiring.RPC_SERVER.replace("if 'id' in p and m:", "if m=='thread/start':\n  print(json.dumps({'id':p['id'],'error':{'code':-32602,'message':'fixture'}}),flush=True); continue\n if 'id' in p and m:")
        with patch.object(wiring, 'RPC_SERVER', source):
            await self.check_requeued()

    async def test_turn_start_definitive_rpc_error(self):
        source = wiring.RPC_SERVER.replace("if 'id' in p and m:", "if m=='turn/start':\n  print(json.dumps({'id':p['id'],'error':{'code':-32602,'message':'fixture'}}),flush=True); continue\n if 'id' in p and m:")
        with patch.object(wiring, 'RPC_SERVER', source):
            await self.check_requeued()
        self.assertEqual(len(self.events('TURN_START_INTENT')), 1)

    async def test_schema_snapshot_does_not_replace_owned_thread_on_scope_flicker(self):
        await self.deliver('first', 'first independent task')
        await self.settle()
        first_thread = self.adapter.thread_ownership.latest()['thread']
        schemas = self.adapter.verification.schemas()
        self.assertTrue(schemas)
        with patch.object(self.adapter.router, '_scope', return_value=None):
            self.assertEqual(self.adapter.verification.schemas(), schemas)
        await self.deliver('second', 'second independent task')
        await self.settle()
        self.assertEqual(self.adapter.thread_ownership.latest()['thread'], first_thread)
        frames = [e['payload'] for e in self.events('STDIN_RPC')]
        self.assertEqual(sum(f.get('method') == 'thread/start' for f in frames), 1)
        self.assertTrue(any(f.get('method') == 'thread/resume' for f in frames))
        self.assertEqual(self.fixture.owner.db.execute('SELECT COUNT(*) FROM c_outbox').fetchone()[0], 0)

    async def test_preclaim_inactive_does_not_claim(self):
        with patch.object(self.adapter.router, 'active', return_value=False):
            await self.deliver('inactive', 'original task')
            await self.settle()
        row = self.fixture.owner.db.execute("SELECT attempt,delivery FROM c_work WHERE state='QUEUED'").fetchone()
        self.assertEqual(tuple(row), (None, 'READY'))

    async def test_resume_runtime_error_before_intent_and_next_wake_success(self):
        from darkharness.integration.codex import OwnedStdioClient
        await self.deliver('initial', 'original completed task')
        await self.settle()
        prior_accepted = len(self.events('TURN_ACCEPTED'))
        original = OwnedStdioClient.request
        async def request(client, method, params=None, **kwargs):
            if method == 'thread/resume':
                raise RuntimeError('fixture resume')
            return await original(client, method, params, **kwargs)
        with patch.object(OwnedStdioClient, 'request', new=request):
            await self.deliver('resume-failure', 'original next task')
            await self.settle()
        row = self.fixture.owner.db.execute("SELECT * FROM c_work WHERE delivery='READY'").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(len(self.events('TURN_ACCEPTED')), prior_accepted)
        self.fixture.owner.db.execute('UPDATE c_retry_wait SET not_before=0 WHERE operation=?', (row['id'],))
        self.adapter._wake()
        await self.settle()
        self.assertEqual(self.fixture.box.read_work(row['id'])['delivery'], 'RETURNED')
        self.assertEqual(self.fixture.box.read_work(row['id'])['state'], 'SUCCEEDED')
