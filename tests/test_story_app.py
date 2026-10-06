import importlib.util
import tempfile
import unittest
from pathlib import Path


STORY_APP_PATH = Path(__file__).resolve().parents[1] / "skills" / "story" / "story.py"
story_app = None


def load_story_app():
    global story_app
    if story_app is None:
        spec = importlib.util.spec_from_file_location("story_app", STORY_APP_PATH)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        story_app = module
    return story_app


class ComputationalNarratologyAppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.story_app = load_story_app()

    def test_load_and_split_novel_reads_and_chunks_text(self):
        app = self.story_app.ComputationalNarratologyApp.__new__(self.story_app.ComputationalNarratologyApp)

        with tempfile.TemporaryDirectory() as temp_dir:
            novel_path = Path(temp_dir) / "novel.txt"
            novel_path.write_text("甲乙丙\n丁戊己", encoding="utf-8")

            chunks = app.load_and_split_novel(str(novel_path), chunk_size=3)

        self.assertEqual(chunks, ["甲乙丙", "丁戊己"])


if __name__ == "__main__":
    unittest.main()
