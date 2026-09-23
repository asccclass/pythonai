import os
import tempfile
import unittest
from pathlib import Path

from base import load_dotenv, read_file


class BaseTests(unittest.TestCase):
    def test_read_file_returns_text(self):
        with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as temp_file:
            temp_file.write("hello")
            path = Path(temp_file.name)

        try:
            self.assertEqual(read_file(path), "hello")
        finally:
            path.unlink()

    def test_load_dotenv_sets_missing_values(self):
        with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as env_file:
            env_file.write("TEST_DOTENV_VALUE=from-file\n")
            env_path = Path(env_file.name)

        try:
            os.environ.pop("TEST_DOTENV_VALUE", None)
            load_dotenv(env_path)
            self.assertEqual(os.environ["TEST_DOTENV_VALUE"], "from-file")
        finally:
            os.environ.pop("TEST_DOTENV_VALUE", None)
            env_path.unlink()


if __name__ == "__main__":
    unittest.main()
