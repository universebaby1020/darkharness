"""Synthetic SDK4 boundary tests; no model, credential, or Band network calls."""
import json
import asyncio
import unittest
from unittest.mock import AsyncMock

import test_integration_codex as fixture


@unittest.skipUnless(fixture.HAS_SDK, 'installed Band SDK4 required')
class EventDeliveryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.CodexTests.asyncSetUp
    asyncTearDown = fixture.CodexTests.asyncTearDown
    make_adapter = fixture.CodexTests.make_adapter
    message = fixture.CodexTests.message
    deliver = fixture.CodexTests.deliver
    settle = fixture.CodexTests.settle

    async def test_serialized_metadata_cap_rejects_before_external_effect(self):
        from darkharness.integration.protected_tools import GuardedTools, LocalSendRejected
        op = self.box.receive('s', 'r', 'peer', 'oversize', 'task')['work']
        self.box.claim(op, 'a')
        tools = GuardedTools(self.tools, self.adapter, op, 'a')
        # SDK4 ChatEventRequest declares 65536 serialized bytes, not value bytes.
        for value in ('x' * 65536, '한' * 22000, '\\' * 33000):
            self.assertGreater(len(json.dumps({'diff': value}, ensure_ascii=False, separators=(',', ':')).encode()), 65536)
            with self.assertRaises(LocalSendRejected):
                await tools.send_event('synthetic diff', 'task', {'diff': value})
        self.assertEqual(self.tools.events, [])
        self.assertEqual(self.owner.db.execute("SELECT COUNT(*) FROM c_outbox WHERE state='DELIVERY_UNKNOWN'").fetchone()[0], 0)
        self.assertEqual(self.owner.db.execute("SELECT COUNT(*) FROM c_outbox WHERE state='REJECTED'").fetchone()[0], 3)

    async def test_sdk_diff_oversize_does_not_block_next_real_drain_turn(self):
        # Actual SDK forwards the diff to a protected send_event. The fake Band
        # enforces its documented size contract, and records any crossing.
        crossed = []
        async def send_event(content, message_type, metadata=None):
            crossed.append(metadata)
            if metadata and len(json.dumps(metadata, ensure_ascii=False, separators=(',', ':')).encode()) > 65536:
                raise ValueError('synthetic server rejects oversized metadata')
            return {'id': 'synthetic-event', 'success': True}
        self.tools.send_event = send_event
        from darkharness.integration.protected_tools import GuardedTools
        op = self.box.receive('s', 'r', 'peer', 'first', 'first synthetic task')['work']
        self.box.claim(op, 'a')
        # Use the actual SDK diff forwarder, whose best-effort event failure is
        # swallowed while the native turn can still finish successfully.
        await self.adapter._forward_diff_event(tools=GuardedTools(self.tools,self.adapter,op,'a'),
            params={'diff': 'x' * 70000},room_id='r',thread_id='session',turn_id='1')
        self.box.update(op,'a',state='SUCCEEDED',delivery='RETURNED')
        await self.deliver(self.message('second', 'second synthetic task'))
        await self.settle()
        self.assertEqual(self.client.turns, 1, 'next queued work must actually start')
        self.assertEqual(self.owner.db.execute("SELECT COUNT(*) FROM c_work WHERE state='SUCCEEDED'").fetchone()[0], 2)
        self.assertEqual(self.owner.db.execute("SELECT COUNT(*) FROM c_outbox WHERE state='REJECTED'").fetchone()[0], 1)
        self.assertFalse(any(m and len(json.dumps(m, ensure_ascii=False, separators=(',', ':')).encode()) > 65536 for m in crossed))
        self.assertEqual(self.owner.db.execute("SELECT COUNT(*) FROM c_outbox WHERE state='DELIVERY_UNKNOWN'").fetchone()[0], 0)

    async def test_unknown_diagnostic_keeps_exception_class_status_and_phase_only(self):
        from darkharness.integration.protected_tools import GuardedTools
        import httpx
        op = self.box.receive('s', 'r', 'peer', 'network', 'task')['work']; self.box.claim(op, 'a')
        exc = httpx.HTTPStatusError('sensitive-body-must-not-be-recorded', request=httpx.Request('POST','https://example.invalid/events'), response=httpx.Response(503))
        self.tools.send_event = AsyncMock(side_effect=exc)
        with self.assertRaises(httpx.HTTPStatusError):
            await GuardedTools(self.tools, self.adapter, op, 'a').send_event('safe', 'task')
        row = json.loads(self.owner.db.execute("SELECT body FROM c_event WHERE kind='DELIVERY_UNKNOWN'").fetchone()[0])['data']
        self.assertEqual(row['exception_class'], 'HTTPStatusError')
        self.assertEqual(row['http_status'], 503)
        self.assertEqual(row['phase'], 'RAW_SEND')
        self.assertNotIn('sensitive-body', json.dumps(row))
        self.assertEqual(self.owner.db.execute('SELECT state FROM c_outbox').fetchone()[0], 'DELIVERY_UNKNOWN')

    async def test_exact_serialized_limit_is_allowed_and_one_byte_over_is_not(self):
        from darkharness.integration.protected_tools import GuardedTools, LocalSendRejected
        op=self.box.receive('s','r','peer','boundary','task')['work'];self.box.claim(op,'a')
        tools=GuardedTools(self.tools,self.adapter,op,'a')
        overhead=len(json.dumps({'diff':''},separators=(',',':')).encode())
        await tools.send_event('exact','task',{'diff':'x'*(65536-overhead)})
        with self.assertRaises(LocalSendRejected):
            await tools.send_event('over','task',{'diff':'x'*(65537-overhead)})
        self.assertEqual(len(self.tools.events),1)
        self.assertEqual(self.owner.db.execute("SELECT COUNT(*) FROM c_outbox WHERE state='ACKED'").fetchone()[0],1)

    async def test_stored_then_response_lost_stays_fenced_without_readback_authority(self):
        from darkharness.integration.protected_tools import GuardedTools
        op=self.box.receive('s','r','peer','lost','task')['work'];self.box.claim(op,'a')
        stored=[]
        async def lost(**body):
            stored.append(body)
            raise TimeoutError('synthetic lost response')
        self.tools.send_event=lost
        with self.assertRaises(TimeoutError):
            await GuardedTools(self.tools,self.adapter,op,'a').send_event('synthetic','task')
        self.box.update(op,'a',state='SUCCEEDED',delivery='RETURNED')
        queued=self.box.receive('s','r','peer','next','next task')['work']
        self.assertEqual(len(stored),1)
        self.assertIsNone(self.box.next_ready('s'))
        self.assertEqual(self.box.read_work(queued)['delivery'],'READY')
        self.assertEqual(self.owner.db.execute('SELECT state FROM c_outbox').fetchone()[0],'DELIVERY_UNKNOWN')

    async def test_post_send_failures_record_exact_phase_and_keep_fence(self):
        from darkharness.integration.protected_tools import GuardedTools
        from darkharness.integration.mailbox import IntegrationError
        from unittest.mock import patch
        import sqlite3
        op=self.box.receive('s','r','peer','phases','task')['work'];self.box.claim(op,'a')
        tools=GuardedTools(self.tools,self.adapter,op,'a')
        self.tools.send_event=AsyncMock(return_value=None)
        with self.assertRaisesRegex(IntegrationError,'SEND_ACK_MISSING'):
            await tools.send_event('missing','task')
        self.tools.send_event=AsyncMock(return_value={'secret':'sk-'+'x'*20})
        with self.assertRaisesRegex(IntegrationError,'OUTBOX_SECRET_BLOCKED'):
            await tools.send_event('guarded','task')
        self.tools.send_event=AsyncMock(return_value={'id':'synthetic-ack'})
        with patch.object(self.box,'sent',side_effect=sqlite3.OperationalError('synthetic lock')):
            with self.assertRaises(sqlite3.OperationalError):
                await tools.send_event('db','task')
        self.tools.send_event=AsyncMock(side_effect=asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            await tools.send_event('cancel','task')
        rows=[json.loads(r[0])['data'] for r in self.owner.db.execute("SELECT body FROM c_event WHERE kind='DELIVERY_UNKNOWN' ORDER BY seq")]
        self.assertEqual([r['phase'] for r in rows],['RECEIPT_VALIDATE','RECEIPT_GUARD','MAILBOX_SENT','RAW_SEND'])
        self.assertEqual(rows[-1]['exception_class'],'CancelledError')
        self.assertEqual(self.owner.db.execute("SELECT COUNT(*) FROM c_outbox WHERE state='DELIVERY_UNKNOWN'").fetchone()[0],4)


if __name__ == '__main__':
    unittest.main()
