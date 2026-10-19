from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from contextvars import copy_context
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from agent_execution.budgets import TaskBudget, BudgetExhausted, budget_scope, budget_client, child_scope, _depth
from agent_execution.context import fit_tool_schemas, govern_messages, normalize_blocks, ContextTooLarge, message_tokens
from agent_execution.policy import execution_scope, execute_tool, current_context
from agent_execution.tasks import task_owner, _active


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env=patch.dict(os.environ,{'AGENT_TASK_DB':str(Path(self.temp.name)/'tasks.db'),
            'AGENT_TASK_MAX_REQUESTS':'3','AGENT_TASK_MAX_TOKENS':'20000','AGENT_MODEL_MAX_OUTPUT_TOKENS':'100',
            'AGENT_TASK_MAX_COST_USD':'0','AGENT_MODEL_RATES':'{}','AGENT_MODEL_CAPABILITIES':'{}',
            'AGENT_TASK_DEADLINE_SECONDS':'60','AGENT_MODEL_CONCURRENCY':'1',
            'AGENT_TASK_MAX_CHILDREN':'2','AGENT_TASK_MAX_DEPTH':'1'})
        env.start();self.addCleanup(env.stop)
        scope=execution_scope(task_id='budget-test',identity='local',session='test')
        scope.__enter__();self.addCleanup(lambda:scope.__exit__(None,None,None))
        self.owner=task_owner(current_context())

    def client(self,result=None):
        return NS(chat=NS(completions=NS(create=Mock(return_value=result or NS(usage=None)))),
            embeddings=NS(create=Mock(return_value=NS(usage=NS(total_tokens=10)))))

    def test_failed_requests_and_explicit_retries_are_reserved_and_persisted(self):
        raw=self.client()
        raw.chat.completions.create.side_effect=TimeoutError('provider timeout')
        with budget_scope() as budget:
            client=budget_client(raw)
            for _ in range(3):
                with self.assertRaises(TimeoutError):
                    client.chat.completions.create(model='m',messages=[{'role':'user','content':'中文'}])
            with self.assertRaises(BudgetExhausted):
                client.chat.completions.create(model='m',messages=[])
        with patch.dict(os.environ,{'AGENT_TASK_MAX_REQUESTS':'100','AGENT_TASK_MAX_TOKENS':'999999'}):
            restored=TaskBudget(self.owner,'budget-test')
            self.assertEqual(restored.max_requests,3)
            self.assertEqual(restored.snapshot()['requests'],3)
            with self.assertRaises(BudgetExhausted):restored.reserve('m',1,1)
        self.assertEqual(raw.chat.completions.create.call_count,3)

    def test_atomic_reservations_cannot_exceed_budget(self):
        budget=TaskBudget(self.owner,'atomic')
        def reserve(_):
            try:budget.reserve('m',1,1);return True
            except BudgetExhausted:return False
        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(sum(pool.map(reserve,range(20))),3)
        self.assertEqual(budget.snapshot()['requests'],3)

    def test_successful_usage_settles_estimate_once_and_survives_restart(self):
        budget=TaskBudget(self.owner,'settle')
        key=budget.reserve('m',18000,100)
        budget.reconcile(key,NS(total_tokens=400,prompt_tokens=300,completion_tokens=100))
        budget.reconcile(key,NS(total_tokens=400,prompt_tokens=300,completion_tokens=100))
        self.assertEqual(budget.snapshot()['tokens'],400)
        restored=TaskBudget(self.owner,'settle')
        restored.reserve('m',18000,100)
        self.assertEqual(restored.snapshot()['tokens'],18500)
        self.assertEqual(restored.snapshot()['requests'],2)

    def test_context_overflow_compacts_tool_schemas_before_provider_call(self):
        schemas = [
            {'function': {'name': 'fetch_url', 'description': 'fetch ' + 'x' * 1500}},
            {'function': {'name': 'write_file', 'description': 'write ' + 'x' * 1500}},
            {'function': {'name': 'ask_user', 'description': 'ask ' + 'x' * 1500}},
        ]
        raw = self.client()
        profile = {'m': {'context_tokens': 2300}}
        with patch.dict(os.environ, {'AGENT_MODEL_CAPABILITIES': json.dumps(profile)}), budget_scope():
            budget_client(raw).chat.completions.create(
                model='m', messages=[{'role': 'user', 'content': 'Fetch https://example.test'}], tools=schemas)
        sent = raw.chat.completions.create.call_args.kwargs['tools']
        self.assertIn('fetch_url', [schema['function']['name'] for schema in sent])
        self.assertLess(len(sent), len(schemas))

    def test_missing_or_invalid_usage_keeps_reservation(self):
        budget=TaskBudget(self.owner,'unknown')
        key=budget.reserve('m',1000,100)
        for usage in (None,NS(total_tokens=-1),NS(total_tokens=True),NS(total_tokens='100')):
            budget.reconcile(key,usage)
        self.assertEqual(budget.snapshot()['tokens'],1100)
        with self.assertRaisesRegex(BudgetExhausted,r'tokens \(1100 \+ 20000 > 20000\)'):
            budget.reserve('m',20000,0)

    def test_failed_stream_does_not_refund_partial_usage(self):
        class Stream:
            def __iter__(self):
                yield NS(usage=NS(total_tokens=1))
                raise RuntimeError('stream failed')
            def close(self):pass
        with budget_scope() as budget:
            stream=budget_client(self.client(Stream())).chat.completions.create(model='m',messages=[],stream=True)
            reserved=budget.snapshot()['tokens']
            with self.assertRaises(RuntimeError):list(stream)
            self.assertEqual(budget.snapshot()['tokens'],reserved)

    def test_partial_stream_usage_charges_upward_before_failure(self):
        class Stream:
            def __iter__(self):
                yield NS(usage=NS(total_tokens=1000))
                raise RuntimeError('stream failed')
            def close(self):pass
        with budget_scope() as budget:
            stream=budget_client(self.client(Stream())).chat.completions.create(model='m',messages=[],stream=True)
            with self.assertRaises(RuntimeError):list(stream)
            self.assertEqual(budget.snapshot()['tokens'],1000)

    def test_stream_close_waits_for_inflight_cancellation_database_read(self):
        import threading
        from agent_execution.budgets import BudgetStream
        entered,release,finished=threading.Event(),threading.Event(),threading.Event()
        budget=TaskBudget(self.owner,'monitor-close')
        def cancelled():
            with closing(budget.connect()) as conn:
                conn.execute('SELECT 1').fetchone()
                entered.set()
                release.wait(5)
            return False
        slot=NS(__exit__=Mock())
        stream=BudgetStream(Mock(),budget,'unused',slot,cancelled)
        closer=threading.Thread(target=lambda:(stream.close(),finished.set()))
        try:
            self.assertTrue(entered.wait(2))
            closer.start()
            self.assertTrue(stream.stopped.wait(2))
            self.assertFalse(finished.wait(.1))
        finally:
            release.set()
            if closer.ident is not None:closer.join(5)
            stream.close()
        self.assertTrue(finished.is_set())
        self.assertFalse(stream.thread.is_alive())
        slot.__exit__.assert_called_once()

    def test_stream_monitor_can_close_itself_without_self_join(self):
        from agent_execution.budgets import BudgetStream
        stream=BudgetStream(Mock(),TaskBudget(self.owner,'self-close'),'unused',NS(__exit__=Mock()),lambda:True)
        stream.thread.join(2)
        self.assertFalse(stream.thread.is_alive())
        self.assertTrue(stream.closed)
        stream.close()

    def test_stream_close_error_still_stops_monitor_and_releases_slot(self):
        from agent_execution.budgets import BudgetStream
        raw=Mock()
        raw.close.side_effect=RuntimeError('injected close failure')
        slot=NS(__exit__=Mock())
        stream=BudgetStream(raw,TaskBudget(self.owner,'close-error'),'unused',slot,lambda:False)
        with self.assertRaisesRegex(RuntimeError,'injected close failure'):
            stream.close()
        self.assertFalse(stream.thread.is_alive())
        slot.__exit__.assert_called_once()

    def test_cost_rates_are_persisted_missing_rates_rejected_and_usage_reconciled(self):
        with patch.dict(os.environ,{'AGENT_TASK_MAX_COST_USD':'0.001','AGENT_MODEL_RATES':'{"m":{"input":1,"output":10}}'}):
            budget=TaskBudget(self.owner,'cost')
        with patch.dict(os.environ,{'AGENT_MODEL_RATES':'{"m":{"input":0,"output":0}}'}):
            restored=TaskBudget(self.owner,'cost')
            with self.assertRaises(BudgetExhausted):restored.reserve('unknown',1,1)
            key=restored.reserve('m',10,10)
            with self.assertRaises(BudgetExhausted):
                restored.reconcile(key,NS(total_tokens=500,prompt_tokens=10,completion_tokens=490))
            with self.assertRaises(BudgetExhausted):restored.check()
            self.assertGreater(restored.snapshot()['cost'],.001)

    def test_children_share_budget_and_cannot_overwrite_parent_checkpoint(self):
        with budget_scope() as budget:
            token=_active.set(('parent','owner','id','lease'))
            try:
                budget.reserve_child(_depth.get())
                copied=copy_context()
                def child():
                    with child_scope(),budget_scope() as inherited:
                        self.assertIs(inherited,budget)
                        self.assertIsNone(_active.get())
                        with self.assertRaises(BudgetExhausted):inherited.reserve_child(_depth.get())
                        inherited.reserve('m',1,1)
                copied.run(child)
                self.assertEqual(_active.get()[0],'parent')
                budget.reserve_child(0)
                with self.assertRaises(BudgetExhausted):budget.reserve_child(0)
                self.assertEqual(budget.snapshot()['requests'],1)
            finally:_active.reset(token)

    def test_deadline_stops_tool_before_side_effect(self):
        effect=Mock()
        with budget_scope() as budget:
            with closing(budget.connect()) as conn,conn:
                conn.execute('UPDATE task_budgets SET deadline=0')
            with self.assertRaises(BudgetExhausted):execute_tool('read_file',{},effect)
        effect.assert_not_called()

    def test_model_routing_capabilities_and_output_cap(self):
        raw=self.client()
        profile={'text':{'vision':False},'vision':{'vision':True,'output_parameter':'max_tokens'},'embed':{'embedding':False}}
        image=[{'role':'user','content':[{'type':'image_url','image_url':{'url':'data:image/png;base64,abc'}}]}]
        with patch.dict(os.environ,{'AGENT_MODEL_CAPABILITIES':json.dumps(profile)}),budget_scope():
            client=budget_client(raw)
            with self.assertRaisesRegex(ValueError,'image analysis was not performed'):
                client.chat.completions.create(model='text',messages=image)
            raw.chat.completions.create.assert_not_called()
            with patch.dict(os.environ,{'OLLAMA_VISION_MODEL':'vision'}):
                client.chat.completions.create(model='text',messages=image,max_tokens=999)
            values=raw.chat.completions.create.call_args.kwargs
            self.assertEqual(values['model'],'vision')
            self.assertEqual(values['max_tokens'],100)
            self.assertNotIn('max_completion_tokens',values)
            with self.assertRaises(ValueError):client.embeddings.create(model='embed',input='test')
            raw.embeddings.create.assert_not_called()

    def test_large_image_reaches_provider_with_multimodal_reservation_and_usage(self):
        import base64,io,random
        from PIL import Image
        stream=io.BytesIO()
        Image.frombytes('RGB',(256,256),random.Random(23).randbytes(256*256*3)).save(stream,'PNG')
        url='data:image/png;base64,'+base64.b64encode(stream.getvalue()).decode()
        messages=[{'role':'system','content':'Recognize the image'}, {'role':'user','content':[
            {'type':'text','text':'請辨識圖檔內容資訊'}, {'type':'image_url','image_url':{'url':url}}]}]
        raw=self.client(NS(usage=NS(total_tokens=1300,prompt_tokens=1200,completion_tokens=100)))
        profile={'vision':{'vision':True,'context_tokens':32768,'image_input_tokens':4096}}
        with patch.dict(os.environ,{'AGENT_MODEL_CAPABILITIES':json.dumps(profile),'OLLAMA_VISION_MODEL':'vision'}),budget_scope() as budget:
            budget_client(raw).chat.completions.create(model='text',messages=messages)
            self.assertGreaterEqual(budget.snapshot()['tokens'],1300)
            self.assertLess(budget.snapshot()['tokens'],6000)
            with closing(budget.connect()) as conn:
                reservation=conn.execute('SELECT tokens,status FROM budget_calls').fetchone()
            self.assertEqual(reservation['status'],'reported')
            self.assertEqual(reservation['tokens'],budget.snapshot()['tokens'])
        sent=raw.chat.completions.create.call_args.kwargs['messages']
        self.assertEqual(sent,messages)
        self.assertEqual(sent[-1]['content'][1]['image_url']['url'],url)
        self.assertLess(message_tokens(sent,4096),10000)
        self.assertGreater(len(url),200000)

    def test_image_reservation_limit_stops_provider_before_call(self):
        raw=self.client()
        images=[{'role':'user','content':[{'type':'image_url','image_url':{'url':'data:image/png;base64,abc'}}]*4}]
        with budget_scope():
            with self.assertRaises(ContextTooLarge):
                budget_client(raw).chat.completions.create(model='m',messages=images)
        raw.chat.completions.create.assert_not_called()

    def test_stream_usage_reconciles_and_close_releases_capacity(self):
        class Stream:
            def __iter__(self):
                yield NS(usage=NS(total_tokens=21000,prompt_tokens=1,completion_tokens=20999))
            def close(self):self.closed=True
        stream=Stream()
        raw=self.client(stream)
        with budget_scope() as budget:
            result=budget_client(raw).chat.completions.create(model='m',messages=[],stream=True)
            self.assertTrue(raw.chat.completions.create.call_args.kwargs['stream_options']['include_usage'])
            with self.assertRaises(BudgetExhausted):list(result)
            self.assertTrue(stream.closed)
            with closing(budget.connect()) as conn:
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM model_slots').fetchone()[0],0)

    def test_global_model_slot_wait_is_bounded_by_cancellation(self):
        first=TaskBudget(self.owner,'first')
        other=TaskBudget(self.owner,'other')
        with first.slot():
            with self.assertRaises(BudgetExhausted):
                with other.slot(lambda:True):pass
        with other.slot():pass

    def test_root_deadline_cancels_actual_host_process(self):
        import sys
        from agent_execution.process import run_process
        with patch.dict(os.environ,{'AGENT_TASK_DEADLINE_SECONDS':'.3'}),budget_scope():
            started=time.monotonic()
            result=run_process([sys.executable,'-c','import time; time.sleep(10)'],capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,130)
        self.assertLess(time.monotonic()-started,5)

    def test_agent_continuous_429_stops_at_root_request_limit(self):
        import httpx
        from openai import OpenAI
        import server
        calls=[]
        def handler(request):
            calls.append(request)
            return httpx.Response(429,headers={'retry-after':'0'},json={'error':{'message':'capacity'}})
        with httpx.Client(transport=httpx.MockTransport(handler)) as http:
            client=OpenAI(api_key='test',base_url='http://fixture/v1',http_client=http)
            with budget_scope(),patch('server.get_client',return_value=client),patch('server.TOOLS_SCHEMAS',[]):
                result=server.run_agent([{'role':'user','content':'hello'}],max_retries=20)
        self.assertEqual(len(calls),3)
        self.assertIn('budget exhausted',result)

    def test_stream_wall_deadline_closes_blocked_stream_and_releases_slot(self):
        import threading
        class Stream:
            def __init__(self):self.stop=threading.Event()
            def __iter__(self):
                self.stop.wait(5)
                return
                yield
            def close(self):self.stop.set()
        stream=Stream()
        with patch.dict(os.environ,{'AGENT_TASK_DEADLINE_SECONDS':'.4'}),budget_scope() as budget:
            result=budget_client(self.client(stream)).chat.completions.create(model='m',messages=[],stream=True)
            with self.assertRaises(BudgetExhausted):list(result)
            with closing(budget.connect()) as conn:
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM model_slots').fetchone()[0],0)

    def test_cancelled_stream_eof_cannot_be_mistaken_for_normal_completion(self):
        import threading
        cancelled=threading.Event()
        class Stream:
            def __init__(self):self.stop=threading.Event()
            def __iter__(self):
                self.stop.wait(5)
                return
                yield
            def close(self):self.stop.set()
        stream=Stream()
        with budget_scope(),execution_scope(check_cancelled=cancelled.is_set):
            result=budget_client(self.client(stream)).chat.completions.create(model='m',messages=[],stream=True)
            cancelled.set()
            with self.assertRaises(BudgetExhausted):list(result)

    def test_sdk_hidden_retries_disabled_on_real_http_failure(self):
        import httpx
        from openai import OpenAI, APIStatusError
        calls=[]
        def handler(request):
            calls.append(request)
            return httpx.Response(429,json={'error':{'message':'capacity','type':'capacity'}})
        with httpx.Client(transport=httpx.MockTransport(handler)) as http:
            raw=OpenAI(api_key='test',base_url='http://fixture/v1',http_client=http,max_retries=5)
            with budget_scope():
                with self.assertRaises(APIStatusError):
                    budget_client(raw).chat.completions.create(model='m',messages=[{'role':'user','content':'hello'}])
        self.assertEqual(len(calls),1)

    def test_probe_failure_is_unknown_and_contains_no_provider_error_body(self):
        from agent_execution.model_probe import probe
        raw=self.client()
        raw.chat.completions.create.side_effect=TimeoutError('secret-provider-key')
        with budget_scope():
            result=probe(raw,'m',vision=True)
        self.assertEqual(result['capabilities']['vision']['status'],'unknown')
        self.assertNotIn('secret-provider-key',json.dumps(result))
        self.assertEqual(raw.chat.completions.create.call_count,2)

    def test_communication_admission_keeps_normal_jobs_pending_but_accepts_status(self):
        from communication_worker import CommunicationWorker
        regular=NS(command_id='job',text='work')
        store=Mock()
        store.pending_commands.return_value=[regular]
        runner=Mock(task_store_enabled=False)
        worker=CommunicationWorker(store,{},runner)
        worker._threads=[Mock()]
        with patch.dict(os.environ,{'AGENT_TASK_CONCURRENCY':'1'}):
            self.assertIsNone(worker.process_next())
            store.mark_command_running.assert_not_called()
            control=NS(command_id='status',text='/status',platform='telegram',sender_id='alice',conversation_id='chat',source_message_id=1)
            store.pending_commands.return_value=[regular,control]
            store.query_jobs_status.return_value=[]
            with patch.object(worker,'_send_reply'):
                worker.process_next()
            store.mark_command_running.assert_called_once_with('status')


