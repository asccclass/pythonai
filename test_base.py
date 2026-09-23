import os
import tempfile
import unittest
from pathlib import Path

from base import list_files, load_dotenv, read_file, run_command, write_file


class BaseTests(unittest.TestCase):
    def test_read_file_returns_text(self):
        with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as temp_file:
            temp_file.write("hello")
            path = Path(temp_file.name)

        try:
            self.assertEqual(read_file(path), "hello")
        finally:
            path.unlink()

    def test_list_files_returns_names(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir)
            (path / "sample.txt").write_text("hello", encoding="utf-8")

            self.assertIn("sample.txt", list_files(path))

    def test_write_file_writes_text(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "sample.txt"

            write_file(path, "hello")

            self.assertEqual(path.read_text(encoding="utf-8"), "hello")

    def test_run_command_returns_completed_process(self):
        result = run_command(["python", "-c", "print('hello')"])

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "hello")

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
