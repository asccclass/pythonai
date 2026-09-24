import unittest
from unittest.mock import patch
import tempfile
from pathlib import Path

import base
import httpx
from openai import APIStatusError
from openai import APITimeoutError
from memory import MemoryStore
import server


class ServerTests(unittest.TestCase):
    def test_create_ollama_client_uses_remote_ollama_settings(self):
        with (
            patch("server.OLLAMA_API_KEY", "sk-test-key"),
            patch("server.OpenAI") as openai,
        ):
            client = server.create_ollama_client()

        self.assertIs(client, openai.return_value)
        openai.assert_called_once_with(
            base_url=server.OLLAMA_BASE_URL,
            api_key="sk-test-key",
        )

    def test_create_ollama_client_rejects_non_litellm_key(self):
        with patch("server.OLLAMA_API_KEY", "not-a-litellm-key"):
            with self.assertRaisesRegex(ValueError, "starts with 'sk-'"):
                server.create_ollama_client()

    def test_format_api_status_error_explains_forbidden_response(self):
        class Error(Exception):
            status_code = 403

        with patch("server.OLLAMA_MODEL", "test-model"):
            message = server.format_api_status_error(Error("nginx forbidden"))

        self.assertIn("HTTP 403", message)
        self.assertIn("OLLAMA_BASE_URL", message)
        self.assertIn("OLLAMA_API_KEY", message)
        self.assertIn("test-model", message)

    def test_format_api_connection_error_explains_timeout(self):
        message = server.format_api_connection_error(Exception("Request timed out."))

        self.assertIn("Could not reach", message)
        self.assertIn("OLLAMA_BASE_URL", message)
        self.assertIn("remote service", message)

    def test_retry_delay_seconds_reads_retry_after_header(self):
        request = httpx.Request("POST", "https://example.test/v1/chat/completions")
        response = httpx.Response(429, headers={"Retry-After": "2.5"}, request=request)
        error = APIStatusError("rate limited", response=response, body={})

        self.assertEqual(server.retry_delay_seconds(error), 2.5)

    def test_get_client_initializes_client_once(self):
        server.client = None
        try:
            with patch("server.create_ollama_client", return_value="client") as create:
                self.assertEqual(server.get_client(), "client")
                self.assertEqual(server.get_client(), "client")

            create.assert_called_once_with()
        finally:
            server.client = None

    def test_run_tool_invokes_registered_tool(self):
        class Function:
            name = "list_files"
            arguments = '{"path": "."}'

        class ToolCall:
            function = Function()

        with patch.dict(base.TOOLS, {"list_files": lambda path: [path]}):
            result = base.run_tool(ToolCall())

        self.assertEqual(result, ["."])

    def test_run_agent_returns_assistant_content(self):
        class Message:
            tool_calls = None
            content = "hello"

        class Choice:
            message = Message()

        class Response:
            choices = [Choice()]

        class Completions:
            def create(self, **kwargs):
                return Response()

        class Chat:
            completions = Completions()

        class Client:
            chat = Chat()

        messages = [{"role": "user", "content": "hi"}]

        with patch("server.get_client", return_value=Client()):
            result = server.run_agent(messages)

        self.assertEqual(result, "hello")
        self.assertIs(messages[-1], Choice.message)

    def test_run_agent_retries_transient_status_error(self):
        class Message:
            tool_calls = None
            content = "hello after retry"

        class Choice:
            message = Message()

        class Response:
            choices = [Choice()]

        class Completions:
            def __init__(self):
                self.calls = 0

            def create(self, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    request = httpx.Request("POST", "https://example.test/v1/chat/completions")
                    response = httpx.Response(429, headers={"Retry-After": "0"}, request=request)
                    raise APIStatusError("rate limited", response=response, body={})
                return Response()

        class Chat:
            def __init__(self):
                self.completions = Completions()

        class Client:
            def __init__(self):
                self.chat = Chat()

        sleeps = []
        client = Client()
        messages = [{"role": "user", "content": "hi"}]

        with (
            patch("server.get_client", return_value=client),
            patch("builtins.print"),
        ):
            result = server.run_agent(messages, sleep=sleeps.append)

        self.assertEqual(result, "hello after retry")
        self.assertEqual(client.chat.completions.calls, 2)
        self.assertEqual(sleeps, [0.0])

    def test_run_agent_logs_tool_call_and_result(self):
        class Function:
            name = "list_files"
            arguments = '{"path": "."}'

        class ToolCall:
            id = "tool-1"
            function = Function()

        class ToolMessage:
            tool_calls = [ToolCall()]
            content = None

        class FinalMessage:
            tool_calls = None
            content = "done"

        class Choice:
            def __init__(self, message):
                self.message = message

        class Response:
            def __init__(self, message):
                self.choices = [Choice(message)]

        class Completions:
            def __init__(self):
                self.responses = [Response(ToolMessage()), Response(FinalMessage())]

            def create(self, **kwargs):
                return self.responses.pop(0)

        class Chat:
            def __init__(self):
                self.completions = Completions()

        class Client:
            def __init__(self):
                self.chat = Chat()

        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.db")
            episode_id = store.start_episode()
            with (
                patch("server.get_client", return_value=Client()),
                patch("server.run_tool", return_value=["sample.txt"]),
            ):
                result = server.run_agent([{"role": "user", "content": "list"}], memory=store, episode_id=episode_id)

            events = store.recent_events(limit=10)

        self.assertEqual(result, "done")
        self.assertEqual([event["event_type"] for event in events], ["tool_result", "tool_call"])
        self.assertEqual(events[1]["metadata"]["name"], "list_files")
        self.assertEqual(events[0]["metadata"]["tool_call_id"], "tool-1")
        self.assertEqual(events[0]["content"], "['sample.txt']")

    def test_read_user_input_treats_ctrl_c_as_exit(self):
        with patch("builtins.input", side_effect=KeyboardInterrupt):
            self.assertEqual(server.read_user_input(), "exit")

    def test_main_logs_episode_memory_for_completed_turn(self):
        class Guard:
            def assess(self, user_input):
                return server.GuardDecision(intent="chat", risk=0.1, needs_confirmation=False)

        class Classifier:
            def assess_episode(self, events):
                return server.MemoryCandidateDecision(memory_kind="semantic", should_extract=True, confidence=0.8)

        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.db")
            with (
                patch("server.LayaGuard", return_value=Guard()),
                patch("server.LayaMemoryClassifier", return_value=Classifier()),
                patch("server.MemoryStore", return_value=store),
                patch("server.read_user_input", side_effect=["hello", "exit"]),
                patch("server.run_agent", return_value="hi"),
                patch("builtins.print"),
            ):
                server.main(async_memory_review=False)

            events = store.recent_events(limit=10)

        event_types = [event["event_type"] for event in events]
        self.assertEqual(
            event_types,
            ["memory_review_result", "memory_review_candidate", "memory_candidate_decision", "message", "guard_decision", "message"],
        )
        self.assertEqual(events[0]["metadata"]["memory_kind"], "semantic")
        self.assertEqual(events[1]["metadata"]["memory_kind"], "semantic")
        self.assertEqual(events[2]["metadata"]["candidate"]["memory_kind"], "semantic")
        self.assertEqual(events[3]["role"], "assistant")
        self.assertEqual(events[3]["content"], "hi")
        self.assertEqual(events[4]["metadata"]["guard"]["intent"], "chat")
        self.assertEqual(events[5]["role"], "user")
        self.assertEqual(events[5]["content"], "hello")

    def test_main_continues_when_memory_classifier_fails(self):
        class Guard:
            def assess(self, user_input):
                return server.GuardDecision(intent="chat", risk=0.1, needs_confirmation=False)

        class Classifier:
            def assess_episode(self, events):
                raise RuntimeError("classifier failed")

        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.db")
            with (
                patch("server.LayaGuard", return_value=Guard()),
                patch("server.LayaMemoryClassifier", return_value=Classifier()),
                patch("server.MemoryStore", return_value=store),
                patch("server.read_user_input", side_effect=["hello", "exit"]),
                patch("server.run_agent", return_value="hi") as run_agent,
                patch("builtins.print"),
            ):
                server.main(async_memory_review=False)

            events = store.recent_events(limit=10)

        run_agent.assert_called_once()
        self.assertEqual(events[0]["event_type"], "memory_candidate_decision")
        self.assertFalse(events[0]["metadata"]["candidate"]["available"])
        self.assertEqual(events[0]["metadata"]["candidate"]["reason"], "Memory classifier failed")

    def test_main_logs_working_memory_summary_when_context_is_compacted(self):
        class Guard:
            def assess(self, user_input):
                return server.GuardDecision(intent="chat", risk=0.1, needs_confirmation=False)

        compacted = [{"role": "system", "content": "summary"}, {"role": "user", "content": "hello"}]

        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.db")
            with (
                patch("server.LayaGuard", return_value=Guard()),
                patch("server.MemoryStore", return_value=store),
                patch("server.read_user_input", side_effect=["hello", "exit"]),
                patch("server.compact_messages", return_value=(compacted, "old context", None)),
                patch("server.run_agent", return_value="hi"),
                patch("builtins.print"),
            ):
                server.main()

            events = store.recent_events(limit=10)

        self.assertIn("working_memory_summary", [event["event_type"] for event in events])
        summary_events = [event for event in events if event["event_type"] == "working_memory_summary"]
        self.assertEqual(summary_events[0]["content"], "old context")

    def test_main_injects_retrieved_memory_context_into_agent_messages(self):
        class Guard:
            def assess(self, user_input):
                return server.GuardDecision(intent="chat", risk=0.1, needs_confirmation=False)

        captured_messages = []

        def run_agent(messages, memory=None, episode_id=None):
            captured_messages.extend(messages)
            return "hi"

        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.db")
            with (
                patch("server.LayaGuard", return_value=Guard()),
                patch("server.MemoryStore", return_value=store),
                patch("server.read_user_input", side_effect=["hello", "exit"]),
                patch("server.build_memory_context", return_value="Relevant long-term memory:\n- user prefers Python"),
                patch("server.run_agent", side_effect=run_agent),
                patch("builtins.print"),
            ):
                server.main()

            events = store.recent_events(limit=10)

        self.assertIn(
            {"role": "system", "content": "Relevant long-term memory:\n- user prefers Python"},
            captured_messages,
        )
        self.assertIn("retrieval_context", [event["event_type"] for event in events])

    def test_main_continues_when_memory_store_cannot_initialize(self):
        class Guard:
            def assess(self, user_input):
                return server.GuardDecision(intent="chat", risk=0.1, needs_confirmation=False)

        with (
            patch("server.LayaGuard", return_value=Guard()),
            patch("server.MemoryStore", side_effect=RuntimeError("database unavailable")),
            patch("server.read_user_input", side_effect=["hello", "exit"]),
            patch("server.run_agent", return_value="hi") as run_agent,
            patch("builtins.print"),
        ):
            server.main()

        run_agent.assert_called_once()

    def test_main_handles_api_timeout_without_traceback(self):
        class Guard:
            def assess(self, user_input):
                return server.GuardDecision(intent="chat", risk=0.1, needs_confirmation=False)

        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.db")
            with (
                patch("server.LayaGuard", return_value=Guard()),
                patch("server.MemoryStore", return_value=store),
                patch("server.read_user_input", side_effect=["hello"]),
                patch("server.run_agent", side_effect=APITimeoutError(request=None)),
                patch("builtins.print") as print_mock,
            ):
                server.main()

            events = store.recent_events(limit=10)

        self.assertEqual(events[0]["event_type"], "error")
        self.assertEqual(events[0]["metadata"]["error_type"], "APITimeoutError")
        printed = "\n".join(str(call.args[0]) for call in print_mock.call_args_list if call.args)
        self.assertIn("Could not reach", printed)

    def test_main_continues_when_memory_write_fails(self):
        class Guard:
            def assess(self, user_input):
                return server.GuardDecision(intent="chat", risk=0.1, needs_confirmation=False)

        class BrokenMemory:
            def start_episode(self):
                return 1

            def add_event(self, *args, **kwargs):
                raise RuntimeError("write failed")

            def finish_episode(self, *args, **kwargs):
                raise RuntimeError("finish failed")

            def active_semantic_memories(self):
                return []

        with (
            patch("server.LayaGuard", return_value=Guard()),
            patch("server.MemoryStore", return_value=BrokenMemory()),
            patch("server.read_user_input", side_effect=["hello", "exit"]),
            patch("server.run_agent", return_value="hi") as run_agent,
            patch("builtins.print"),
        ):
            server.main()

        run_agent.assert_called_once()

    def test_format_guard_notice_warns_when_laya_unavailable(self):
        decision = server.GuardDecision(available=False, reason="missing package")

        notice = server.format_guard_notice(decision)

        self.assertIn("continuing without guard", notice)
        self.assertIn("missing package", notice)

    def test_format_guard_notice_warns_for_risky_request(self):
        decision = server.GuardDecision(
            intent="run_command",
            risk=1.5,
            needs_confirmation=True,
        )

        notice = server.format_guard_notice(decision)

        self.assertIn("intent=run_command", notice)
        self.assertIn("needs_confirmation=True", notice)

    def test_format_guard_notice_is_empty_for_low_risk_request(self):
        decision = server.GuardDecision(intent="chat", risk=0.2, needs_confirmation=False)

        self.assertEqual(server.format_guard_notice(decision), "")


if __name__ == "__main__":
    unittest.main()
