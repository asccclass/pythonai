# Cronjob 排程系統詳細設計 (P0)

## 1. 簡介

為了讓 Python AI Mini Agent 具備主動定期執行任務的能力，我們需要實作一套類似 Linux cron 的排程系統。這套系統將負責管理、排程與執行各種類型的背景工作，如 Shell 指令、Python 腳本、本地 Skill、固定 Agent prompt 以及系統維護工作（例如：每日摘要、記憶回顧）。

## 2. 系統架構與模組劃分

主要將新增 `scheduler.py` 模組，並與現有的 `agent_runtime.py`、`observability.py` 等模組整合。

### 核心元件

- **Scheduler Daemon (`scheduler.py`)**: 
  - 啟動背景執行緒 (或使用 asyncio task) 定期（例如每 60 秒）檢查是否有需要執行的任務。
  - 負責解析 Cron 表達式，並計算任務的下次執行時間。
  - 使用資料庫鎖 (Database Lock) 或類似機制確保多個執行個體/多行程下，同一個排程任務不會被重複執行。
- **Job Dispatcher**: 
  - 根據任務類型 (`job_type`)，將任務派發給對應的執行器 (Executor)。
- **CLI 命令 (`cli.py` 或獨立的 `schedule_cli.py`)**:
  - 提供使用者介面來管理排程任務 (新增、刪除、列表、啟用/停用、立即執行)。

## 3. 資料庫 Schema 設計 (SQLite)

在主資料庫中新增以下三個資料表：

### 3.1 `scheduled_jobs`
負責儲存排程任務的定義。
`sql
CREATE TABLE scheduled_jobs (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    cron_expression TEXT NOT NULL, -- e.g., '0 * * * *'
    job_type TEXT NOT NULL, -- 'command', 'script', 'skill', 'agent_prompt', 'maintenance'
    job_payload TEXT NOT NULL, -- JSON 格式，包含任務執行所需的參數
    is_enabled BOOLEAN DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    next_run_at TIMESTAMP -- 預先計算好的下次執行時間
);
`

### 3.2 `scheduled_job_runs`
負責記錄每次任務的執行結果（與 `observability.py` 整合）。
`sql
CREATE TABLE scheduled_job_runs (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    status TEXT NOT NULL, -- 'running', 'completed', 'failed'
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP,
    output TEXT, -- 執行輸出或日誌摘要
    error_message TEXT, -- 失敗時的錯誤訊息
    FOREIGN KEY (job_id) REFERENCES scheduled_jobs(id)
);
`

### 3.3 `scheduled_job_locks`
用於多行程環境下的排他鎖定，確保同一個時間點只有一個 worker 執行該任務。
`sql
CREATE TABLE scheduled_job_locks (
    job_id TEXT PRIMARY KEY,
    locked_by TEXT NOT NULL, -- Worker ID 或 PID
    locked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP NOT NULL, -- 鎖的過期時間，避免 dead worker 造成死鎖
    FOREIGN KEY (job_id) REFERENCES scheduled_jobs(id)
);
`

## 4. 支援的任務類型 (`job_type`)

| 類型 | 說明 | `job_payload` 範例 |
|---|---|---|
| `command` | 執行系統 Shell 指令 | `{"command": "df -h > /tmp/disk.log"}` |
| `script` | 執行 Python 腳本 | `{"script_path": "/path/to/script.py", "args": ["--verbose"]}` |
| `skill` | 呼叫本地已註冊的 Skill | `{"skill_name": "web_search", "args": {"query": "today news"}}` |
| `agent_prompt` | 觸發 Agent 執行特定 Prompt | `{"prompt": "Summarize my unread emails", "session_id": "auto"}` |
| `maintenance` | 系統內建的維護作業 (如: 記憶回顧) | `{"task": "daily_summary"}` |

## 5. 核心流程

### 5.1 排程檢查迴圈 (Tick)
1. 每分鐘 (或每 30 秒) 喚醒一次。
2. 查詢 `scheduled_jobs`，找出 `is_enabled = 1` 且 `next_run_at <= CURRENT_TIMESTAMP` 的任務。
3. 對於每個到期的任務，嘗試在 `scheduled_job_locks` 寫入鎖定紀錄。
4. 如果鎖定成功，在 `scheduled_job_runs` 建立一筆 `running` 狀態的紀錄，並更新 `scheduled_jobs.next_run_at`。
5. 在背景執行緒或 Async Task 中派發任務。

### 5.2 任務執行與回報
1. Dispatcher 根據 `job_type` 執行任務。
2. 捕獲 stdout/stderr 或例外狀況。
3. 執行結束後，更新 `scheduled_job_runs`，設定為 `completed` 或 `failed`，並寫入 `output` / `error_message`。
4. 釋放 `scheduled_job_locks` 中的鎖定。

## 6. CLI 介面設計

使用類似以下的命令列結構：
- `schedule list`: 列出所有排程任務及其狀態 (下次執行時間、是否啟用)。
- `schedule add <name> <cron> <type> <payload>`: 新增任務。
- `schedule remove <id>`: 刪除任務。
- `schedule enable <id>`: 啟用任務。
- `schedule disable <id>`: 停用任務。
- `schedule run-now <id>`: 忽略 cron 表達式，立即執行一次該任務。

## 7. 驗證與測試計畫

- **Cron 解析測試**: 使用 `croniter` 或自建解析器，單元測試各種 cron 表達式（包含邊界值與跳躍時間）。
- **併發鎖測試 (Concurrency Test)**: 啟動多個 Scheduler 實例，確保同一個任務在同一個時間點只會被執行一次。
- **錯誤恢復與重試**: 測試任務執行失敗時，是否正確記錄錯誤，以及 Worker crash 時鎖是否會過期並由其他 Worker 接手。
- **CLI 整合測試**: 透過 CLI 新增任務並驗證資料庫寫入正確性，並測試 `run-now` 的強制執行邏輯。