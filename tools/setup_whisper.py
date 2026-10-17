"""
setup_whisper.py - 複製 whisper-cli.exe 及必要 DLL 到本專案工具目錄。
執行方式：python tools/setup_whisper.py
"""
import shutil
from pathlib import Path

SRC_DIR = Path(r"C:\Users\andyl\Go\whisper.cpp\build\bin\Release")
SRC_MODEL = Path(r"C:\Users\andyl\Go\whisper.cpp\ggml-base.bin")

DST_BIN = Path(__file__).parent / "whisper" / "bin"
DST_BIN.mkdir(parents=True, exist_ok=True)

FILES = [
    "whisper-cli.exe",
    "ggml.dll",
    "ggml-base.dll",
    "ggml-cpu.dll",
    "whisper.dll",
]

errors = []
for f in FILES:
    src = SRC_DIR / f
    dst = DST_BIN / f
    if not src.exists():
        errors.append(f"  [MISS] {src}")
        continue
    shutil.copy2(src, dst)
    print(f"  [OK]  {f} -> {dst}")

# 複製模型（選用，模型較大可略過，工具會 fallback 到原始路徑）
if SRC_MODEL.exists():
    model_dst = Path(__file__).parent / "whisper" / "ggml-base.bin"
    size_mb = SRC_MODEL.stat().st_size / 1024 / 1024
    print(f"\n  模型檔案大小: {size_mb:.1f} MB")
    ans = input("  是否也複製模型到 tools/whisper/？(y/N) ").strip().lower()
    if ans == "y":
        shutil.copy2(SRC_MODEL, model_dst)
        print(f"  [OK]  ggml-base.bin -> {model_dst}")
    else:
        print("  [SKIP] 模型將使用原始路徑（工具自動 fallback）")
else:
    print(f"\n  [WARN] 找不到模型: {SRC_MODEL}")

if errors:
    print("\n以下檔案複製失敗：")
    for e in errors:
        print(e)
else:
    print("\n✅ 完成！工具目錄：", DST_BIN)
