import importlib.util
import tempfile
import unittest
from pathlib import Path


MOTION_PATH = Path(__file__).resolve().parents[1] / "skills" / "story" / "motion.py"
SPEC = importlib.util.spec_from_file_location("story_motion", MOTION_PATH)
motion = importlib.util.module_from_spec(SPEC)
assert SPEC is not None and SPEC.loader is not None
SPEC.loader.exec_module(motion)


class StoryMotionTests(unittest.TestCase):
    def test_load_story_chapters_reads_story_txt_lines(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            story_path = Path(temp_dir) / "story.txt"
            story_path.write_text("第一段\n\n第二段\n第三段\n", encoding="utf-8")

            chapters = motion.load_story_chapters(story_path)

        self.assertEqual(
            chapters,
            {
                "第1章": "第一段",
                "第2章": "第二段",
                "第3章": "第三段",
            },
        )


if __name__ == "__main__":
    unittest.main()
