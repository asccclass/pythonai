import unittest
from unittest.mock import patch

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

        with patch.dict(server.TOOLS, {"list_files": lambda path: [path]}):
            result = server.run_tool(ToolCall())

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


if __name__ == "__main__":
    unittest.main()
