"""Real SDK4/Claude Agent SDK + local fake native stdio; never inference/login."""
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from importlib.util import find_spec
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from darkharness.integration.artifacts import SecretGuard, render_mandate
from darkharness.integration.contract import SeatSettings
from darkharness.integration.launch import BACKENDS, _claude_factory
from darkharness.integration.mailbox import IntegrationError, Mailbox, digest, encode
from darkharness.integration.policy import ApprovalRouter
from test_integration_mailbox import Owner
from test_integration_codex import Tools

HAS_CLAUDE = find_spec('band') is not None and find_spec('claude_agent_sdk') is not None and find_spec('mcp') is not None
if HAS_CLAUDE:
    from band.core.types import PlatformMessage
    from band.adapters.claude_sdk import ClaudeSDKSessionState
    from claude_agent_sdk import ClaudeSDKClient, ResultMessage, SystemMessage
    from darkharness.integration.claude import OwnedClaudeTransport
    from darkharness.integration.protected_tools import GuardedTools


# Implements actual SDK control protocol, not invented Codex events. Commands
# only spawn this Python fixture. It has no network/provider/auth implementation.
NATIVE = r'''
import sys,json,uuid,os
mode,model,session,cwd=sys.argv[1:5]
session=session or uuid.uuid4().hex
phase=0

def emit(x):print(json.dumps(x),flush=True)
def control(identifier,request):emit({'type':'control_request','request_id':identifier,'request':request})
def result():emit({'type':'result','subtype':'success','duration_ms':1,'duration_api_ms':0,'is_error':False,'num_turns':1,'session_id':session,'result':'component terminal'})
for line in sys.stdin:
 p=json.loads(line)
 if p.get('type')=='control_request':
  request=p['request'];sub=request['subtype']
  emit({'type':'control_response','response':{'subtype':'success','request_id':p['request_id'],'response':{}}})
  if sub=='initialize':
   print(json.dumps({'hook':request['hooks'],'env_names':sorted(os.environ),'cwd':os.getcwd()}),file=sys.stderr,flush=True)
 if p.get('type')=='user':
  emit({'type':'system','subtype':'init','session_id':session,'model':'wrong-model' if mode=='model-mismatch' else model})
  if mode=='wait':continue
  if mode=='eof':sys.exit(0)
  if mode=='no-terminal-tool':result();continue
  control('native-hook',{'subtype':'hook_callback','callback_id':'hook_0','tool_use_id':'native-write','input':{'hook_event_name':'PreToolUse','cwd':cwd,'tool_name':'Write','tool_input':{'file_path':'/outside/scoped.txt','content':'fixture'}}})
 if p.get('type')=='control_response':
  r=p['response'];rid=r['request_id']
  if rid=='native-hook':
   print(json.dumps({'native_permission':r.get('response')}),file=sys.stderr,flush=True)
   control('mcp-init',{'subtype':'mcp_message','server_name':'dh','message':{'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2024-11-05','capabilities':{},'clientInfo':{'name':'fake-native','version':'1'}}}})
  elif rid=='mcp-init':
   control('mcp-ready',{'subtype':'mcp_message','server_name':'dh','message':{'jsonrpc':'2.0','method':'notifications/initialized'}})
  elif rid=='mcp-ready':
   name='dh_peer_question' if mode=='peer' else 'band_send_message'
   args={'question':'full peer question'} if mode=='peer' else {'content':'component done','mentions':['peer']}
   control('mcp-call',{'subtype':'mcp_message','server_name':'dh','message':{'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':name,'arguments':args}}})
  elif rid=='mcp-call':
   print(json.dumps({'mcp_receipt':r.get('response')}),file=sys.stderr,flush=True)
   if mode=='after-send-eof':sys.exit(0)
   result()
'''


