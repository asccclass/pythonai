# Serena 的本機 Codex MCP 安裝紀錄

安裝日期：2026-10-20。此目錄只保存設定與驗證紀錄，Serena 本體由 uv 管理，未加入 PythonAI 的 venv 或套件需求。

## 官方來源與安裝

- 專案：https://github.com/oraios/serena
- 安裝：https://oraios.github.io/serena/02-usage/010_installation.html
- Codex 設定：https://oraios.github.io/serena/02-usage/030_clients.html#codex-cli-and-app
- Codex MCP：https://developers.openai.com/codex/mcp

採用官方推薦的已安裝版本，避免每次啟動同步 Git 原始碼：

```powershell
uv tool install -p 3.13 serena-agent
& "$env:USERPROFILE\.local\bin\serena.exe" init
```

本次安裝 Serena 2.1.0，使用 LSP backend。uv 工具環境位於 `C:\Users\andyl\AppData\Roaming\uv\tools\serena-agent`，啟動程式位於 `C:\Users\andyl\.local\bin\serena.exe`。

## Codex 使用者設定

在 `C:\Users\andyl\.codex\config.toml` 末尾新增以下區塊。採官方支援的手動設定方式，未執行會另行設定 hooks 的 `serena setup codex`。

```toml
[mcp_servers.serena]
command = 'C:\Users\andyl\.local\bin\serena.exe'
args = ["start-mcp-server", "--project-from-cwd", "--context=codex", "--open-web-dashboard", "false"]
startup_timeout_sec = 60
```

絕對路徑避免桌面程式 PATH 差異；60 秒預留 Windows 冷啟動時間。保留 dashboard 預設功能，但不自動開啟瀏覽器。未綁定單一專案，Codex 可在其他專案使用 Serena。

修改前的完整設定備份為 `C:\Users\andyl\.codex\config.toml.before-serena-20261020-194629.bak`。備份與實際使用者設定均留在本機，未納入 Git。

## 已完成驗證

- `uv tool install` 與 `serena init` 成功；`serena --version` 為 2.1.0。
- 使用專案 `venv\Scripts\python.exe` 解析 TOML，確認只新增 `mcp_servers.serena`，所有既有設定語意一致，原檔位元組完整保留為新檔前綴。
- `codex mcp get serena` 成功讀取 stdio 設定，服務為 enabled。
- 使用專案 venv Python，依實際設定在暫存工作目錄啟動服務，完成 JSON-RPC `initialize`、`notifications/initialized` 與 `tools/list`。服務回報 Serena 2.1.0，啟動約 3.41 秒，提供 `activate_project`、`initial_instructions`、`serena_repl` 三項工具。
- 驗證後關閉測試服務；未對 PythonAI 進行 Serena onboarding 或建立索引。未測試專案符號查詢或語言伺服器功能。

目前聊天的工具清單不一定會動態載入新 MCP。重新啟動 Codex／開啟新工作階段後，使用 `/mcp` 確認連線。官方提醒 Codex App 的啟動目錄可能不是專案目錄；開始工作時可要求：「使用 Serena 啟用 C:\Users\andyl\Go\pythonai 專案，並讀取 initial_instructions」。

## 更新與回復

更新使用 `uv tool upgrade serena-agent`。移除此整合時，使用 `codex mcp remove serena`，再視需要執行 `uv tool uninstall serena-agent`。不要以整份舊備份覆蓋已有後續修改的 Codex 設定。Serena 全域設定位於 `C:\Users\andyl\.serena\serena_config.yml`。
