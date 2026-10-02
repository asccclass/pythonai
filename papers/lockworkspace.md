# Workspace Lock Plan

## 背景

目前專案已在 `base.py` 中提供部分 workspace 保護，例如 `AGENT_WORKSPACE_ROOT`、`resolve_workspace_path()` 與 `run_command()` 的路徑檢查。不過這套保護仍偏向「有設定環境變數時才啟用」，尚未形成完整的 Agent 執行沙箱。

目標是將 Agent 的所有檔案產出與命令列操作都鎖定在專案的 `workspace` 目錄下：

- Agent 產生、讀取、列出、刪除的檔案都必須位於 `workspace` 內。
- Agent 執行的命令列操作必須以 `workspace` 作為工作目錄。
- 命令中出現的路徑參數不得逃出 `workspace`。
- 規則應由工具層強制執行，而不只依賴 prompt 要求 Agent 自律。

## 建議修改方向

### 1. 將 workspace 作為預設硬性根目錄

調整 `workspace_root()` 的行為：

- 若環境變數 `AGENT_WORKSPACE_ROOT` 有設定，使用該路徑作為 workspace root。
- 若沒有設定，預設使用專案根目錄下的 `workspace`。
- 啟動或第一次解析 workspace 時，確保 `workspace` 目錄存在。

同時更新 `.env.example`：

- 將 `AGENT_WORKSPACE_ROOT` 改成 `workspace`，或補充說明預設值就是專案下的 `workspace`。
- 避免範例將 workspace 指向整個專案根目錄，造成 Agent 可以操作專案原始碼。

### 2. 強化檔案工具的 workspace 限制

以下工具應全部透過 `resolve_workspace_path()`：

- `read_file`
- `list_files`
- `write_file`
- `delete_file`

需要補強的細節：

- `write_file()` 寫入前可自動建立父目錄，但父目錄仍必須位於 workspace 內。
- 錯誤訊息統一說明是 Agent workspace 限制。
- 繼續使用 `.resolve()` 檢查，避免 `..`、絕對路徑或 symlink 逃出 workspace。

### 3. 命令列操作一律鎖定在 workspace 下

`run_command(command, cwd=None, env_file=None)` 應符合以下規則：

- `cwd=None` 時，固定使用 workspace root。
- `cwd` 若有傳入，只能是 workspace 內的相對路徑或子目錄。
- `env_file` 若有傳入，只能位於 workspace 內。
- `command` 中看起來像路徑的參數必須解析並確認在 workspace 內。
- URL 不應被誤判成檔案路徑。

目前已有 `resolve_command_cwd()`、`validate_command_paths()` 與 `resolve_command_executable()` 等雛形，主要工作是讓 workspace 限制預設啟用，並補足測試涵蓋。

### 4. 處理現有 Skill 相容性

現有 `skills/mybrain_query_cli/skill.json` 會以：

```json
{
  "cwd": "skills\\mybrain_query_cli\\scripts",
  "env_file": "skills\\mybrain_query_cli\\scripts\\envfile"
}
```

執行 bundled CLI。若全面鎖定到 `workspace`，這類 skill 會被拒絕。

可選方案：

- **嚴格方案：** skill 若要執行命令、讀 envfile 或使用 executable，相關檔案必須放在 `workspace` 下。
- **例外方案：** 對專案內受信任的 bundled skill assets 開白名單，但命令輸出與產檔仍必須限制在 workspace。

建議先採用嚴格方案，因為它最符合「所有 Agent 操作都鎖定 workspace」的需求，規則也比較容易理解、測試與維護。

### 5. 更新工具描述與 Agent 可見說明

更新 `TOOLS_SCHEMAS` 中工具描述，讓模型知道：

- 檔案工具只能操作 Agent workspace 內路徑。
- `run_command` 一律在 Agent workspace 內執行。
- `cwd` 是 workspace 內的相對位置。
- workspace 外部路徑會被拒絕。

這不是安全邊界本身，但能減少模型嘗試外部路徑而造成無效工具呼叫。

### 6. 測試規劃

因為這會修改程式行為，需更新或新增 `tests/test_base.py` 測試：

- 未設定 `AGENT_WORKSPACE_ROOT` 時，預設 root 是專案下的 `workspace`。
- `run_command()` 未指定 `cwd` 時，預設在 workspace 執行。
- `run_command(..., cwd="subdir")` 可在 workspace 子目錄執行。
- `cwd="../"` 或外部絕對路徑會被拒絕。
- 外部 `env_file` 會被拒絕。
- `write_file("a/b.txt", "...")` 會在 workspace 內建立父目錄。
- 指令參數包含 workspace 外部路徑時會被拒絕。
- 指令參數包含 URL 時不會被誤判成路徑。

### 7. 提交流程

依照 `AGENTS.md`：

- 每次程式更改都必須新增或更新相關測試。
- 每次更改完成後都必須建立對應 commit。
- 若只異動文件，不需要撰寫測試，但仍需建立 commit。

## 預期結果

完成後，Agent 的工具層會形成一致的 workspace 邊界：

- 模型無法透過檔案工具直接讀寫 workspace 外的檔案。
- 模型無法透過 `run_command` 在 workspace 外執行命令。
- 任何可疑路徑都會在工具執行前被拒絕。
- workspace 限制不依賴環境變數是否存在，預設即生效。
