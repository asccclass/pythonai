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


if __name__ == "__main__":
    unittest.main()
