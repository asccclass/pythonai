import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


RELATION_PATH = Path(__file__).resolve().parents[1] / "skills" / "story" / "relation.py"
SPEC = importlib.util.spec_from_file_location("story_relation", RELATION_PATH)
relation = importlib.util.module_from_spec(SPEC)
assert SPEC is not None and SPEC.loader is not None
SPEC.loader.exec_module(relation)


class StoryRelationTests(unittest.TestCase):
    def test_ensure_ckip_ner_model_downloads_to_models_ckip(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            model_dir = Path(temp_dir) / "models" / "ckip" / "albert-tiny-chinese-ner"

            with patch.object(relation, "snapshot_download") as snapshot_download:
                result = relation.ensure_ckip_ner_model(model_dir)

        self.assertEqual(result, model_dir)
        snapshot_download.assert_called_once_with(
            repo_id=relation.CKIP_NER_REPO_ID,
            local_dir=str(model_dir),
            local_dir_use_symlinks=False,
        )

    def test_create_ner_driver_loads_from_local_model_path(self):
        with (
            patch.object(relation, "ensure_ckip_ner_model", return_value=Path("models/ckip/albert-tiny-chinese-ner")),
            patch.object(relation, "CkipNerChunker") as ner_chunker,
        ):
            relation.create_ner_driver(device=-1)

        ner_chunker.assert_called_once_with(
            model_name=str(Path("models/ckip/albert-tiny-chinese-ner")),
            device=-1,
        )

    def test_build_edges_data_returns_x_py_edge_tuples(self):
        chapter_characters = [
            ["張小明", "張小華"],
            ["張小明", "李大叔"],
            ["李大叔", "張小華", "張小明"],
        ]

        edges_data = relation.build_edges_data(chapter_characters)

        self.assertEqual(
            edges_data,
            [
                ("張小明", "張小華", 2),
                ("張小明", "李大叔", 2),
                ("張小華", "李大叔", 1),
            ],
        )

    def test_analyze_character_centrality_returns_ranked_table(self):
        graph = relation.build_relation_graph([
            ["張小明", "張小華"],
            ["張小明", "李大叔"],
            ["李大叔", "張小華", "張小明"],
        ])

        df = relation.analyze_character_centrality(graph)

        self.assertEqual(
            list(df.columns),
            [
                "角色名稱",
                "度中心性 (社交廣度)",
                "中介中心性 (情節橋樑)",
                "特徵向量中心性 (影響力)",
            ],
        )
        self.assertEqual(set(df["角色名稱"]), {"張小明", "張小華", "李大叔"})

    def test_draw_relation_graph_saves_output_file(self):
        graph = relation.build_relation_graph([["張小明", "張小華"]])

        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "relation_graph.png"
            result = relation.draw_relation_graph(graph, output_path=output_path)

            self.assertEqual(result, output_path)
            self.assertTrue(output_path.exists())
            self.assertGreater(output_path.stat().st_size, 0)


if __name__ == "__main__":
    unittest.main()
