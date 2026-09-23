import tempfile
import unittest
from pathlib import Path

from base import read_file


class BaseTests(unittest.TestCase):
    def test_read_file_returns_text(self):
        with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as temp_file:
            temp_file.write("hello")
            path = Path(temp_file.name)

        try:
            self.assertEqual(read_file(path), "hello")
        finally:
            path.unlink()


if __name__ == "__main__":
    unittest.main()
