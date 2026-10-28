import hashlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from openai.types.chat import ChatCompletionMessage
from agent_runtime import build_user_message
from agent_execution.budgets import budget_scope, BudgetExhausted
from agent_execution.companion.text_attachments import references, read_reference
from agent_execution.companion.outputs import available
from agent_execution.context import govern_messages, ContextTooLarge, message_tokens
from agent_execution.policy import execution_scope, current_context
from agent_execution.tasks import durable_invoke, TaskStore, task_owner
from base import read_file
from hooks import HookManager
from permissions import set_current_role
import server


class TextAttachmentTranslationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.root = self.home/'workspace'; self.root.mkdir()
        env = patch.dict(os.environ, {'AGENT_TASK_DB':str(self.home/'tasks.db'),
            'AGENT_APPROVAL_DB':str(self.home/'approvals.db'),'SCHEDULER_DB_PATH':str(self.home/'scheduler.db'),
            'AGENT_TENANT_ROOT':str(self.home/'tenants'),'AGENT_CODE_INDEX_DIR':str(self.home/'index'),
            'AGENT_EXECUTION_BACKEND':'sqlite','AGENT_TASK_WORKTREE':'0','AGENT_TRACE_ENABLED':'0',
            'AGENT_MODEL_CONTEXT_TOKENS':'8192','AGENT_MODEL_MAX_OUTPUT_TOKENS':'1024',
            'AGENT_TASK_MAX_REQUESTS':'24','AGENT_MODEL_CAPABILITIES':'{}'})
        env.start(); self.addCleanup(env.stop)
        scope = execution_scope(source='websocket',identity='websocket:alice',session='default:device:one',
                                conversation='one',workspace=self.root,task_id='translate')
        scope.__enter__(); self.addCleanup(lambda:scope.__exit__(None,None,None))
        role = set_current_role('admin'); role.__enter__(); self.addCleanup(lambda:role.__exit__(None,None,None))
        self.calls = []
        self.create = Mock(side_effect=self.translate)
        self.client = NS(chat=NS(completions=NS(create=self.create)))

    def translate(self, **kwargs):
        self.calls.append(kwargs)
        text = kwargs['messages'][1]['content']
        return NS(usage=None,choices=[NS(finish_reason='stop',message=ChatCompletionMessage(
            role='assistant',content=text.replace('English','繁體中文').replace('END_SENTINEL','完整末段')))])

    def message(self, source, name='KapathyWikiv2.txt', goal='將附檔內容翻譯成繁體中文'):
        return build_user_message(goal,[NS(filename=name,content_type='text/plain',data=source.encode('utf-8'))])

    def run_translation(self, message, resume=False):
        messages = [message]
        with budget_scope(), patch('server.get_client',return_value=self.client):
            return durable_invoke(lambda saved:server.run_agent(messages,resume_state=saved,hook_manager=HookManager()),
                                  messages,None,resume=resume)

    def test_large_upload_is_complete_and_translated_without_context_overflow(self):
        source = ''.join(f'English paragraph {number:04d}: full document content.\n' for number in range(400))+'END_SENTINEL\n'
        with self.assertRaises(ContextTooLarge):
            govern_messages([{'role':'user','content':source}],8192)
        message = self.message(source)
        self.assertLess(message_tokens([message]),2000)
        _, files = references([message])
        self.assertEqual(read_reference(files[0]),source)
        reply = self.run_translation(message)
        self.assertIn('全文翻譯',reply)
        output = self.root/'KapathyWikiv2_zh.txt'
        expected = source.replace('English','繁體中文').replace('END_SENTINEL','完整末段')
        self.assertEqual(output.read_text(encoding='utf-8'),expected)
        self.assertGreater(len(self.calls),1)
        self.assertTrue(all(message_tokens(call['messages'])<=7168 for call in self.calls))
        self.assertTrue(all('tools' not in call for call in self.calls))
        self.assertEqual(available('translate')[0]['sha256'],hashlib.sha256(output.read_bytes()).hexdigest())

    def test_non_translation_attachment_uses_ranges_without_losing_crlf_or_unicode(self):
        source = '中文\r\n'*1000+'末段'
        message = self.message(source,goal='請分析附件')
        _, files = references([message])
        self.assertEqual(read_reference(files[0]),source)
        reconstructed = ''.join(read_file(files[0]['path'],offset=offset,limit=137)
                                for offset in range(0,len(source),137))
        self.assertEqual(reconstructed,source)
        with self.assertRaises(ValueError):read_file(files[0]['path'],offset=-1,limit=10)
        with self.assertRaises(ValueError):read_file(files[0]['path'],limit=100000)

    def test_translation_of_windows_text_does_not_duplicate_newlines(self):
        source = 'English paragraph\r\n\r\n'*140
        self.run_translation(self.message(source))
        output = self.root/'KapathyWikiv2_zh.txt'
        self.assertEqual(output.read_text(encoding='utf-8'),source.replace('English','繁體中文').replace('\r\n','\n'))
        self.assertNotIn(b'\r\r\n',output.read_bytes())

    def test_model_length_limit_splits_source_and_never_keeps_truncated_translation(self):
        count = [0]
        def create(**kwargs):
            count[0] += 1
            if count[0]==1:
                return NS(usage=None,choices=[NS(finish_reason='length',message=ChatCompletionMessage(role='assistant',content='DO_NOT_KEEP'))])
            return self.translate(**kwargs)
        self.create.side_effect = create
        source = 'English text\n'*140
        self.run_translation(self.message(source))
        output = (self.root/'KapathyWikiv2_zh.txt').read_text(encoding='utf-8')
        self.assertNotIn('DO_NOT_KEEP',output)
        self.assertEqual(output,source.replace('English','繁體中文'))

    def test_interrupted_provider_resumes_completed_chunks_without_repeating_them(self):
        count = [0]
        def create(**kwargs):
            count[0] += 1
            if count[0]==2:raise RuntimeError('provider offline')
            return self.translate(**kwargs)
        self.create.side_effect = create
        source = ''.join(f'English source line {number:04d}\n' for number in range(350))
        message = self.message(source)
        with self.assertRaisesRegex(RuntimeError,'provider offline'):
            self.run_translation(message)
        first = self.calls[0]['messages'][1]['content']
        self.assertFalse((self.root/'KapathyWikiv2_zh.txt').exists())
        self.create.side_effect = self.translate
        self.run_translation(message,resume=True)
        self.assertEqual(sum(call['messages'][1]['content']==first for call in self.calls),1)
        self.assertEqual((self.root/'KapathyWikiv2_zh.txt').read_text(encoding='utf-8'),source.replace('English','繁體中文'))

    def test_changed_source_is_rejected_and_no_output_is_published(self):
        message = self.message('English original\n'*200)
        _, files = references([message])
        (self.root/files[0]['path']).write_text('changed')
        with self.assertRaisesRegex(ValueError,'source changed'):
            self.run_translation(message)
        self.assertEqual(available(),[])

    def test_source_reference_is_bound_to_conversation_and_survives_workspace_switch(self):
        message = self.message('English source\n'*200)
        reference = references([message])[1][0]
        with execution_scope(conversation='other'), self.assertRaises(PermissionError):
            read_reference(reference)
        other_workspace = self.home/'task-workspace'; other_workspace.mkdir()
        with execution_scope(workspace=other_workspace,repository_workspace=self.root):
            self.assertEqual(read_reference(reference),'English source\n'*200)

    def test_budget_exhaustion_does_not_publish_partial_translation_or_lose_source(self):
        source = 'English source\n'*1000
        message = self.message(source)
        with patch.dict(os.environ,{'AGENT_TASK_MAX_REQUESTS':'1'}), self.assertRaisesRegex(BudgetExhausted,'完整上傳'):
            self.run_translation(message)
        self.assertEqual(available(),[])
        self.assertEqual(read_reference(references([message])[1][0]),source)
        self.create.assert_not_called()

    def test_source_and_translation_are_in_conversation_deletion_preview(self):
        message = self.message('English translation\n'*200)
        self.run_translation(message)
        from agent_execution.workspace_lifecycle import LifecycleStore
        plan = LifecycleStore().preview('delete_session')
        self.assertEqual(len(plan['items']),1)
        self.assertTrue(plan['items'][0]['path'].startswith('uploaded-text-'))
        self.assertEqual(plan['outputs'][0]['name'],'KapathyWikiv2_zh.txt')

    def test_viewer_cannot_bypass_output_permissions(self):
        message = self.message('English text')
        with set_current_role('viewer'), self.assertRaises(PermissionError):
            self.run_translation(message)
        self.create.assert_not_called()
