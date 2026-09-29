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

    def test_extract_chapter_characters_keeps_only_people(self):
        story_ner = [
            [
                Entity("張小明", "PERSON", (0, 3)),
                Entity("平安村", "GPE", (4, 7)),
            ],
            [
                Entity("李大叔", "PERSON", (0, 3)),
                Entity("黑龍會", "ORG", (4, 7)),
            ],
        ]

        chapter_characters = motion.extract_chapter_characters(story_ner)

        self.assertEqual(chapter_characters, [["張小明"], ["李大叔"]])

    def test_build_edges_data_returns_relation_edge_tuples(self):
        chapter_characters = [
            ["張小明", "張小華"],
            ["張小明", "李大叔"],
            ["李大叔", "張小華", "張小明"],
        ]

        edges_data = motion.build_edges_data(chapter_characters)

        self.assertEqual(
            edges_data,
            [
                ("張小明", "張小華", 2),
                ("張小明", "李大叔", 2),
                ("張小華", "李大叔", 1),
            ],
        )

    def test_analyze_character_centrality_returns_ranked_table(self):
        graph = motion.build_relation_graph([
            ["張小明", "張小華"],
            ["張小明", "李大叔"],
            ["李大叔", "張小華", "張小明"],
        ])

        df = motion.analyze_character_centrality(graph)

        self.assertEqual(
            list(df.columns),
            [
                "角色名稱",
                "加權度中心性 (互動廣度)",
                "加權中介中心性 (情節橋樑)",
                "加權特徵向量中心性 (影響力)",
            ],
        )
        self.assertEqual(set(df["角色名稱"]), {"張小明", "張小華", "李大叔"})

    def test_analyze_character_centrality_uses_edge_weights(self):
        graph = motion.nx.Graph()
        graph.add_edge("主角", "夥伴", weight=10)
        graph.add_edge("主角", "配角", weight=1)
        graph.add_edge("夥伴", "配角", weight=1)

        df = motion.analyze_character_centrality(graph)
        rows = {row["角色名稱"]: row for row in df.to_dict("records")}

        self.assertEqual(rows["主角"]["加權度中心性 (互動廣度)"], 1.0)
        self.assertEqual(rows["夥伴"]["加權度中心性 (互動廣度)"], 1.0)
        self.assertEqual(rows["配角"]["加權度中心性 (互動廣度)"], 0.182)
        self.assertGreater(
            rows["主角"]["加權特徵向量中心性 (影響力)"],
            rows["配角"]["加權特徵向量中心性 (影響力)"],
        )

    def test_draw_relation_graph_saves_output_file(self):
        graph = motion.build_relation_graph([["張小明", "張小華"]])

        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "relation_graph.png"
            result = motion.draw_relation_graph(graph, output_path=output_path)

            self.assertEqual(result, output_path)
            self.assertTrue(output_path.exists())
            self.assertGreater(output_path.stat().st_size, 0)

    def test_build_sentiment_relation_graph_accumulates_edge_sentiment(self):
        chapter_characters = [
            ["張小明", "張小華"],
            ["張小明", "張小華", "李大叔"],
            ["張小明"],
        ]
        sentiment_scores = [-0.6, 0.4, 0.9]

        graph = motion.build_sentiment_relation_graph(chapter_characters, sentiment_scores)

        self.assertEqual(graph["張小明"]["張小華"]["interactions"], 2)
        self.assertAlmostEqual(graph["張小明"]["張小華"]["total_sentiment"], -0.2)
        self.assertEqual(graph["張小明"]["李大叔"]["interactions"], 1)
        self.assertFalse(graph.has_node("孤立角色"))

    def test_analyze_character_sentiment_traits_labels_roles(self):
        graph = motion.nx.Graph()
        graph.add_edge("李大叔", "張小明", total_sentiment=0.8, interactions=1)
        graph.add_edge("黑龍會", "張小明", total_sentiment=-0.8, interactions=1)
        graph.add_edge("張小華", "張小明", total_sentiment=0.0, interactions=1)

        df = motion.analyze_character_sentiment_traits(graph)
        rows = {row["角色名稱"]: row for row in df.to_dict("records")}

        self.assertEqual(
            list(df.columns),
            ["角色名稱", "總互動次數", "情感投射分數", "敘事角色診斷"],
        )
        self.assertEqual(rows["李大叔"]["敘事角色診斷"], "正向角色 / 救贖者")
        self.assertEqual(rows["黑龍會"]["敘事角色診斷"], "負向角色 / 衝突源")
        self.assertEqual(rows["張小華"]["敘事角色診斷"], "中性角色 / 受害者")
        self.assertGreater(
            rows["李大叔"]["情感投射分數"],
            rows["黑龍會"]["情感投射分數"],
        )


if __name__ == "__main__":
    unittest.main()