@unittest.skipUnless(HAS_CLAUDE and sys.platform=='linux','optional pinned Claude SDK + Linux required')
class ClaudeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name).resolve()
        subprocess.run(['git','init','-q',str(self.root)],check=True)
        self.owner=Owner();self.box=Mailbox(self.owner)
        self.owner.grant(self.root,file_write=True,approved_commands=[['git','status']])
        self.router=ApprovalRouter(self.owner,'g','s','r','run',self.root)
        self.guard=SecretGuard([('fixture-pattern',r'\bsk-\w{16,}')],source_hash='fixture')
        self.settings=SeatSettings('fixture-claude','claude_code',str(self.root),'test-claude','high',10,
                                  ('/usr/bin/true',),(('CLAUDE_CONFIG_DIR',str(self.root.parent/'external-fixture-auth')),),())
        self.mode='send';self.transports=[];self.clients=[]
        seat={'alias':'s','display_name':'fixture builder','role':'builder'}
        text=render_mandate(seat['display_name'],seat['role'],BACKENDS['claude_code'].harness,self.settings.model,self.settings.effort)
        self.adapter,self.runtime,self.binding=_claude_factory(settings=self.settings,seat=seat,config={'room_id':'r'},text=text,
            mailbox=self.box,router=self.router,guard=self.guard,coordinator='coordinator-id')
        outer=self
        class FakeNativeTransport(OwnedClaudeTransport):
            def _build_command(self):
                return [sys.executable,'-B','-u','-c',NATIVE,outer.mode,self._options.model,self._options.resume or '',self._cwd]
        def build(options,context):
            transport=FakeNativeTransport(options,lambda kind,data:self.adapter._record_for(context,kind,data),self.guard)
            self.transports.append(transport)
            self.adapter._transport_owned=transport
            client=ClaudeSDKClient(options=options,transport=transport)
            self.clients.append(client)
            return client
        self.adapter._build_native_client=build
        self.tools=Tools()
        await self.adapter.on_started('fixture builder','component')

    async def asyncTearDown(self):
        await asyncio.wait_for(self.adapter.on_cleanup('r'),10)
        self.owner.db.close();self.tmp.cleanup()

    async def deliver(self,mid='m',content='original full goal',sender='peer'):
        msg=PlatformMessage(mid,'r',content,sender,'agent','fixture peer','text',{},datetime.now(timezone.utc))
        await self.adapter.on_message(msg,self.tools,ClaudeSDKSessionState(session_id='untrusted-codex-thread'),None,None,is_session_bootstrap=True,room_id='r')

    async def settle(self):
        await asyncio.wait_for(self.adapter.worker,15)

    def events(self,kind):
        return [json.loads(r[0]) for r in self.owner.db.execute('SELECT body FROM c_event WHERE kind=?',(kind,))]

    async def until(self,condition):
        async with asyncio.timeout(10):
            while not condition():await asyncio.sleep(.01)

    async def test_real_sdk_mcp_protected_send_native_hook_provenance_and_env(self):
        with patch.dict('os.environ',{'ANTHROPIC_API_KEY':'synthetic-not-real','CLAUDE_CODE_USE_BEDROCK':'1','CLAUDE_CODE_USE_VERTEX':'1'}):
            await self.deliver();await self.settle()
        self.assertEqual(self.tools.sent,[('component done',['peer'])])
        work=self.owner.db.execute('SELECT * FROM c_work').fetchone()
        self.assertEqual((work['state'],work['delivery']),('SUCCEEDED','RETURNED'),work['result'])
        options=self.clients[0].options
        self.assertEqual((options.model,options.effort,options.cwd,options.permission_mode),('test-claude','high',str(self.root),'default'))
        self.assertEqual(options.system_prompt,self.adapter.config.custom_section)
        self.assertEqual(options.env['CLAUDE_CONFIG_DIR'],dict(self.settings.runtime_env)['CLAUDE_CONFIG_DIR'])
        self.assertEqual(options.plugins,[]);self.assertEqual(options.setting_sources,[]);self.assertIsNone(options.fallback_model)
        stderr=[e['data']['text'] for e in self.events('CLAUDE_STDERR')]
        startup=next(json.loads(t) for t in stderr if 'env_names' in t)
        self.assertNotIn('ANTHROPIC_API_KEY',startup['env_names']);self.assertNotIn('CLAUDE_CODE_USE_BEDROCK',startup['env_names'])
        self.assertNotIn('CLAUDE_CODE_USE_VERTEX',startup['env_names'])
        decision=self.events('CLAUDE_PERMISSION_REPLY')[-1]['data'];self.assertFalse(decision['accepted'])
        self.assertTrue(self.events('CLAUDE_NATIVE_MESSAGE'))
        self.assertFalse(self.events('STDIN_RPC'));self.assertFalse(self.events('TURN_ACCEPTED'))
        self.assertTrue(self.events('CLAUDE_PROCESS_STOPPED'))
        self.assertEqual(self.owner.db.execute("SELECT state FROM c_outbox").fetchone()[0],'ACKED')

    async def test_owned_resume_source_evidence_cutover_and_untrusted_history_ignored(self):
        await self.deliver('first');await self.settle()
        first=self.adapter.thread_ownership.latest()['thread']
        await self.deliver('second','next own task');await self.settle()
        self.assertEqual(self.clients[-1].options.resume,first)
        with self.owner.transaction(self.owner.epoch) as db:
            db.execute("UPDATE c_owned_thread SET compatibility='old-tools' WHERE thread=?",(first,))
        await self.deliver('third','reviewer accepted scoped revision');await self.settle()
        self.assertIsNone(self.clients[-1].options.resume)
        self.assertNotEqual(self.adapter.thread_ownership.latest()['thread'],first)
        histories=self.events('OWNED_HISTORY_LINK')
        body=json.loads(self.owner.db.execute('SELECT body FROM c_artifact WHERE hash=?',(histories[-1]['history_ref'],)).fetchone()[0])
        self.assertIn('original full goal',json.dumps(body));self.assertIn('component terminal',json.dumps(body))
        self.assertNotIn('untrusted-codex-thread',[c.options.resume for c in self.clients])
        self.assertIn('sources',self.events('CLAUDE_TURN_DISPATCH')[-1]['data'])

    async def test_peer_question_authenticated_continuation_and_original_task(self):
        self.mode='peer';await self.deliver();await self.settle()
        work=dict(self.owner.db.execute('SELECT * FROM c_work').fetchone())
        self.assertEqual((work['state'],work['delivery']),('PAUSED','YIELDED'))
        question=self.owner.db.execute('SELECT * FROM c_question').fetchone()
        with self.assertRaisesRegex(IntegrationError,'PEER_BINDING_DENIED'):
            await self.runtime.peer_answer(question['id'],'not-coordinator','forged')
        self.mode='send'
        await self.runtime.peer_answer(question['id'],'coordinator-id','complete answer and evidence')
        await self.settle()
        rows=self.owner.db.execute('SELECT * FROM c_work ORDER BY rowid').fetchall()
        self.assertEqual(len(rows),2);self.assertEqual(rows[-1]['state'],'SUCCEEDED')
        self.assertIn('original full goal',rows[-1]['input']);self.assertIn('complete answer and evidence',rows[-1]['input'])

    async def test_permission_paths_commands_stale_callbacks_and_foreign_room(self):
        self.box.receive('s','r','peer','permission-task','full task');op=self.box.next_ready('s')['id'];self.box.claim(op,'a')
        context=(op,'a');self.adapter._context=context
        self.assertTrue(await self.adapter.native_permission('Write',{'file_path':str(self.root/'file')},'w',context))
        self.assertFalse(await self.adapter.native_permission('Write',{'file_path':'/outside/file'},'w2',context))
        self.assertTrue(await self.adapter.native_permission('Read',{'file_path':str(self.root/'file')},'r',context))
        self.assertFalse(await self.adapter.native_permission('Read',{'file_path':'/outside/file'},'r2',context))
        self.assertTrue(await self.adapter.native_permission('Bash',{'command':'git status'},'c',context))
        for command in ('git status; curl example.invalid','python -c pass','git status && true','git status $(id)'):
            self.assertFalse(await self.adapter.native_permission('Bash',{'command':command},'bad',context))
        hook=self.adapter._hooks(context)['PreToolUse'][0].hooks[0]
        reply=await hook({'cwd':'/outside','tool_name':'Write','tool_input':{'file_path':str(self.root/'file')}},'id',{})
        self.assertEqual(reply['hookSpecificOutput']['permissionDecision'],'deny')
        tools=GuardedTools(self.tools,self.adapter,*context);registered,_=self.adapter._mcp_tools(tools,context)
        send=next(t for t in registered if t.name=='band_send_message')
        blocked=await send.handler({'chat_id':'foreign','content':'no','mentions':['peer']})
        self.assertTrue(blocked['is_error']);self.assertEqual(self.tools.sent,[])
        self.box.update(*context,state='PAUSED',delivery='DELIVERY_UNKNOWN')
        stale=await send.handler({'content':'no','mentions':['peer']})
        self.assertTrue(stale['is_error']);self.assertEqual(self.tools.sent,[])
        self.adapter._context=None

    async def test_stderr_secret_guard_across_native_chunk_boundaries(self):
        from claude_agent_sdk import ClaudeAgentOptions
        records=[]
        opaque='opaque'+'fixture'+'value'+'123456789'
        self.guard.register_known(opaque)
        transport=OwnedClaudeTransport(ClaudeAgentOptions(cli_path='/usr/bin/true'),lambda k,d:records.append((k,d)),self.guard)
        async def chunks():
            yield opaque[:12]
            yield opaque[12:]+'\nordinary diagnostic\n'
        transport._stderr_stream=chunks()
        await transport._capture_stderr()
        text=json.dumps(records)
        self.assertNotIn(opaque,text);self.assertNotIn(opaque[:12],text)
        self.assertIn('[REDACTED]',text);self.assertIn('ordinary diagnostic',text)

    async def test_after_send_unknown_never_replays_or_synthetic_success(self):
        self.mode='after-send-eof';await self.deliver();await self.settle()
        work=dict(self.owner.db.execute('SELECT * FROM c_work').fetchone())
        self.assertEqual((work['state'],work['delivery']),('PAUSED','DELIVERY_UNKNOWN'))
        self.assertEqual(len(self.tools.sent),1)
        with self.assertRaisesRegex(IntegrationError,'UNKNOWN_EFFECT_RECONCILIATION_REQUIRED'):
            await self.runtime.resume(work['id'],work['attempt'])
        self.assertEqual(len(self.tools.sent),1);self.assertIsNone(self.box.next_ready('s'))

    async def test_native_model_mismatch_is_unknown_not_acceptance(self):
        self.mode='model-mismatch';await self.deliver();await self.settle()
        work=self.owner.db.execute('SELECT * FROM c_work').fetchone()
        self.assertEqual(work['delivery'],'DELIVERY_UNKNOWN');self.assertEqual(self.tools.sent,[])

    async def test_missing_terminal_tool_is_failure_not_acceptance(self):
        self.mode='no-terminal-tool';await self.deliver();await self.settle()
        latest=self.owner.db.execute('SELECT * FROM c_work').fetchone()
        self.assertEqual((latest['state'],latest['delivery']),('FAILED','RETURNED'))
        self.assertEqual(json.loads(latest['result'])['acceptance'],'NOT_EVALUATED')
        self.assertEqual(self.tools.sent,[])

    async def test_cancel_real_owned_process_and_attempt_fence(self):
        self.mode='wait';await self.deliver()
        await self.until(lambda:bool(self.events('CLAUDE_SESSION_BOUND')))
        work=dict(self.owner.db.execute('SELECT * FROM c_work').fetchone())
        with self.assertRaisesRegex(IntegrationError,'OWNER_ATTEMPT_FENCE'):
            await self.runtime.cancel(work['id'],'stale')
        result=await asyncio.wait_for(self.runtime.cancel(work['id'],work['attempt']),10)
        self.assertEqual(result['termination'],'OWNED_GROUP_STOPPED')
        self.assertEqual(self.box.read_work(work['id'])['state'],'CANCELLED')
        self.assertTrue(self.events('CLAUDE_PROCESS_STOPPED'))
        self.assertEqual(self.tools.sent,[])

    async def test_sdk_interrupt_routes_core_cancel_and_stops_owned_native(self):
        self.mode='wait';await self.deliver()
        await self.until(lambda:bool(self.events('CLAUDE_SESSION_BOUND')))
        work=dict(self.owner.db.execute('SELECT * FROM c_work').fetchone())
        await asyncio.wait_for(self.adapter.on_interrupt('r','stop'),10)
        self.assertEqual(self.box.read_work(work['id'])['state'],'CANCELLED')
        self.assertTrue(self.adapter.stopping);self.assertTrue(self.events('CLAUDE_PROCESS_STOPPED'))
        self.assertEqual(self.tools.sent,[])
        self.assertEqual(len(self.events('CONTROL_RECEIVED')),1)
        control=self.owner.db.execute('SELECT operation,attempt,kind,state FROM c_control').fetchone()
        self.assertEqual(tuple(control),(work['id'],work['attempt'],'cancel','DRAINED'))

    async def test_timeout_is_unknown_and_not_automatically_replayed(self):
        self.adapter.effective_settings=replace(self.settings,turn_timeout_s=.15)
        self.mode='wait';await self.deliver();await self.settle()
        row=self.owner.db.execute('SELECT * FROM c_work').fetchone()
        self.assertEqual((row['state'],row['delivery']),('PAUSED','DELIVERY_UNKNOWN'))
        self.assertIsNone(self.box.next_ready('s'));self.assertEqual(self.tools.sent,[])


