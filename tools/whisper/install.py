"""
tools/whisper/install.py — 複製 whisper-cli 二進位與模型到本目錄
執行方式：python tools/whisper/install.py
"""
import shutil
import sys
from pathlib import Path

WHISPER_BUILD = Path(r"C:\Users\andyl\Go\whisper.cpp\build\bin\Release")
MODEL_SRC = Path(r"C:\Users\andyl\Go\whisper.cpp\ggml-base.bin")
DEST = Path(__file__).resolve().parent  # tools/whisper/

BINARIES = [
    "whisper-cli.exe",
    "whisper.dll",
    "ggml.dll",
    "ggml-base.dll",
    "ggml-cpu.dll",
]

def main():
    print("=== Whisper STT 工具安裝 ===")
    print(f"來源目錄: {WHISPER_BUILD}")
    print(f"模型來源: {MODEL_SRC}")
    print(f"目標目錄: {DEST}\n")

    errors = []
    for fname in BINARIES:
        src = WHISPER_BUILD / fname
        dst = DEST / fname
        if not src.exists():
            print(f"  ⚠️  找不到 {src}，跳過。")
            errors.append(fname)
            continue
        shutil.copy2(src, dst)
        size_kb = dst.stat().st_size // 1024
        print(f"  ✅ {fname} ({size_kb} KB)")

    if MODEL_SRC.exists():
        dst = DEST / "ggml-base.bin"
        print(f"\n複製模型（約 142 MB）...")
        shutil.copy2(MODEL_SRC, dst)
        size_mb = dst.stat().st_size / 1024 / 1024
        print(f"  ✅ ggml-base.bin ({size_mb:.1f} MB)")
    else:
        print(f"  ⚠️  模型不存在：{MODEL_SRC}")
        errors.append("ggml-base.bin")

    if errors:
        print(f"\n⚠️  以下檔案未複製：{', '.join(errors)}")
        sys.exit(1)
    else:
        print("\n✅ 安裝完成！")

if __name__ == "__main__":
    main()
