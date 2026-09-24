import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

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

    def test_root_endpoint_lists_available_routes(self):
        response = TestClient(server.app).get("/")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "ok")
        self.assertIn("/v1/models", data["endpoints"])
        self.assertIn("/v1/chat/completions", data["endpoints"])

    def test_health_endpoint_returns_ok(self):
        response = TestClient(server.app).get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_models_endpoint_returns_openai_compatible_model_list(self):
        with patch("server.OLLAMA_MODEL", "test-model"):
            response = TestClient(server.app).get("/v1/models")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["object"], "list")
        self.assertEqual(data["data"][0]["id"], "test-model")
        self.assertEqual(data["data"][0]["object"], "model")

    def test_chat_completions_endpoint_returns_openai_compatible_response(self):
        payload = {
            "model": "test-model",
            "messages": [{"role": "user", "content": "hi"}],
        }

        with patch("server.run_agent", return_value="hello") as run_agent:
            response = TestClient(server.app).post("/v1/chat/completions", json=payload)

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["object"], "chat.completion")
        self.assertEqual(data["model"], "test-model")
        self.assertEqual(data["choices"][0]["message"]["role"], "assistant")
        self.assertEqual(data["choices"][0]["message"]["content"], "hello")
        self.assertEqual(data["choices"][0]["finish_reason"], "stop")
        run_agent.assert_called_once_with([{"role": "user", "content": "hi"}])

    def test_chat_completions_endpoint_rejects_streaming(self):
        payload = {
            "model": "test-model",
            "stream": True,
            "messages": [{"role": "user", "content": "hi"}],
        }

        response = TestClient(server.app).post("/v1/chat/completions", json=payload)

        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
