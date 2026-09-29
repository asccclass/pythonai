import importlib.util
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path


MOTION_PATH = Path(__file__).resolve().parents[1] / "skills" / "story" / "motion.py"
SPEC = importlib.util.spec_from_file_location("story_motion", MOTION_PATH)
motion = importlib.util.module_from_spec(SPEC)
assert SPEC is not None and SPEC.loader is not None
SPEC.loader.exec_module(motion)


@dataclass
class Entity:
    word: str
    ner: str
    idx: tuple[int, int]


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

    def test_merge_tokens_with_entities_keeps_person_name_together(self):
        text = "張小明遇到了李大叔"
        tokens = ["張小明", "遇到", "了", "李", "大叔"]
        ner = [
            Entity("張小明", "PERSON", (0, 3)),
            Entity("李大叔", "PERSON", (6, 9)),
        ]

        merged_tokens = motion.merge_tokens_with_entities(text, tokens, ner)

        self.assertEqual(merged_tokens, ["張小明", "遇到", "了", "李大叔"])
        self.assertNotIn("李", merged_tokens)
        self.assertNotIn("大叔", merged_tokens)


if __name__ == "__main__":
    unittest.main()
