"""Focused offline configuration/factory tests. No credentials or providers."""
import asyncio
from copy import deepcopy
from dataclasses import replace
from importlib.util import find_spec
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from darkharness.integration import launch
from darkharness.integration.artifacts import SecretGuard, render_mandate
from darkharness.integration.mailbox import IntegrationError, Mailbox
from darkharness.integration.policy import ApprovalRouter
from darkharness.integration.thread_ownership import ThreadOwnership
from test_integration_mailbox import Owner

HAS_SDK = find_spec('band') is not None
HAS_CLAUDE = HAS_SDK and find_spec('claude_agent_sdk') is not None and find_spec('mcp') is not None


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.repo = self.root/'workspace'
        self.repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        self.config = {'run_id': 'fixture-run', 'room_id': 'fixture-room', 'workspace': str(self.repo),
            'credentials_path': str(self.root/'absent-agents.json'), 'grant_id': 'fixture-grant',
            'model': 'test-codex', 'effort': 'high', 'codex_command': ['/usr/bin/true', 'app-server', '--listen', 'stdio://'],
            'runtime_env': {'CODEX_HOME': str(self.root/'external-codex-home')},
            'seats': [{'alias': 'seat-'+role, 'display_name': 'fixture '+role, 'role': role,
                       'participant_id': 'participant-'+role} for role in ('coordinator','builder','reviewer')]}

    def tearDown(self):
        self.tmp.cleanup()

    def profiles(self):
        cfg = deepcopy(self.config)
        cfg['connections'] = {'legacy': {'runtime':'codex','command':cfg['codex_command'], 'runtime_env':cfg['runtime_env']}}
        cfg['default_connection'] = 'legacy'
        return cfg

    def test_legacy_default_and_origin_equivalence(self):
        implicit = launch.resolve_settings(self.config)
        explicit = self.profiles()
        explicit['turn_timeout_s'] = 3600
        for seat in explicit['seats']:
            seat.update(connection='legacy', model='test-codex', effort='high', turn_timeout_s=3600)
        actual = launch.resolve_settings(explicit)
        self.assertEqual([s.fingerprint for s in implicit], [s.fingerprint for s in actual])
        self.assertNotEqual(implicit[0].sources, actual[0].sources)
        self.assertEqual(launch.validate_config(self.config)['effective_timeout_s'], 3600)
        self.assertFalse(Path(self.config['credentials_path']).exists())
        with self.assertRaises(Exception):
            implicit[0].model = 'mutated'

    def test_three_independent_bindings_and_precedence(self):
        cfg = self.profiles()
        cfg['turn_timeout_s'] = 7200
        cfg['connections'].update({
            'other-codex': {'runtime':'codex', 'command':['/usr/bin/true','app-server'], 'runtime_env':{'CODEX_HOME':str(self.root/'other-auth')}, 'model':'test-review', 'effort':'low', 'turn_timeout_s':900},
            'claude-native': {'runtime':'claude_code', 'command':['/usr/bin/true'], 'runtime_env':{'CLAUDE_CONFIG_DIR':str(self.root/'claude-auth')}, 'model':'test-claude', 'effort':'medium', 'turn_timeout_s':600}})
        cfg['seats'][0].update(model='test-coordinate',effort='xhigh',turn_timeout_s=3600)
        cfg['seats'][1].update(connection='claude-native',turn_timeout_s=450.5)
        cfg['seats'][2].update(connection='other-codex')
        result = launch.resolve_settings(cfg)
        self.assertEqual([(s.connection,s.runtime,s.model,s.effort,s.turn_timeout_s) for s in result],
            [('legacy','codex','test-coordinate','xhigh',3600),('claude-native','claude_code','test-claude','medium',450.5),('other-codex','codex','test-review','low',900)])
        self.assertEqual(len({s.fingerprint for s in result}),3)
        cfg['seats'][0]['role'],cfg['seats'][2]['role'] = cfg['seats'][2]['role'],cfg['seats'][0]['role']
        self.assertEqual([s.fingerprint for s in result], [s.fingerprint for s in launch.resolve_settings(cfg)])
        cfg['seats'][0].pop('turn_timeout_s')
        self.assertEqual(launch.resolve_settings(cfg)[0].turn_timeout_s,7200)

    def test_run_pin_survives_manager_restart_and_ignores_origins(self):
        owner=Owner()
        try:
            manager=launch.SeatManager(owner,'unused')
            first=manager.pin_settings(self.config,launch.resolve_settings(self.config))
            cfg=self.profiles();cfg['turn_timeout_s']=3600
            restarted=launch.SeatManager(owner,'unused')
            self.assertEqual(first,restarted.pin_settings(cfg,launch.resolve_settings(cfg)))
            cfg['seats'][1]['model']='changed-model'
            with self.assertRaisesRegex(IntegrationError,'RUN_SETTINGS_PIN_MISMATCH'):
                restarted.pin_settings(cfg,launch.resolve_settings(cfg))
            cfg['run_id']='new-configured-run'
            self.assertNotEqual(first,restarted.pin_settings(cfg,launch.resolve_settings(cfg)))
            body=owner.db.execute('SELECT body FROM c_run_settings LIMIT 1').fetchone()[0]
            self.assertNotIn('external-codex-home',body);self.assertNotIn('/usr/bin/true',body)
        finally:owner.db.close()

    def test_timeout_each_level_positive_finite_no_upper_policy_cap(self):
        for location in ('run','connection','seat'):
            for value in (0.25,1,7200.5,10**20):
                cfg=self.profiles(); target=cfg if location=='run' else cfg['connections']['legacy'] if location=='connection' else cfg['seats'][0]
                target['turn_timeout_s']=value
                self.assertEqual(launch.resolve_settings(cfg)[0].turn_timeout_s,value)
            for value in (True,False,float('nan'),float('inf'),-float('inf'),0,-1,'3600',None):
                cfg=self.profiles(); target=cfg if location=='run' else cfg['connections']['legacy'] if location=='connection' else cfg['seats'][0]
                target['turn_timeout_s']=value
                with self.assertRaisesRegex(IntegrationError,'TIMEOUT_INVALID'):
                    launch.validate_config(cfg)

    def test_precise_unknown_missing_and_runtime_mismatch(self):
        cases=[]
        cfg=self.profiles();cfg['seats'][0]['connection']='absent';cases.append((cfg,'UNKNOWN_SEAT_CONNECTION:seat-coordinator'))
        cfg=self.profiles();cfg.pop('default_connection');cases.append((cfg,'SEAT_CONNECTION_REQUIRED:seat-coordinator'))
        cfg=self.profiles();cfg['connections']['legacy']['runtime']='unregistered';cases.append((cfg,'UNSUPPORTED_RUNTIME:connection.legacy'))
        cfg=self.profiles();cfg['seats'][0]['model_typo']='test';cases.append((cfg,'CONFIG_UNKNOWN_FIELD:seat:model_typo'))
        cfg=self.profiles();cfg['connections']['cc']={'runtime':'claude_code','command':['/usr/bin/true']};cfg['seats'][1]['connection']='cc';cases.append((cfg,'RUNTIME_DEFAULT_MISMATCH:seat-builder:model'))
        cfg=self.profiles();cfg.pop('model');cases.append((cfg,'SEAT_SETTING_REQUIRED:seat-coordinator:model'))
        cfg=self.profiles();cfg['connections']['legacy']['command']=[];cases.append((cfg,'CONNECTION_COMMAND_REQUIRED:legacy'))
        for cfg,code in cases:
            with self.assertRaisesRegex(IntegrationError,code):launch.validate_config(cfg)

    def test_external_references_and_safe_effective_cli_output(self):
        result=launch.validate_config(self.config)
        output=json.dumps(result)+repr(launch.resolve_settings(self.config)[0])
        self.assertNotIn(str(self.root/'external-codex-home'),output)
        self.assertNotIn('/usr/bin/true',output)
        self.assertNotIn('absent-agents.json',output)
        cfg=deepcopy(self.config);cfg['runtime_env']['ANTHROPIC_API_KEY']='not-a-real-key'
        with self.assertRaisesRegex(IntegrationError,'CONNECTION_ENV_UNSUPPORTED'):launch.validate_config(cfg)
        cfg=deepcopy(self.config);cfg['runtime_env']['CODEX_HOME']=str(self.repo/'auth')
        with self.assertRaisesRegex(IntegrationError,'EXTERNAL_NATIVE_AUTH_HOME_REQUIRED'):launch.validate_config(cfg)
        for command in (['bash','-c','true'],['codex','app-server','--yolo'],['codex','app-server','-c','sandbox_mode="danger-full-access"']):
            cfg=deepcopy(self.config);cfg['codex_command']=command
            with self.assertRaises(IntegrationError):launch.validate_config(cfg)
        path=self.root/'config.json';path.write_text(json.dumps(self.config))
        completed=subprocess.run([sys.executable,'-B','-m','darkharness.integration','validate','--config',str(path)],capture_output=True,text=True)
        self.assertEqual(completed.returncode,0,completed.stderr)
        self.assertEqual(len(json.loads(completed.stdout)['effective_settings']),3)
        self.assertNotIn('external-codex-home',completed.stdout)

    @unittest.skipUnless(HAS_SDK,'Band SDK4 required')
    def test_native_codex_options_exact_and_no_ambient_config(self):
        settings=launch.resolve_settings(self.config)[0]
        with patch.dict(os.environ,{'CODEX_TRANSPORT':'ws','CODEX_SANDBOX_POLICY':'{"type":"dangerFullAccess"}','CODEX_SKILL_ROOTS':'["/ambient"]'}):
            cfg=launch.codex_sdk_config(settings,'fixture-room','exact header','fixture coordinator')
        self.assertEqual((cfg.model,cfg.reasoning_effort,cfg.turn_timeout_s),('test-codex','high',3600))
        self.assertEqual(cfg.codex_command,settings.command)
        self.assertEqual(cfg.codex_env['CODEX_HOME'],str(self.root/'external-codex-home'))
        self.assertEqual((cfg.transport,cfg.sandbox,cfg.approval_policy,cfg.system_prompt),('stdio','workspace-write','on-request','exact header'))
        self.assertIsNone(cfg.sandbox_policy);self.assertEqual(cfg.skill_roots,[])
        self.assertEqual(cfg.workspace_for_room('fixture-room'),str(self.repo))
        with self.assertRaisesRegex(IntegrationError,'ROOM_BINDING_DENIED'):cfg.workspace_for_room('foreign')

    @unittest.skipUnless(HAS_SDK,'Band SDK4 required')
    def test_legacy_structured_node_official_codex_entrypoint_and_identity(self):
        cfg=deepcopy(self.config)
        node=self.root/'native'/'bin'/'node';node.parent.mkdir(parents=True)
        node.write_text('fixture executable reference only');node.chmod(0o700)
        entry=self.root/'native-codex'/'node_modules'/'@openai'/'codex'/'bin'/'codex.js'
        entry.parent.mkdir(parents=True);entry.write_text('// anonymous official-layout fixture; never executed')
        package=entry.parent.parent/'package.json'
        package.write_text(json.dumps({'name':'@openai/codex','version':'0.159.3','bin':{'codex':'bin/codex.js'}}))
        command=[str(node),str(entry),'app-server','--listen','stdio://']
        cfg['codex_command']=command
        self.assertTrue(launch.validate_config(cfg)['valid'])
        settings=launch.resolve_settings(cfg)
        launch.preflight_backends(settings)
        self.assertEqual(launch.codex_sdk_config(settings[0],'fixture-room','exact header','fixture coordinator').codex_command,tuple(command))
        self.assertFalse(Path(cfg['credentials_path']).exists())
        for bad in ([str(node),str(self.root/'arbitrary.js'),'app-server'],[str(node),str(entry),'app-server','-c','sandbox=danger-full-access'],[str(node),'-e','process.exit()']):
            cfg['codex_command']=bad
            with self.assertRaisesRegex(IntegrationError,'CODEX_OFFICIAL_NODE_ENTRYPOINT_REQUIRED'):
                launch.validate_config(cfg)
        cfg['codex_command']=command
        package.write_text(json.dumps({'name':'untrusted-package','version':'0.159.3','bin':{'codex':'bin/codex.js'}}))
        with self.assertRaisesRegex(IntegrationError,'CODEX_NODE_PACKAGE_IDENTITY_UNSUPPORTED'):
            launch.preflight_backends(launch.resolve_settings(cfg))
        entry.unlink()
        with self.assertRaisesRegex(IntegrationError,'CODEX_NODE_ENTRYPOINT_UNAVAILABLE'):
            launch.preflight_backends(launch.resolve_settings(cfg))

    def test_missing_backend_rejected_before_credentials_room_or_mandates(self):
        cfg=self.profiles();cfg['connections']['legacy']['runtime']='claude_code';cfg['connections']['legacy']['command']=['/usr/bin/true']
        cfg['connections']['legacy'].update(model='test-claude',effort='high',runtime_env={})
        registration=launch.BACKENDS['claude_code']
        def missing(settings):raise IntegrationError('CLAUDE_BACKEND_DEPENDENCY_MISSING:fixture')
        with patch.dict(launch.BACKENDS,{'claude_code':replace(registration,preflight=missing)}):
            with self.assertRaisesRegex(IntegrationError,'DEPENDENCY_MISSING'):launch.prepare(cfg,'unused')
            self.assertFalse((self.repo/'mandates').exists())
            owner=Owner(); manager=launch.SeatManager(owner,'unused')
            try:
                with self.assertRaisesRegex(IntegrationError,'DEPENDENCY_MISSING'):asyncio.run(manager.start(cfg))
                self.assertIsNone(manager.config);self.assertEqual(manager.agents,[])
            finally:owner.db.close()

    @unittest.skipUnless(HAS_CLAUDE,'optional Claude test dependencies required')
    def test_real_mixed_factories_native_options_mandate_and_local_readiness(self):
        from darkharness.integration.codex import DurableCodexAdapter
        from darkharness.integration.claude import DurableClaudeAdapter
        cfg=self.profiles();cfg['connections']['cc']={'runtime':'claude_code','command':['/usr/bin/true'],'model':'test-claude','effort':'medium','turn_timeout_s':55,'runtime_env':{'CLAUDE_CONFIG_DIR':str(self.root/'cc-auth')}}
        cfg['seats'][1]['connection']='cc'
        settings=launch.resolve_settings(cfg)
        launch.preflight_backends(settings)  # local executable path/types only
        owner=Owner();box=Mailbox(owner);guard=SecretGuard([('fixture-pattern', r'\bsk-\w{16,}')],source_hash='fixture')
        adapters=[]
        try:
            for seat,effective in zip(cfg['seats'],settings):
                router=ApprovalRouter(owner,'fixture-grant',seat['alias'],cfg['room_id'],cfg['run_id'],self.repo)
                text=render_mandate(seat['display_name'],seat['role'],launch.BACKENDS[effective.runtime].harness,effective.model,effective.effort)
                adapter,runtime,binding=launch.BACKENDS[effective.runtime].factory(settings=effective,seat=seat,config=cfg,text=text,mailbox=box,router=router,guard=guard,coordinator='participant-coordinator')
                adapters.append(adapter)
                self.assertEqual(binding.settings_sha256,effective.fingerprint)
                self.assertIn(effective.model,text);self.assertIn(effective.effort,text)
                # Origins are evidence only, not execution identity or startup authority.
                source_only=replace(binding,settings_sources=(('model','explicit-seat'),))
                self.assertEqual(source_only,binding)
                asyncio.run(runtime.start(source_only))
                with self.assertRaisesRegex(IntegrationError,'RUNTIME_BINDING_MISMATCH'):
                    asyncio.run(runtime.start(replace(binding,model='wrong-model')))
                if effective.runtime=='claude_code':
                    self.assertIsInstance(adapter,DurableClaudeAdapter);self.assertNotIsInstance(adapter,DurableCodexAdapter)
                    self.assertEqual(adapter.config.cli.env['CLAUDE_CONFIG_DIR'],str(self.root/'cc-auth'))
                    self.assertIsNone(adapter.config.fallback_model)
                    ready=asyncio.run(runtime.readiness())
                    self.assertEqual(ready['authentication'],'NOT_PROBED');self.assertEqual(ready['execution_qualification'],'NOT_RUN')
                else:self.assertIsInstance(adapter,DurableCodexAdapter)
        finally:
            for adapter in adapters:adapter.verification.broker.close()
            owner.db.close()

    @unittest.skipUnless(HAS_SDK,'Band SDK4 required')
    def test_optional_imports_lazy_legacy_start_and_negative_preflight(self):
        script = '''
import asyncio, importlib.abc, json, sys
from pathlib import Path
class NoOptional(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'claude_agent_sdk','mcp'}:
            raise ModuleNotFoundError('optional dependency deliberately unavailable')
sys.meta_path.insert(0, NoOptional())
sys.path.insert(0, 'tests')
from test_integration_mailbox import Owner
from darkharness.integration import launch
from darkharness.integration.mailbox import Mailbox, IntegrationError
from darkharness.integration.policy import ApprovalRouter
from darkharness.integration.artifacts import SecretGuard, render_mandate
config=json.loads(Path(sys.argv[1]).read_text())
launch.validate_config(config)
settings=launch.resolve_settings(config)[0]
launch.preflight_backends((settings,))
owner=Owner();box=Mailbox(owner);seat=config['seats'][0]
router=ApprovalRouter(owner,'g',seat['alias'],config['room_id'],config['run_id'],config['workspace'])
guard=SecretGuard([('fixture', r'never-matches-fixture-pattern')],source_hash='fixture')
text=render_mandate(seat['display_name'],seat['role'],launch.BACKENDS['codex'].harness,settings.model,settings.effort)
a,r,b=launch.BACKENDS['codex'].factory(settings=settings,seat=seat,config=config,text=text,mailbox=box,router=router,guard=guard,coordinator='fixture-coordinator')
asyncio.run(r.start(b))
assert not any(k.split('.')[0] in {'claude_agent_sdk','mcp'} for k in sys.modules)
config['connections']={'cc':{'runtime':'claude_code','command':['/usr/bin/true'],'model':'test-claude','effort':'high'}}
config['default_connection']='cc'
try:
    launch.preflight_backends(launch.resolve_settings(config))
    raise AssertionError('missing optional dependency accepted')
except IntegrationError as exc:
    assert 'CLAUDE_BACKEND_DEPENDENCY_MISSING' in str(exc)
assert not Path(config['credentials_path']).exists()
a.verification.broker.close();owner.db.close()
print('legacy validate/preflight/factory/Runtime.start: OK; optional dependency: rejected before effects')
'''
        path=self.root/'negative.json';path.write_text(json.dumps(self.config))
        completed=subprocess.run([sys.executable,'-B','-c',script,str(path)],capture_output=True,text=True)
        self.assertEqual(completed.returncode,0,completed.stderr)
        self.assertIn('before effects',completed.stdout)

    def test_no_cross_runtime_connection_settings_session_reuse(self):
        owner=Owner();box=Mailbox(owner);owner.grant(self.repo)
        router=ApprovalRouter(owner,'g','s','r','run',self.repo)
        first=launch.resolve_settings(self.config)[0]
        own=ThreadOwnership(box,router,first.fingerprint);own.bind('native-session','tools');own.prompted('native-session','mandate')
        self.assertEqual(own.latest()['thread'],'native-session')
        for settings in (replace(first,connection='another'),replace(first,runtime='claude_code'),replace(first,model='different'),replace(first,turn_timeout_s=60)):
            other=ThreadOwnership(box,router,settings.fingerprint)
            self.assertIsNone(other.latest('native-session'));self.assertIsNone(other.owned('native-session'))
            with self.assertRaisesRegex(IntegrationError,'CROSS_BINDING_THREAD_DENIED'):other.bind('native-session','tools')
        owner.db.close()