class ContextTests(unittest.TestCase):
    def test_tool_schema_fallback_keeps_explicitly_requested_tool_within_budget(self):
        schemas = [
            {'function': {'name': 'fetch_url', 'description': 'fetch ' + 'x' * 1500}},
            {'function': {'name': 'write_file', 'description': 'write ' + 'x' * 1500}},
            {'function': {'name': 'ask_user', 'description': 'ask ' + 'x' * 1500}},
        ]
        messages = [{'role': 'system', 'content': 'trusted'},
                    {'role': 'user', 'content': 'Fetch this URL: https://example.test'}]
        with self.assertRaises(ContextTooLarge):
            govern_messages(messages, 2200, schemas)
        fitted = fit_tool_schemas(messages, 2200, schemas)
        names = [schema['function']['name'] for schema in fitted]
        self.assertIn('fetch_url', names)
        self.assertNotIn('write_file', names)
        self.assertEqual(govern_messages(messages, 2200, fitted), messages)

    def test_image_encoding_length_is_not_text_context_and_images_remain_intact(self):
        import copy
        messages=[{'role':'user','content':[{'type':'text','text':'辨識'},
            {'type':'image_url','image_url':{'url':'data:image/png;base64,'+'A'*1000000,'detail':'high'}}]}]
        original=copy.deepcopy(messages)
        with patch.dict(os.environ,{'AGENT_MODEL_IMAGE_TOKENS':'8192'}):
            self.assertEqual(govern_messages(messages,32768),original)
            self.assertEqual(messages,original)
            short=copy.deepcopy(messages)
            short[0]['content'][1]['image_url']['url']='https://example.com/image.png'
            self.assertEqual(message_tokens(short),message_tokens(messages))
            self.assertEqual(message_tokens(messages*2,8192)-message_tokens(messages*2,4096),8192)

    def test_image_metadata_and_large_text_are_still_budgeted(self):
        image={'type':'image_url','image_url':{'url':'data:image/png;base64,'+'A'*100000}}
        messages=[{'role':'user','content':[image,{'type':'text','text':'text'*10000}]}]
        with self.assertRaises(ContextTooLarge):govern_messages(messages,32768)
        with self.assertRaises(ContextTooLarge):govern_messages([{'role':'system','content':'x'*40000},{'role':'user','content':[image]}],32768)
        with self.assertRaises(ContextTooLarge):govern_messages([{'role':'user','content':[image]}],32768,tools=[{'description':'x'*40000}])
        for value in (0,-1,True,1.5,'nan'):
            with self.assertRaises(ValueError):message_tokens([{'role':'user','content':[image]}],value)

    def test_compaction_keeps_latest_image_and_protected_approval(self):
        image={'type':'image_url','image_url':{'url':'data:image/png;base64,'+'A'*100000}}
        messages=[{'role':'system','content':'trusted'}, {'role':'user','content':'old'*10000},
            {'role':'assistant','tool_calls':[{'id':'call','function':{'name':'x','arguments':'{}'}}]},
            {'role':'tool','tool_call_id':'call','content':'approval_required: 123'},
            {'role':'user','content':[image]}]
        result=govern_messages(messages,10000,image_tokens=4096,goal='recognize the image')
        self.assertIn(image,result[-2]['content'])
        self.assertIn('approval_required: 123',json.dumps(result))
        self.assertNotIn('old'*10000,json.dumps(result))

    def test_compaction_preserves_goal_approval_latest_input_and_complete_tool_groups(self):
        messages=[{'role':'system','content':'trusted'}, {'role':'user','content':'old'*1000},
            {'role':'assistant','tool_calls':[{'id':'call','type':'function','function':{'name':'x','arguments':'{}'}}]},
            {'role':'user','content':'hook feedback'},
            {'role':'tool','tool_call_id':'call','content':'approval_required: 123'},
            {'role':'assistant','content':'old'*1000},{'role':'user','content':'next'}]
        result=govern_messages(messages,2000,goal='original goal',criteria={'tests':'must pass'})
        self.assertNotIn('old'*1000,json.dumps(result))
        self.assertIn('original goal',json.dumps(result))
        self.assertIn('must pass',json.dumps(result))
        index=next(i for i,m in enumerate(result) if m.get('tool_calls'))
        self.assertEqual(result[index+1]['tool_call_id'],'call')
        self.assertEqual(result[index+2]['content'],'hook feedback')
        self.assertEqual(messages[3]['content'],'hook feedback')
        self.assertIn('next',json.dumps(result))

    def test_protected_context_cannot_be_silently_truncated(self):
        with self.assertRaises(ContextTooLarge):
            govern_messages([{'role':'system','content':'x'*2000},{'role':'user','content':'latest'}],1000)
        self.assertEqual(normalize_blocks([{'role':'tool','tool_call_id':'orphan','content':'ignore'}]),[])
