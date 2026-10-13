import asyncio
import base64
import io
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from PIL import Image
from agent_execution.companion.protocol import decode_attachments, dedup_input, conversation_id
from agent_execution.companion.desktop import desktop_bridge, desktop_tool, human_desktop_command
from permissions import set_current_role, check_tool_permission
from websocket_server import Session, Hub, request_desktop_tool, handle_client_message
from agent_execution.delivery import EventStore


class CompanionFeaturesTests(unittest.TestCase):
    def test_attachment_validation_and_digest_include_bytes(self):
        def item(data=b'hello'):
            return {'filename': 'document.txt', 'content_type': 'text/plain', 'data_base64': base64.b64encode(data).decode()}
        attachments = decode_attachments([item()])
        self.assertEqual(attachments[0].data, b'hello')
        self.assertNotEqual(dedup_input('same', attachments), dedup_input('same', decode_attachments([item(b'other')])))
        self.assertEqual(dedup_input('legacy', ()), 'legacy')
        for value in [[dict(item(), filename='../bad')], [dict(item(), data_base64='bad')], [dict(item(), content_type='image/png')], [item()] * 5, [item(b'x' * (1024 * 1024 + 1))], [item(b'x' * 1024 * 1024)] * 3]:
            with self.assertRaises(ValueError):
                decode_attachments(value)

    def test_image_validation_and_actual_multimodal_message(self):
        stream = io.BytesIO()
        Image.new('RGB', (10, 10)).save(stream, 'PNG')
        data = {'filename': 'picture.png', 'content_type': 'image/png', 'data_base64': base64.b64encode(stream.getvalue()).decode()}
        from agent_runtime import build_user_message
        message = build_user_message('image', decode_attachments([data]))
        self.assertTrue(any(part.get('type') == 'image_url' for part in message['content']))
        with self.assertRaises(ValueError):
            decode_attachments([dict(data, content_type='image/jpeg')])

    def test_conversation_owners_and_legacy_hub_keys(self):
        with tempfile.TemporaryDirectory() as root:
            hub = Hub(EventStore(Path(root) / 'events.db'))
            old = hub.get('device', 'desktop', 'user')
            new = hub.get('device', 'desktop', 'user', conversation='new')
            self.assertIs(hub.sessions[('user', 'default', 'device')], old)
            self.assertNotEqual(old.owner, new.owner)
            self.assertNotEqual(old.scoped_device, new.scoped_device)
            asyncio.run(new.send('notify', {'text': 'new'}))
            self.assertEqual(old.seq, 0)
            self.assertEqual(new.seq, 1)
        for value in [None, '../new', 'a' * 65, 5]:
            with self.assertRaises(ValueError):
                conversation_id(value)

    def test_desktop_roles_and_missing_transport(self):
        with desktop_bridge(lambda action, arguments: {'success': True, 'action': action}):
            with set_current_role('viewer'):
                self.assertTrue(check_tool_permission('desktop_tool'))
                self.assertTrue(desktop_tool('system_info')['success'])
                with self.assertRaises(PermissionError):
                    desktop_tool('open_url', {'url': 'https://example.com'})
            with set_current_role('operator'):
                with self.assertRaises(PermissionError):
                    desktop_tool('clipboard_write', {'text': 'x'})
            with set_current_role('admin'):
                self.assertIn('clipboard_write', human_desktop_command('/desktop clipboard_write text'))
        self.assertFalse(desktop_tool('system_info')['success'])
        with self.assertRaises(ValueError):
            desktop_tool({}, {})

    def test_desktop_request_correlates_result_and_cleans_waiter(self):
        async def scenario():
            class Socket:
                async def send_text(self, raw):
                    import json
                    message = json.loads(raw)
                    self.message = message
                    # A wrong ref cannot resolve this waiter.
                    await handle_client_message(session, {'type': 'tool.result', 'ref': 'wrong', 'payload': {'ok': True}})
                    await handle_client_message(session, {'type': 'tool.result', 'ref': message['id'], 'payload': {'ok': True, 'data': 'actual'}})
            socket = Socket()
            session = Session('device', caps={'desktop.system_info'}, ws=socket)
            session.state['desktop_connection_id'] = 'connection'
            result = await request_desktop_tool(session, 'system_info', {})
            self.assertEqual(result['output'], 'actual')
            self.assertEqual(session.desktop_requests, {})
            self.assertEqual(socket.message['payload']['connection_id'], 'connection')
            session.caps.clear()
            self.assertFalse((await request_desktop_tool(session, 'system_info', {}))['success'])
        asyncio.run(scenario())

    def test_core_tool_registered(self):
        from base import TOOLS, TOOLS_SCHEMAS
        self.assertIn('desktop_tool', TOOLS)
        self.assertEqual(len([s for s in TOOLS_SCHEMAS if s.get('function', {}).get('name') == 'desktop_tool']), 1)

    def test_suspended_answer_keeps_attachment_user_message(self):
        from agent_execution.tasks import dispatch_task_input
        from types import SimpleNamespace
        attachment = decode_attachments([{'filename': 'answer.txt', 'content_type': 'text/plain', 'data_base64': base64.b64encode(b'attached answer').decode()}])
        row = {'id': 'suspended-task', 'state': 'suspended', 'episode': 42,
               'checkpoint': {'messages': [], 'suspended_tool_call_id': 'ask-user'}}
        with patch('agent_execution.tasks.TaskStore') as store, patch('agent_runtime.resume_agent_turn', return_value='resumed') as resume:
            store.return_value.list.return_value = [row]
            store.return_value.get.return_value = row
            self.assertEqual(dispatch_task_input('answer', SimpleNamespace(), attachments=attachment), 'resumed')
            messages = resume.call_args.args[0]
            self.assertEqual(messages[0]['tool_call_id'], 'ask-user')
            self.assertEqual(messages[1]['role'], 'user')
            self.assertIn('attached answer', messages[1]['content'][1]['text'])
            with self.assertRaises(ValueError):
                dispatch_task_input('/clear', SimpleNamespace(), attachments=attachment)
