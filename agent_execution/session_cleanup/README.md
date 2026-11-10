# Session Workspace Cleanup (工作階段錯誤自動清理)

## 功能說明

當 AI 執行動作（例如各項 Tool 呼叫、檔案寫入、編輯）或程式執行（例如 `run_command`、`run_skill` 等）出現錯誤，且必須停止動作或程式執行時，此模組會自動將剛剛工作階段（Work Session / Turn / Task）內所產生的檔案與目錄全數刪除，以避免工作區殘留錯誤或不完整的半成品。

## 設計要點

1. **工作階段狀態快照與即時追蹤**：
   - 進入工作階段（`session_cleanup_scope`）時自動擷取工作區既有檔案與目錄快照。
   - `write_file`、`edit_file`（建立新檔）在建立檔案時主動登記至當前追蹤器。
   - 透過快照差異比對，即使是透過外部程序或命令（`run_command`）在工作區生成的檔案或目錄，也能被完整辨識並刪除。

2. **安全保護與回原**：
   - 工作階段開始前已存在的檔案與目錄絕對不會被刪除。
   - 若既有檔案在該工作階段內被修改，在清理階段會自動將內容復原至修改前狀態。
   - 刪除操作由深至淺安全清理目錄，並相容 Windows 檔案權限（移除 Read-Only 屬性後刪除）。
   - 支援巢狀工作階段：子階段失敗時清理子階段產物，子階段成功時將生成清單向上合併至父階段。

3. **系統整合**：
   - `server.py` (`run_agent`)：當 Agent 因執行限制、驗收未通過、任務取消或未攔截例外停止時，自動觸發清理。
   - `agent_runtime.py` (`run_agent_turn` / `resume_agent_turn`)：當輪次發生未處理錯誤或評估狀態為 failed 時清理。
   - `agent_execution/tasks.py` (`durable_invoke`)：當持久化任務狀態標記為 failed 或 cancelled 時執行清理。
   - `base.py`：`write_file`、`edit_file` 與 `run_command` 主動通知當前活躍追蹤器。

