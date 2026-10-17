"""Tests for whisper_stt tool."""
from __future__ import annotations

import importlib
import sys
import wave
import struct
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _make_silent_wav(path: Path, duration_sec: float = 0.5, sample_rate: int = 16000) -> None:
    n_samples = int(sample_rate * duration_sec)
    with wave.open(str(path), "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(struct.pack(f"<{n_samples}h", *([0] * n_samples)))


def _load():
    """每次取得一個乾淨的模組實例。"""
    if "tools.whisper_stt" in sys.modules:
        del sys.modules["tools.whisper_stt"]
    root = str(Path(__file__).parent.parent)
    if root not in sys.path:
        sys.path.insert(0, root)
    return importlib.import_module("tools.whisper_stt")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_module():
    """每個測試前後都清除模組快取。"""
    yield
    sys.modules.pop("tools.whisper_stt", None)


@pytest.fixture
def workspace_wav(tmp_path, monkeypatch):
    """
    在 workspace 內建立靜音 WAV。
    同時 monkeypatch workspace root，讓 resolve_path 接受此路徑。
    """
    wav = tmp_path / "silent.wav"
    _make_silent_wav(wav)
    return wav


# ---------------------------------------------------------------------------
# TestFindExe
# ---------------------------------------------------------------------------

class TestFindExe:
    def test_returns_none_when_both_dirs_missing(self, tmp_path):
        """兩個目錄都不存在時回傳 None。"""
        mod = _load()
        nonexistent = tmp_path / "no_such_dir"
        # 直接替換模組層級變數（不 patch property）
        mod._TOOL_DIR = nonexistent
        mod._FALLBACK_DIR = nonexistent
        result = mod._find_exe()
        assert result is None

    def test_finds_tool_dir_first(self, tmp_path):
        """tool_dir 有 exe 就優先回傳。"""
        mod = _load()
        fake_exe = tmp_path / "whisper-cli.exe"
        fake_exe.touch()
        mod._TOOL_DIR = tmp_path
        mod._FALLBACK_DIR = tmp_path / "fallback"  # 不存在
        assert mod._find_exe() == fake_exe

    def test_fallback_to_fallback_dir(self, tmp_path):
        """tool_dir 沒有時才用 fallback_dir。"""
        mod = _load()
        fallback = tmp_path / "fallback"
        fallback.mkdir()
        fake_exe = fallback / "whisper-cli.exe"
        fake_exe.touch()
        mod._TOOL_DIR = tmp_path / "no_bin"  # 不存在
        mod._FALLBACK_DIR = fallback
        assert mod._find_exe() == fake_exe


# ---------------------------------------------------------------------------
# TestWhisperStt
# ---------------------------------------------------------------------------

class TestWhisperStt:
    def test_missing_audio_file(self, tmp_path):
        """音訊檔案不存在時回傳錯誤。"""
        mod = _load()
        nonexistent = tmp_path / "missing.wav"  # 沒有 create
        with patch.object(mod, "_find_exe", return_value=tmp_path / "whisper-cli.exe"):
            with patch.object(mod, "_find_model", return_value=tmp_path / "model.bin"):
                result = mod.whisper_stt(str(nonexistent))
        assert result.success is False
        assert result.error  # 有錯誤訊息就夠

    def test_unsupported_format(self, tmp_path):
        """不合法的 output_format 應立即拒絕。"""
        mod = _load()
        wav = tmp_path / "a.wav"
        _make_silent_wav(wav)
        fake_exe = tmp_path / "whisper-cli.exe"
        fake_exe.touch()
        fake_model = tmp_path / "model.bin"
        fake_model.touch()
        with patch.object(mod, "_find_exe", return_value=fake_exe):
            with patch.object(mod, "_find_model", return_value=fake_model):
                result = mod.whisper_stt(str(wav), output_format="xyz")
        assert result.success is False
        assert "xyz" in result.error

    def test_no_exe_found(self, tmp_path):
        """找不到 exe 時立即回傳錯誤。"""
        mod = _load()
        wav = tmp_path / "a.wav"
        _make_silent_wav(wav)
        with patch.object(mod, "_find_exe", return_value=None):
            result = mod.whisper_stt(str(wav))
        assert result.success is False
        assert "whisper-cli" in result.error

    def test_no_model_found(self, tmp_path):
        """找不到模型時立即回傳錯誤。"""
        mod = _load()
        wav = tmp_path / "a.wav"
        _make_silent_wav(wav)
        fake_exe = tmp_path / "w.exe"
        fake_exe.touch()
        with patch.object(mod, "_find_exe", return_value=fake_exe):
            with patch.object(mod, "_find_model", return_value=None):
                result = mod.whisper_stt(str(wav))
        assert result.success is False
        assert "模型" in result.error

    def test_success_reads_output_file(self, tmp_path):
        """正常執行：讀取 whisper-cli 寫出的 .txt 輸出。"""
        mod = _load()
        wav = tmp_path / "a.wav"
        _make_silent_wav(wav)
        fake_exe = tmp_path / "w.exe"
        fake_exe.touch()
        fake_model = tmp_path / "model.bin"
        fake_model.touch()

        transcription_text = "這是測試轉錄結果。"

        def fake_run_process(cmd, **kwargs):
            # 找 --output-file 並寫入假輸出
            for i, arg in enumerate(cmd):
                if arg == "--output-file" and i + 1 < len(cmd):
                    Path(cmd[i + 1] + ".txt").write_text(transcription_text, encoding="utf-8")
                    break
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            return r

        with patch.object(mod, "_find_exe", return_value=fake_exe):
            with patch.object(mod, "_find_model", return_value=fake_model):
                with patch("tools.whisper_stt.run_process", side_effect=fake_run_process):
                    result = mod.whisper_stt(str(wav))

        assert result.success is True
        assert transcription_text in result.output
        assert result.evidence["kind"] == "stt_process"

    def test_failure_returns_error(self, tmp_path):
        """process 回傳非零 exit code 時工具應回傳失敗。"""
        mod = _load()
        wav = tmp_path / "a.wav"
        _make_silent_wav(wav)
        fake_exe = tmp_path / "w.exe"
        fake_exe.touch()
        fake_model = tmp_path / "model.bin"
        fake_model.touch()

        def fake_fail(cmd, **kwargs):
            r = MagicMock()
            r.returncode = 1
            r.stdout = ""
            r.stderr = "error: cannot load model"
            return r

        with patch.object(mod, "_find_exe", return_value=fake_exe):
            with patch.object(mod, "_find_model", return_value=fake_model):
                with patch("tools.whisper_stt.run_process", side_effect=fake_fail):
                    result = mod.whisper_stt(str(wav))

        assert result.success is False
        assert result.exit_code == 1

    def test_language_param_passed(self, tmp_path):
        """language != 'auto' 時 --language 應出現在 cmd 中。"""
        mod = _load()
        wav = tmp_path / "a.wav"
        _make_silent_wav(wav)
        fake_exe = tmp_path / "w.exe"
        fake_exe.touch()
        fake_model = tmp_path / "model.bin"
        fake_model.touch()
        captured: list[str] = []

        def fake_run(cmd, **kwargs):
            captured.extend(cmd)
            r = MagicMock()
            r.returncode = 0
            r.stdout = "你好"
            r.stderr = ""
            return r

        with patch.object(mod, "_find_exe", return_value=fake_exe):
            with patch.object(mod, "_find_model", return_value=fake_model):
                with patch("tools.whisper_stt.run_process", side_effect=fake_run):
                    mod.whisper_stt(str(wav), language="zh")

        assert "--language" in captured
        assert "zh" in captured

    def test_auto_language_not_passed(self, tmp_path):
        """language='auto' 時 --language 不應出現。"""
        mod = _load()
        wav = tmp_path / "a.wav"
        _make_silent_wav(wav)
        fake_exe = tmp_path / "w.exe"
        fake_exe.touch()
        fake_model = tmp_path / "model.bin"
        fake_model.touch()
        captured: list[str] = []

        def fake_run(cmd, **kwargs):
            captured.extend(cmd)
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            return r

        with patch.object(mod, "_find_exe", return_value=fake_exe):
            with patch.object(mod, "_find_model", return_value=fake_model):
                with patch("tools.whisper_stt.run_process", side_effect=fake_run):
                    mod.whisper_stt(str(wav), language="auto")

        assert "--language" not in captured

    def test_threads_param_passed(self, tmp_path):
        """threads 值應正確傳進 cmd。"""
        mod = _load()
        wav = tmp_path / "a.wav"
        _make_silent_wav(wav)
        fake_exe = tmp_path / "w.exe"
        fake_exe.touch()
        fake_model = tmp_path / "model.bin"
        fake_model.touch()
        captured: list[str] = []

        def fake_run(cmd, **kwargs):
            captured.extend(cmd)
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            return r

        with patch.object(mod, "_find_exe", return_value=fake_exe):
            with patch.object(mod, "_find_model", return_value=fake_model):
                with patch("tools.whisper_stt.run_process", side_effect=fake_run):
                    mod.whisper_stt(str(wav), threads=8)

        assert "--threads" in captured
        idx = captured.index("--threads")
        assert captured[idx + 1] == "8"
