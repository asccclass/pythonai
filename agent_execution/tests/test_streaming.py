import asyncio
import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch
from fastapi.testclient import TestClient

import server
from agent_runtime import AgentRuntime
from laya_guard import GuardDecision
from agent_execution.streaming import streamed_completion
from websocket_server import app, hub, reset_hub_for_tests, send_via_telegram


def chunk(text=None, calls=None, finish=None):
    return NS(choices=[NS(index=0, delta=NS(content=text, tool_calls=calls), finish_reason=finish)])


class Stream:
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = threading.Event()
    def __iter__(self):
        yield from self.chunks
    def close(self):
        self.closed.set()


def runtime():
    return AgentRuntime(messages=[{"role": "system", "content": "system"}],
        guard=NS(assess=lambda text: GuardDecision()), memory=None, memory_searcher=None,
        skill_matcher=NS(match=lambda text: []), memory_worker=None, memory_classifier_factory=None,
        semantic_extractor=None, procedure_matcher=None, run_agent=server.run_agent)


class StreamingTests(unittest.TestCase):
    def setUp(self):
        reset_hub_for_tests()
        self.addCleanup(reset_hub_for_tests)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        env = patch.dict(os.environ, {"AGENT_WORKSPACE_ROOT": str(self.root), "AGENT_PROJECT_WORKSPACES": "{}",
            "COMPANION_WS_TOKENS": "token:alice", "ROLES_CONFIG": "", "AGENT_TENANT_ROOT": str(self.root / "tenants"), 'AGENT_TASK_DB':str(self.root/'tasks.db')})
        env.start()
        self.addCleanup(env.stop)
        app.state.agent_runtime = runtime()
        self.addCleanup(lambda: setattr(app.state, "agent_runtime", None))

    def test_fragmented_function_calls_preserve_json_history(self):
        parts = [chunk(calls=[NS(index=0, id="call1", function=NS(name="read_file", arguments='{"pa'))]),
                 chunk(calls=[NS(index=0, id=None, function=NS(name=None, arguments='th":"note.txt"}'))]),
                 chunk(finish="tool_calls")]
        stream = Stream(parts)
        client = NS(chat=NS(completions=NS(create=Mock(return_value=stream))))
        result = streamed_completion(client, "model", [], [], Mock())
        self.assertEqual(json.loads(result.tool_calls[0].function.arguments), {"path": "note.txt"})
        self.assertEqual(result.tool_calls[0].id, "call1")
        json.dumps(result.model_dump(exclude_none=True))
        self.assertTrue(stream.closed.is_set())

    def test_truncated_stream_never_executes_partial_tool(self):
        stream = Stream([chunk(calls=[NS(index=0, id="call1", function=NS(name="read_file", arguments='{"path":'))])])
        client = NS(chat=NS(completions=NS(create=Mock(return_value=stream))))
        with self.assertRaises(RuntimeError):
            streamed_completion(client, "model", [], [], Mock())
        self.assertTrue(stream.closed.is_set())

    def test_real_sdk_reads_local_sse_and_serializes_followup_history(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from openai import OpenAI
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                requests.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                for delta, finish in [({'tool_calls':[{'index':0,'id':'call1','type':'function',
                        'function':{'name':'read_file','arguments':'{"path":"note.txt"}'}}]}, None), ({}, 'tool_calls')]:
                    event = {'id':'local-test','object':'chat.completion.chunk','created':1,'model':'fixture',
                             'choices':[{'index':0,'delta':delta,'finish_reason':finish}]}
                    self.wfile.write(('data: ' + json.dumps(event) + '\n\n').encode())
                self.wfile.write(b'data: [DONE]\n\n')
                self.wfile.flush()
        http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        try:
            with OpenAI(api_key='local-fixture', base_url=f'http://127.0.0.1:{http.server_port}/v1') as client:
                result = streamed_completion(client, 'fixture', [{'role':'user','content':'read'}], [], Mock())
                history = [result.model_dump(exclude_none=True), {'role':'tool','tool_call_id':'call1','content':'result'}]
                streamed_completion(client, 'fixture', history, [], Mock())
            self.assertEqual(requests[1]['messages'][0]['tool_calls'][0]['id'], 'call1')
            self.assertTrue(requests[1]['stream'])
        finally:
            http.shutdown()
            http.server_close()
            thread.join(2)

    def test_websocket_approval_binds_owner_project_and_device(self):
        from agent_execution.approvals import ApprovalStore
        from websocket_server import Session, handle_client_message
        roles = self.root / 'roles.json'
        roles.write_text('{"websocket:alice":"admin"}', encoding='utf-8')
        with patch.dict(os.environ, {'ROLES_CONFIG':str(roles), 'AGENT_APPROVAL_DB':str(self.root/'approvals.db')}):
            store = ApprovalStore()
            key = store.request('websocket:alice', 'default:device', 'default:manage_memory', {'operation':'archive'})
            data = {'type':'approval.decide','payload':{'decision':'approve','approval_id':key}}
            asyncio.run(handle_client_message(Session('device', user_id='alice', project='other'), data))
            self.assertEqual(len(store.pending('websocket:alice', 'default:device')), 1)
            asyncio.run(handle_client_message(Session('device', user_id='alice'), data))
            store.consume(key, 'websocket:alice', 'default:device', 'default:manage_memory', {'operation':'archive'})

    def test_second_chat_is_rejected_while_cleanup_is_running(self):
        from websocket_server import Session, handle_client_message
        async def check():
            session = Session('device')
            session.current_chat_task = asyncio.create_task(asyncio.Event().wait())
            await handle_client_message(session, {'type':'chat.send','payload':{'text':'second'}})
            self.assertEqual(session.outbox[-1]['payload']['code'], 'chat_busy')
            session.current_chat_task.cancel()
            try:
                await session.current_chat_task
            except asyncio.CancelledError:
                pass
        asyncio.run(check())

    def test_actual_agent_tool_request_streams_real_result_over_websocket(self):
        (self.root / "note.txt").write_text("STREAM FIXTURE", encoding="utf-8")
        streams = [Stream([chunk(calls=[NS(index=0, id="call1", function=NS(name="read_file", arguments='{"path":"note.txt"}'))]), chunk(finish="tool_calls")]),
                   Stream([chunk("STREAM "), chunk("FIXTURE"), chunk(finish="stop")])]
        calls = []
        def create(**kwargs):
            self.assertTrue(kwargs["stream"])
            json.dumps(kwargs["messages"])
            calls.append(kwargs)
            return streams.pop(0)
        client = NS(chat=NS(completions=NS(create=create)))
        with patch("server.get_client", return_value=client), TestClient(app).websocket_connect("/ws/v1") as ws:
            ws.send_json({"type":"hello", "payload":{"token":"token", "device_id":"device"}})
            ws.receive_json()
            ws.send_json({"type":"chat.send", "id":"request", "payload":{"text":"read note"}})
            events = []
            while True:
                event = ws.receive_json()
                events.append(event)
                if event["type"] == "chat.end":
                    break
        self.assertEqual(events[0]["type"], "chat.start")
        self.assertIn("tool.start", [event["type"] for event in events])
        result = next(event for event in events if event["type"] == "tool.result")
        self.assertEqual(result["payload"]["result"], "STREAM FIXTURE")
        self.assertEqual(events[-1]["payload"]["text"], "STREAM FIXTURE")
        self.assertFalse(events[-1]["payload"]["interrupted"])
        self.assertEqual(len(calls), 2)
        self.assertTrue(any(message.get("role") == "tool" for message in calls[1]["messages"]))

    def test_interrupt_closes_blocked_stream_before_terminal_event(self):
        started = threading.Event()
        class BlockingStream(Stream):
            def __iter__(self):
                yield chunk("first")
                started.set()
                self.closed.wait(3)
                yield chunk(finish="stop")
        stream = BlockingStream([])
        client = NS(chat=NS(completions=NS(create=Mock(return_value=stream))))
        with patch("server.get_client", return_value=client), TestClient(app).websocket_connect("/ws/v1") as ws:
            ws.send_json({"type":"hello", "payload":{"token":"token", "device_id":"device"}})
            ws.receive_json()
            ws.send_json({"type":"chat.send", "id":"request", "payload":{"text":"hello"}})
            while ws.receive_json()["type"] != "chat.delta":
                pass
            self.assertTrue(started.wait(2))
            ws.send_json({"type":"chat.interrupt", "ref":"request", "payload":{}})
            while True:
                event = ws.receive_json()
                if event["type"] == "chat.end":
                    break
            self.assertTrue(event["payload"]["interrupted"])
            self.assertTrue(stream.closed.is_set())

    def test_fallback_delivers_only_to_configured_owner_target(self):
        with patch.dict(os.environ, {"COMPANION_TELEGRAM_TARGETS": '{"alice:default":"123"}'}), \
             patch("communication_adapters.telegram_adapter.TelegramAdapter") as adapter:
            asyncio.run(send_via_telegram("hello", user_id="alice"))
            outbound = adapter.return_value.send_message.call_args.args[0]
            self.assertEqual(outbound.conversation_id, "123")
            self.assertEqual(outbound.text, "hello")
            with self.assertRaises(ValueError):
                asyncio.run(send_via_telegram("private", user_id="bob"))
            adapter.return_value.send_message.assert_called_once()

    def test_suspended_websocket_task_resumes_after_runtime_restart(self):
        streams = [Stream([chunk(calls=[NS(index=0,id='ask1',function=NS(name='ask_user',arguments='{"question":"which file?"}'))]),chunk(finish='tool_calls')]),
                   Stream([chunk('received note.txt'),chunk(finish='stop')])]
        client=NS(chat=NS(completions=NS(create=Mock(side_effect=lambda **kwargs:streams.pop(0)))))
        with patch('server.get_client',return_value=client):
            with TestClient(app).websocket_connect('/ws/v1') as ws:
                ws.send_json({'type':'hello','payload':{'token':'token','device_id':'device'}});ws.receive_json()
                ws.send_json({'type':'chat.send','id':'first','payload':{'text':'ask for a filename'}})
                while True:
                    event=ws.receive_json()
                    if event['type']=='chat.end':break
                self.assertTrue(event['payload']['suspended'])
                last_seq=event['seq']
            hub.sessions.clear()
            with TestClient(app).websocket_connect('/ws/v1') as ws:
                ws.send_json({'type':'hello','payload':{'token':'token','device_id':'device','last_seq':last_seq}});ws.receive_json()
                ws.send_json({'type':'chat.send','id':'answer','payload':{'text':'note.txt'}})
                while True:
                    event=ws.receive_json()
                    if event['type']=='chat.end':break
                self.assertEqual(event['payload']['text'],'received note.txt')
        self.assertEqual(client.chat.completions.create.call_count,2)
        self.assertTrue(any(m.get('role')=='tool' and m.get('tool_call_id')=='ask1' and m.get('content')=='note.txt' for m in client.chat.completions.create.call_args.kwargs['messages']))

    def test_interrupt_reaches_real_host_tool_process(self):
        roles = self.root / "roles.json"
        roles.write_text('{"websocket:alice":"admin"}', encoding="utf-8")
        command = json.dumps({"command": ["python", "-c", "import time; time.sleep(30)"]})
        stream = Stream([chunk(calls=[NS(index=0, id="call1", function=NS(name="run_command", arguments=command))]), chunk(finish="tool_calls")])
        client = NS(chat=NS(completions=NS(create=Mock(return_value=stream))))
        with patch.dict(os.environ, {"ROLES_CONFIG": str(roles), "AGENT_EXECUTION_MODE": "host"}), \
             patch("base._command_guard", None), patch("server.get_client", return_value=client), \
             TestClient(app).websocket_connect("/ws/v1") as ws:
            ws.send_json({"type":"hello", "payload":{"token":"token", "device_id":"device"}})
            ws.receive_json()
            ws.send_json({"type":"chat.send", "id":"request", "payload":{"text":"run the test command"}})
            while ws.receive_json()["type"] != "tool.start":
                pass
            ws.send_json({"type":"chat.interrupt", "ref":"request", "payload":{}})
            while True:
                event = ws.receive_json()
                if event["type"] == "chat.end":
                    break
            self.assertTrue(event["payload"]["interrupted"])
            self.assertIn("cancelled", event["payload"]["text"])

    @unittest.skipUnless(os.environ.get("PYTHONAI_PROVIDER_TEST") == "1", "opt-in actual provider round trip")
    def test_actual_provider_stream_calls_tool_and_uses_result(self):
        from agent_execution import execution_scope
        from permissions import set_current_role
        marker = "stream-fixture-" + os.urandom(8).hex()
        (self.root / "note.txt").write_text(marker, encoding="utf-8")
        messages = [{"role":"system", "content":"You must call read_file for note.txt before answering. Then return its exact content. Never invent file contents."},
                    {"role":"user", "content":"Read note.txt and return its contents."}]
        schema = next(schema for schema in server.TOOLS_SCHEMAS if schema["function"]["name"] == "read_file")
        events = []
        with patch.object(server, "TOOLS_SCHEMAS", [schema]), set_current_role("viewer"), execution_scope(workspace=self.root):
            reply = server.run_agent(messages, max_retries=0, max_iterations=3,
                                     event_callback=lambda kind, value: events.append((kind, value)))
        self.assertIn(marker, reply)
        self.assertTrue(any(kind == "tool.result" and marker in value["result"] for kind, value in events))
        self.assertTrue(any(kind == "model.delta" for kind, _ in events))
