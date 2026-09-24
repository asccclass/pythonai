"""Download only the pinned multilingual Laya checkpoint."""

import hashlib
import json
from pathlib import Path

from huggingface_hub import snapshot_download


ROOT = Path(__file__).resolve().parent
REPO = "convaiinnovations/laya-multilingual"
MODEL_DIR = ROOT / "models" / "laya-multilingual"
ALLOW_PATTERNS = [
    "model.safetensors",
    "rl_agent_config.json",
    "encoder/*",
    "tokenizer/*",
]


if __name__ == "__main__":
    snapshot_download(REPO, local_dir=MODEL_DIR, allow_patterns=ALLOW_PATTERNS)
    files = []
    for path in sorted(MODEL_DIR.rglob("*")):
        if not path.is_file() or ".cache" in path.parts:
            continue
        with path.open("rb") as file:
            digest = hashlib.file_digest(file, "sha256").hexdigest()
        files.append(
            {
                "path": str(path.relative_to(MODEL_DIR)),
                "bytes": path.stat().st_size,
                "sha256": digest,
            }
        )
    receipt = {
        "repo": REPO,
        "model_dir": str(MODEL_DIR),
        "files": files,
        "total_bytes": sum(file["bytes"] for file in files),
    }
    (ROOT / "download-receipt.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    print("Multilingual checkpoint ready:", MODEL_DIR)
    print("Downloaded model files:", receipt["total_bytes"], "bytes")
