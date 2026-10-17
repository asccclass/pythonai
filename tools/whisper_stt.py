"""Speech-to-Text tool using whisper-cli.exe (whisper.cpp)."""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from agent_execution import policy_tool, resolve_path
from agent_execution.process import run_process
from agent_execution.results import ToolResult

# --- 路徑設定 ---
# 優先用放在本工具旁邊的 bin/ 目錄，找不到才 fallback 到原始建置路徑
_TOOL_DIR = Path(__file__).parent / "whisper" / "bin"
_FALLBACK_DIR = Path(r"C:\Users\andyl\Go\whisper.cpp\build\bin\Release")
_MODEL_PATHS = [
    Path(r"C:\Users\andyl\Go\whisper.cpp\ggml-base.bin"),
    Path(__file__).parent / "whisper" / "ggml-base.bin",
]


def _find_exe() -> Path | None:
    for directory in (_TOOL_DIR, _FALLBACK_DIR):
        exe = directory / "whisper-cli.exe"
        if exe.exists():
            return exe
    return None


def _find_model() -> Path | None:
    for p in _MODEL_PATHS:
        if p.exists():
            return p
    return None


@policy_tool("whisper_stt")
def whisper_stt(
    audio_file: str,
    language: str = "auto",
    output_format: str = "txt",
    threads: int = 4,
    model: str = "",
) -> ToolResult:
    """
    使用 whisper-cli.exe 對音訊檔案進行語音辨識（STT）。
    支援 wav/mp3/flac/ogg 等格式。
    """
    # 1. 找 exe
    exe = _find_exe()
    if exe is None:
        return ToolResult(
            False,
            error=(
                "找不到 whisper-cli.exe。"
                f"請確認 {_TOOL_DIR} 或 {_FALLBACK_DIR} 中存在該檔案。"
            ),
        )

    # 2. 找模型
    if model:
        model_path = Path(model)
        if not model_path.is_absolute():
            model_path = resolve_path(model)
    else:
        model_path = _find_model()

    if model_path is None or not model_path.exists():
        return ToolResult(
            False,
            error=(
                f"找不到 Whisper 模型檔案。"
                f"請確認模型存在於: {[str(p) for p in _MODEL_PATHS]}"
            ),
        )

    # 3. 解析音訊檔案路徑（音訊可在 workspace 外，絕對路徑直接用）
    audio_path = Path(audio_file) if Path(audio_file).is_absolute() else resolve_path(audio_file)
    if not audio_path.exists():
        return ToolResult(False, error=f"音訊檔案不存在: {audio_path}")

    # 4. 驗證 output_format
    valid_formats = {"txt", "vtt", "srt", "lrc", "csv", "json"}
    if output_format not in valid_formats:
        return ToolResult(
            False,
            error=f"不支援的輸出格式 '{output_format}'，請使用: {', '.join(sorted(valid_formats))}",
        )

    # 5. 建立暫存輸出目錄
    with tempfile.TemporaryDirectory(prefix="whisper-stt-") as tmp_dir:
        tmp_path = Path(tmp_dir)
        out_stem = tmp_path / "output"

        # 6. 組建命令列
        cmd = [
            str(exe),
            "--model", str(model_path),
            "--file", str(audio_path),
            "--output-file", str(out_stem),
            f"--output-{output_format}",
            "--threads", str(threads),
            "--print-colors", "false",
        ]

        if language != "auto":
            cmd += ["--language", language]

        # 把 exe 所在目錄加進 PATH，讓 DLL 可以被找到
        env = os.environ.copy()
        env["PATH"] = str(exe.parent) + os.pathsep + env.get("PATH", "")

        try:
            process = run_process(
                cmd,
                capture_output=True,
                text=True,
                timeout=300,
                env=env,
            )
        except Exception as exc:
            return ToolResult(False, error=f"執行 whisper-cli 失敗: {exc}")

        stdout = (process.stdout or "").strip()
        stderr = (process.stderr or "").strip()

        # 7. 讀取輸出檔案
        out_file = tmp_path / f"output.{output_format}"
        transcription = ""
        if out_file.exists():
            transcription = out_file.read_text(encoding="utf-8", errors="replace").strip()

        # 若檔案不存在但有 stdout，也試著抓
        if not transcription and stdout:
            transcription = stdout

        if process.returncode != 0:
            return ToolResult(
                False,
                output=stderr or stdout,
                exit_code=process.returncode,
                error=f"whisper-cli 回傳錯誤碼 {process.returncode}",
                evidence={
                    "kind": "stt_process",
                    "audio_file": str(audio_path),
                    "model": str(model_path),
                    "language": language,
                    "format": output_format,
                },
            )

        return ToolResult(
            True,
            output=transcription,
            exit_code=process.returncode,
            evidence={
                "kind": "stt_process",
                "audio_file": str(audio_path),
                "model": str(model_path),
                "language": language,
                "format": output_format,
                "transcription_length": len(transcription),
            },
        )
