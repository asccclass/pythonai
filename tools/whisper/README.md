# Whisper STT 工具

本目錄存放 whisper-cli.exe 及所需的 DLL、模型。

## 安裝（首次）

```powershell
# 方式 A：PowerShell
.\tools\whisper\install.ps1

# 方式 B：Python
python tools\whisper\install.py
```

安裝後目錄結構：
```
tools/whisper/
  whisper-cli.exe
  whisper.dll
  ggml.dll
  ggml-base.dll
  ggml-cpu.dll
  ggml-base.bin      ← 模型（142 MB）
  install.ps1
  install.py
  README.md
```

## 環境變數（可選覆寫）

| 變數 | 預設 | 說明 |
|------|------|------|
| `WHISPER_CLI` | `tools/whisper/whisper-cli.exe` | exe 路徑 |
| `WHISPER_MODEL` | `tools/whisper/ggml-base.bin` | 模型路徑 |
| `WHISPER_LANG` | `zh` | 語言（`auto` 自動偵測）|
| `WHISPER_THREADS` | `4` | CPU 執行緒數 |

## 手動測試

```bash
cd tools\whisper
.\whisper-cli.exe --model .\ggml-base.bin --file test.wav --language zh --no-timestamps
```

## Python API

```python
from desktop_assistant.services.whisper_stt import get_stt

stt = get_stt()
if stt.available():
    text = stt.transcribe("recording.wav")   # 同步
    stt.transcribe_async("rec.wav", callback) # 非同步
```

## Qt 元件

```python
from desktop_assistant.services.whisper_mic import WhisperMicButton

btn = WhisperMicButton()
btn.transcribed.connect(input_box.insertPlainText)
btn.error.connect(notify)
# 需要 sounddevice: pip install sounddevice
```
