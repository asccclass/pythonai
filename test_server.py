import json
import os
import tempfile
import unittest
from unittest.mock import patch

import server


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return b'{"message":{"role":"assistant","content":"ok"}}'


class ServerTests(unittest.TestCase):
    def test_load_dotenv_sets_missing_values(self):
        with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as env_file:
            env_file.write("TEST_DOTENV_VALUE=from-file\n")
            env_path = env_file.name

        try:
            os.environ.pop("TEST_DOTENV_VALUE", None)
            server.load_dotenv(env_path)
            self.assertEqual(os.environ["TEST_DOTENV_VALUE"], "from-file")
        finally:
            os.environ.pop("TEST_DOTENV_VALUE", None)
            os.remove(env_path)

    def test_chat_with_ollama_posts_expected_payload_and_headers(self):
        messages = [{"role": "user", "content": "hello"}]

        with patch("server.urllib.request.urlopen", return_value=FakeResponse()) as urlopen:
            result = server.chat_with_ollama(
                messages,
                base_url="https://example.test/",
                model="test-model",
                api_key="test-key",
            )

        request = urlopen.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))

        self.assertEqual(result["message"]["content"], "ok")
        self.assertEqual(request.full_url, "https://example.test/api/chat")
        self.assertEqual(payload["model"], "test-model")
        self.assertEqual(payload["messages"], messages)
        self.assertFalse(payload["stream"])
        self.assertEqual(request.headers["Authorization"], "Bearer test-key")
        self.assertEqual(request.headers["X-api-key"], "test-key")


if __name__ == "__main__":
    unittest.main()
