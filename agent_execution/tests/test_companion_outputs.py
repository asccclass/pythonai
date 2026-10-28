import base64
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from agent_execution.policy import execution_scope
from agent_execution.companion.outputs import available, read_chunk, CHUNK_BYTES
from agent_execution.workspace_lifecycle import LifecycleStore
from base import write_file
from permissions import set_current_role


class CompanionOutputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[2])
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.root = self.home/'workspace'
        self.root.mkdir()
        env = patch.dict(os.environ, {'AGENT_TASK_DB':str(self.home/'tasks.db'),
            'AGENT_APPROVAL_DB':str(self.home/'approvals.db'), 'AGENT_WORKSPACE_CLEANUP':'1',
            'SCHEDULER_DB_PATH':str(self.home/'scheduler.db'), 'AGENT_TENANT_ROOT':str(self.home/'tenants'),
            'AGENT_CODE_INDEX_DIR':str(self.home/'index'), 'AGENT_TRACE_DB':str(self.home/'trace.db')})
        env.start(); self.addCleanup(env.stop)
        scope = execution_scope(source='websocket', identity='websocket:alice', session='default:device:one',
                                conversation='one', workspace=self.root, task_id='request')
        scope.__enter__(); self.addCleanup(lambda:scope.__exit__(None,None,None))
        role = set_current_role('admin')
        role.__enter__(); self.addCleanup(lambda:role.__exit__(None,None,None))

    def test_translated_file_is_verified_and_downloaded_in_chunks(self):
        content = '繁體中文翻譯\n' * (CHUNK_BYTES//10)
        result = write_file('prompt_zh.txt', content)
        self.assertTrue(result['success'])
        item = result['artifacts'][0]
        self.assertEqual(available('request'), [item])
        data = bytearray()
        while True:
            chunk = read_chunk(item['id'], len(data))
            data.extend(base64.b64decode(chunk['data']))
            if chunk['eof']:break
        self.assertEqual(data, (self.root/'prompt_zh.txt').read_bytes())
        self.assertEqual(data.decode('utf-8').replace('\r\n','\n'), content)
        self.assertEqual(hashlib.sha256(data).hexdigest(), item['sha256'])
        self.assertNotIn('path', item)

    def test_existing_user_document_is_not_adopted(self):
        (self.root/'existing.txt').write_text('user document')
        self.assertNotIn('artifacts', write_file('existing.txt','updated'))
        self.assertEqual(available(), [])

    def test_explicit_output_filename_can_replace_a_previous_unregistered_translation(self):
        from agent_execution.tasks import TaskStore, task_owner, _active
        from agent_execution.policy import current_context
        (self.root/'prompt_zh.txt').write_text('previous unregistered result')
        (self.root/'prompt.txt').write_text('source document')
        store = TaskStore(); owner = task_owner(current_context())
        token = store.claim(owner,'request','幫我把 D:\\prompt.txt 翻譯成繁體中文，然後寫入prompt_zh.txt中',[],
                            criteria={'version':1,'checks':[]})
        active = _active.set((store,owner,'request',token))
        try:
            result = write_file('prompt_zh.txt','新的翻譯')
            self.assertEqual(result['artifacts'][0]['name'],'prompt_zh.txt')
            # Mentioning the source as input is not an output ownership grant.
            self.assertNotIn('artifacts',write_file('prompt.txt','source document'))
        finally:
            _active.reset(active)

    def test_other_conversation_cannot_download_or_overwrite(self):
        item = write_file('prompt_zh.txt', '翻譯')['artifacts'][0]
        for changes in ({'conversation':'two'}, {'identity':'websocket:bob'}):
            with execution_scope(**changes):
                self.assertEqual(available(), [])
                with self.assertRaises(PermissionError):read_chunk(item['id'],0)
                with self.assertRaises(PermissionError):write_file('prompt_zh.txt','wrong')
        self.assertEqual((self.root/'prompt_zh.txt').read_text(encoding='utf-8'), '翻譯')

    def test_missing_changed_and_linked_outputs_are_not_offered(self):
        item = write_file('prompt_zh.txt', '翻譯')['artifacts'][0]
        target = self.root/'prompt_zh.txt'
        target.write_text('changed', encoding='utf-8')
        self.assertEqual(available(), [])
        target.unlink()
        with self.assertRaises(FileNotFoundError):read_chunk(item['id'],0)
        outside = self.home/'outside.txt'; outside.write_text('private')
        os.link(outside,target)
        with self.assertRaises(PermissionError):read_chunk(item['id'],0)

    @unittest.skipUnless(os.name=='nt','native Windows cleanup')
    def test_delete_conversation_removes_output_and_skips_manual_deletion(self):
        write_file('prompt_zh.txt', '翻譯')
        write_file('missing.txt', 'already removed')
        (self.root/'missing.txt').unlink()
        store = LifecycleStore()
        plan = store.preview('delete_session')
        self.assertEqual(len(plan['outputs']), 2)
        self.assertTrue(next(r for r in plan['outputs'] if r['name']=='missing.txt')['missing'])
        self.assertNotIn('root', plan['outputs'][0])
        result = store.confirm(plan['plan_id'])
        self.assertEqual(result['state'], 'completed', result)
        self.assertFalse((self.root/'prompt_zh.txt').exists())
        self.assertEqual(available(), [])

    @unittest.skipUnless(os.name=='nt','native Windows cleanup')
    def test_manual_deletion_after_preview_is_also_safe(self):
        write_file('prompt_zh.txt', '翻譯')
        store = LifecycleStore(); plan = store.preview('delete_session')
        (self.root/'prompt_zh.txt').unlink()
        self.assertEqual(store.confirm(plan['plan_id'])['state'], 'completed')

    def test_changed_output_blocks_stale_deletion_plan(self):
        write_file('prompt_zh.txt', '翻譯')
        store = LifecycleStore(); plan = store.preview('delete_session')
        (self.root/'prompt_zh.txt').write_text('user changes',encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'Output files changed'):
            store.confirm(plan['plan_id'])
        self.assertTrue((self.root/'prompt_zh.txt').exists())

    def test_judge_receives_verified_file_and_workspace_delivery_policy(self):
        from task_evaluator import TaskEvaluator
        item = write_file('prompt_zh.txt', '繁體中文翻譯')['artifacts'][0]
        client = MagicMock()
        client.chat.completions.create.return_value.choices = [MagicMock(message=MagicMock(
            content='{"success":true,"score":1,"feedback":"translated and saved"}'))]
        evaluator = TaskEvaluator(self.root, client=client, model='judge')
        self.assertTrue(evaluator.evaluate_llm_judge('翻譯 D:\\prompt.txt 並寫入 prompt_zh.txt', [], '已產生檔案')[0])
        prompt = client.chat.completions.create.call_args.kwargs['messages'][1]['content']
        self.assertIn(item['sha256'],prompt)
        self.assertIn('desktop download link',prompt)
        (self.root/'prompt_zh.txt').unlink()
        evaluator.evaluate_llm_judge('翻譯','', '已完成')
        self.assertNotIn(item['sha256'],client.chat.completions.create.call_args.kwargs['messages'][1]['content'])

    def test_duplicate_write_does_not_reject_an_identical_verified_delivery(self):
        from agent_execution.results import normalize_result, ToolResult
        from agent_execution.tasks import TaskStore, task_owner, _active
        from agent_execution.policy import current_context
        from task_evaluator import TaskEvaluator
        result = write_file('prompt_zh.txt','翻譯')
        store = TaskStore(); owner = task_owner(current_context())
        token = store.claim(owner,'request','翻譯並寫入 prompt_zh.txt',[],criteria={'version':1,'checks':[]})
        args = {'path':'prompt_zh.txt','content':'翻譯'}
        store.checkpoint(owner,'request',token,'tool_done',{}, {'id':'written','name':'write_file',
            'arguments':args,'state':'completed','result':json.dumps(normalize_result(result).to_dict())})
        failed = ToolResult(False,error='Repeated tool operation',side_effects={'status':'none'},
                            evidence={'kind':'repeat_prevented'})
        store.checkpoint(owner,'request',token,'tool_done',{}, {'id':'duplicate','name':'write_file',
            'arguments':args,'state':'failed','result':json.dumps(failed.to_dict())})
        active = _active.set((store,owner,'request',token))
        try:
            evaluator = TaskEvaluator(self.root,git_status_fn=lambda:'clean')
            outcome = evaluator.evaluate('翻譯並寫入 prompt_zh.txt',[],'已存入 workspace')
            self.assertTrue(outcome.success,outcome.feedback)
            self.assertTrue(outcome.verified)
            (self.root/'prompt_zh.txt').unlink()
            outcome = evaluator.evaluate('翻譯並寫入 prompt_zh.txt',[],'已完成')
            self.assertFalse(outcome.success)
            self.assertIn('Tool step not successful',outcome.feedback)
        finally:
            _active.reset(active)
