import unittest
from unittest.mock import patch

import server


class ServerTests(unittest.TestCase):
    def test_create_ollama_client_uses_remote_ollama_settings(self):
        with patch("server.OpenAI") as openai:
            client = server.create_ollama_client()

        self.assertIs(client, openai.return_value)
        openai.assert_called_once_with(
            base_url=server.OLLAMA_BASE_URL,
            api_key=server.OLLAMA_API_KEY,
        )

    def test_run_tool_invokes_registered_tool(self):
        class Function:
            name = "list_files"
            arguments = '{"path": "."}'

        class ToolCall:
            function = Function()

        with patch.dict(server.TOOLS, {"list_files": lambda path: [path]}):
            result = server.run_tool(ToolCall())

        self.assertEqual(result, ["."])


if __name__ == "__main__":
    unittest.main()
