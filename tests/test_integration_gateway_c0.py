"""C0 synthetic mixed-backend regressions; no model or Band participation."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from darkharness.core import Store
from darkharness.integration.gateway import IntegrationService, IntegrationSession
from darkharness.integration.launch import SeatManager
from darkharness.ipc import envelope
from test_integration_artifacts import OFFICIAL


@unittest.skipUnless(sys.platform == 'linux', 'Linux owner/proc component test')
class MixedBackendGatewayTests(unittest.TestCase):
    def recovery_call(self, action):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / 'result'
            subprocess.run(['git', 'init', '-q', str(repo)], check=True)
            (repo / 'base').write_text('synthetic source')
            subprocess.run(['git', '-C', str(repo), 'add', 'base'], check=True)
            subprocess.run(['git', '-C', str(repo), '-c', 'user.name=fixture',
                            '-c', 'user.email=fixture@actors.invalid',
                            'commit', '-qm', 'base'], check=True)
            store = Store(str(Path(td) / 'state'))
            service = IntegrationService(store, 1024 * 1024, 16384, OFFICIAL, sys.executable)
            try:
                box = service.manager.mailbox
                op = box.receive('codex-seat', 'fixture-room', 'peer', 'original', 'full original task')['work']
                box.claim(op, 'attempt')
                box.update(op, 'attempt', state='FAILED', delivery='RETURNED', thread='original-thread')
                session = IntegrationSession(service)
                session.hello = True
                count = 0

                def call(name, payload, revision=None):
                    nonlocal count
                    count += 1
                    req = envelope(name, 'c0-' + str(count), payload=payload,
                                   environment_id=service.environment_id, expected_revision=revision)
                    req['operation_id'] = op
                    with service.mutex:
                        return session.handle(req)

                grant = call('grant.record', {'id': 'fixture-grant', 'source': 'synthetic controller',
                    'end_condition': 'STOP/revoke or run completion', 'scope': {
                        'run_id': 'fixture-run', 'workspace': str(repo), 'seats': ['codex-seat'],
                        'rooms': ['fixture-room'], 'continuation': {'operations': [op]}}}, 0)
                self.assertEqual(grant['execution_status'], 'SUCCEEDED')
                payload = {'attempt': 'attempt', 'grant_id': 'fixture-grant'}
                if action == 'integration.continue':
                    # Establish proof before adding the second backend so RED
                    # independently reaches resume's quiescence callback.
                    proof = call('integration.recovery.observe', payload)
                    self.assertEqual(proof['execution_status'], 'SUCCEEDED')
                    payload['evidence_id'] = proof['data']['evidence_id']
                native = SimpleNamespace(alias='codex-seat', worker=None, stopping=True, _room_clients={})
                fake = SimpleNamespace(alias='fake-seat', worker=None, stopping=True)
                self.assertFalse(hasattr(fake, '_room_clients'))
                service.manager.adapters = [native, fake]
                result = call(action, payload)
                self.assertEqual(result['execution_status'], 'SUCCEEDED')
                if action == 'integration.continue':
                    child = box.read_work(result['data']['id'])
                    self.assertEqual(child['thread'], 'original-thread')
                    self.assertEqual(json.loads(child['input'])['content'], 'full original task')
                    self.assertEqual(store.db.execute('SELECT COUNT(*) FROM c_inbox').fetchone()[0], 1)
                else:
                    self.assertIn('evidence_id', result['data'])
                self.assertEqual(call('integration.status', {})['execution_status'], 'SUCCEEDED')
            finally:
                # Synthetic adapters have no live lifecycle to stop.
                service.manager.adapters = []
                service.close()
                store.close()

    def test_observe_with_idle_adapter_without_room_clients(self):
        self.recovery_call('integration.recovery.observe')

    def test_continue_with_idle_adapter_without_room_clients(self):
        self.recovery_call('integration.continue')

    def test_startup_missing_flag_defaults_off_and_explicit_flag_preserved(self):
        manager = SeatManager.__new__(SeatManager)
        missing = SimpleNamespace(worker=None)
        disabled = SimpleNamespace(auto_recover_settled_timeouts=False)
        enabled = SimpleNamespace(auto_recover_settled_timeouts=True, router=object(), git_broker=object())
        manager.adapters = [missing, disabled, enabled]
        with tempfile.TemporaryDirectory() as td:
            store = Store(td)
            try:
                manager.owner = store
                manager.mailbox = object()
                with patch('darkharness.integration.codex_timeout.CodexTimeoutRecovery') as recovery:
                    recovery.return_value.automatic_allowed.return_value = False
                    self.assertEqual(manager.recover_timeouts_on_startup(), [])
                    recovery.assert_called_once_with(manager.mailbox, enabled.router, enabled.git_broker)
                    recovery.return_value.recover.assert_not_called()
            finally:
                store.close()

    def test_idle_pid_exemption_keeps_native_attempt_and_worker_fences(self):
        from darkharness.integration.codex import OwnedStdioClient
        manager = SeatManager.__new__(SeatManager)

        def client(context, group):
            value = OwnedStdioClient.__new__(OwnedStdioClient)
            value.evidence = SimpleNamespace(context=context)
            value.group = group
            return SimpleNamespace(client=value)

        manager.adapters = [
            SimpleNamespace(worker=None),
            SimpleNamespace(worker=SimpleNamespace(done=lambda: False), _room_clients={'active': client(None, 2)}),
            SimpleNamespace(worker=None, _room_clients={'idle': client(None, 1), 'bound': client(object(), 3),
                                                     'ungrouped': client(None, None), 'foreign': SimpleNamespace(client=object())}),
        ]
        with patch('darkharness.integration.codex.group_members', return_value=[(101, 'boot', 'start')]) as members:
            self.assertEqual(manager.idle_client_pids(), {101})
            members.assert_called_once_with(1)
