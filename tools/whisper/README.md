# whisper/ — Whisper STT 工具目錄

存放 whisper-cli.exe 及其依賴 DLL，供 `whisper_stt` 工具使用。

## 目錄結構

```
tools/whisper/
├── bin/                  # 執行檔與 DLL（git 忽略，需手動複製）
│   ├── whisper-cli.exe
│   ├── ggml.dll
│   ├── ggml-base.dll
│   ├── ggml-cpu.dll
│   └── whisper.dll
└── ggml-base.bin         # 模型（選用，git 忽略）
```

## 初次設定

執行 setup script 複製二進位檔：

```bash
python tools/setup_whisper.py
```

或手動從建置路徑複製：
- **來源**：`C:\Users\andyl\Go\whisper.cpp\build\bin\Release\`
- **模型**：`C:\Users\andyl\Go\whisper.cpp\ggml-base.bin`

## 備注

- `bin/` 目錄和 `.bin` 模型檔已加入 `.gitignore`，不會被提交
- 若 `bin/` 不存在，工具會自動 fallback 到原始建置路徑
- 模型同樣會先找本目錄，找不到再 fallback 到原始路徑
