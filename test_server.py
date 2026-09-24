import unittest
from unittest.mock import patch

import base
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