@unittest.skipUnless(HAS_CLAUDE and sys.platform=='linux','optional pinned Claude SDK + Linux required')
class ClaudeVerificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_typed_git_and_real_verification_broker_with_report_paging(self):
        # Real trusted local checker fixture; protected Claude MCP handler routes
        # through the same GuardedTools/brokers rather than an adapter shortcut.
        from test_integration_verification import VerificationTests
        from darkharness.integration.launch import _claude_factory
        fixture=VerificationTests('test_automatic_receipt_resolution_and_returned_receipt_selector');fixture.setUp()
        f=fixture
        guard=SecretGuard([('fixture-pattern',r'\bsk-\w{16,}')],source_hash='fixture')
        settings=SeatSettings('fixture-cc','claude_code',str(f.repo),'test-claude','high',3600,('/usr/bin/true',),(),())
        adapter,runtime,binding=_claude_factory(settings=settings,seat={'alias':'s','display_name':'fixture reviewer'},config={'room_id':'r'},text='exact mandate',mailbox=f.box,router=f.router,guard=guard,coordinator='coordinator-id')
        adapter.verification.broker.parsers['json-v1']=lambda p,code:json.loads((p/'report.json').read_text())['accepted']
        context=(f.op,'a');adapter._context=context;tools=GuardedTools(Tools(),adapter,*context)
        registered,schemas=adapter._mcp_tools(tools,context)
        names={t.name for t in registered}
        self.assertTrue({'dh_local_git_commit','dh_review_snapshot','dh_verify','dh_verification_read'}<=names)
        try:
            # The runtime's review scratch must be INSIDE its exact workspace,
            # unlike the older broad-root helper used to build this fixture.
            with f.owner.transaction(f.owner.epoch) as db:
                grant=json.loads(db.execute("SELECT body FROM controls WHERE id='g'").fetchone()[0])
                grant['scope']['review_snapshot']['scratch']=str(f.repo/'.review')
                db.execute("UPDATE controls SET body=? WHERE id='g'",(json.dumps(grant),))
            snapshot=await next(t for t in registered if t.name=='dh_review_snapshot').handler(
                {'cwd':str(f.repo),'revision':f.revision,'name':'claude-exact-review'})
            self.assertFalse(snapshot['is_error'])
            receipt=json.loads(snapshot['content'][0]['text'])
            self.assertEqual(receipt['revision'],f.revision)
            self.assertTrue(Path(receipt['path']).is_dir())
            origins = [json.loads(row[0]) for row in f.owner.db.execute(
                "SELECT body FROM c_event WHERE operation=? AND kind='GIT_RUN_ORIGIN'", (f.op,))]
            self.assertTrue(any(origin['seat'] == 's' and origin['room'] == 'r'
                                and origin['run_id'] == f.router.run_id
                                and origin['workspace'] == str(f.repo)
                                and origin['attempt'] == 'a' for origin in origins))
            request=next(t for t in registered if t.name=='dh_verify')
            response=await request.handler({'check_id':'check','checkout_receipt':f.snap,'revision':f.revision})
            self.assertFalse(response['is_error'])
            effect=adapter._verification_effect
            self.assertIsNotNone(effect)
            result=await asyncio.wait_for(adapter.verification.broker.wait(*context,effect),10)
            child=adapter.verification.complete(*context,effect)
            self.assertIsNotNone(child);self.assertEqual(result['state'],'SUCCEEDED')
            f.box.claim(child,'child');adapter._context=(child,'child')
            adapter._verification_effect=None
            child_tools=GuardedTools(Tools(),adapter,child,'child')
            registered,_=adapter._mcp_tools(child_tools,adapter._context)
            page=await next(t for t in registered if t.name=='dh_verification_read').handler({'effect_id':effect,'artifact':'stderr','offset':0,'limit':8})
            self.assertFalse(page['is_error']);self.assertIn('stderr c',str(page))
        finally:
            adapter._context=None;adapter.verification.broker.close();fixture.tearDown()
