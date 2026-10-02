# 後續功能待辦規劃

本文件整理本專案接下來適合加入的功能。整體方向是讓目前的 Python AI Mini Agent 從被動的 CLI/通訊回覆，逐步擴展為具備排程、任務治理、記憶治理、技能管理與可觀測性的個人 Agent runtime。

## 優先順序總覽

| 優先級 | 功能 | 目標 | 主要關聯模組 |
|---|---|---|---|
| P0 | Cronjob 排程系統 | 讓 Agent 能依照 Linux cron 風格定時執行任務。 | `agent_runtime.py`, `communication_worker.py`, `observability.py` |
| P1 | 任務狀態查詢與取消 | 讓長任務可被查詢、追蹤與取消。 | `communication_store.py`, `communication_worker.py`, `communication_server.py` |
| P1 | 每日/每週摘要 | 定期彙整記憶、任務與錯誤，並主動推送。 | `memory.py`, `observability.py`, `communication_adapters/` |
| P2 | 記憶自然語言修正 | 允許使用者用自然語言檢查、修正或封存記憶。 | `memory.py`, `memory_update.py`, `memory_review.py`, `observability.py` |
| P2 | Web 管理介面 | 提供記憶、任務、技能與通訊狀態的視覺化管理。 | `communication_server.py`, `observability.py`, `skills.py` |
| P3 | Skill 管理 CLI | 讓本地 Skill 能被安裝、啟用、停用與檢查權限。 | `skills.py`, `observability.py` |
| P3 | LINE / Discord Adapter | 擴充 Telegram 之外的通訊入口。 | `communication_adapters/`, `communication_server.py` |
| P3 | 權限模型強化 | 對危險操作、排程與通訊入口建立角色權限。 | `base.py`, `agent_runtime.py`, `communication_store.py` |

## P0：Cronjob 排程系統

### 目標

新增類似 Linux cronjob 的排程能力，使系統可以定時執行：

- Shell 指令。
- Python 腳本。
- 本地 Skill。
- 固定 Agent prompt。
- 維護工作，例如記憶回顧、嵌入補全、每日摘要。

### 建議交付物

- 新增 `scheduler.py`，負責 cron 表達式解析、下次執行時間計算、任務派發。
- 新增 SQLite 資料表：
  - `scheduled_jobs`
  - `scheduled_job_runs`
  - `scheduled_job_locks`
- 支援 cron 五欄格式：`minute hour day_of_month month day_of_week`。
- 支援任務類型：
  - `command`
  - `script`
  - `skill`
  - `agent_prompt`
  - `maintenance`
- 新增 CLI 操作：
  - `schedule list`
  - `schedule add`
  - `schedule remove`
  - `schedule enable`
  - `schedule disable`
  - `schedule run-now`
- 與 `observability.py` 整合，顯示排程健康狀態、最近執行紀錄、失敗原因。

### 驗證方式

- 單元測試 cron 表達式解析與下次執行時間。
- 單元測試任務入列、鎖定、完成、失敗與重試。
- 測試重啟後 pending job 不遺失。
- 測試多進程時只有一個 worker 取得同一個排程鎖。

## P1：任務狀態查詢與取消

### 目標

讓使用者可以透過 CLI 或通訊軟體查詢目前任務，並取消尚未完成的任務。

### 建議交付物

- 為 `agent_command_jobs` 補齊狀態轉移：
  - `pending`
  - `running`
  - `completed`
  - `failed`
  - `cancel_requested`
  - `cancelled`
- 新增 `/status` 指令，回傳最近任務與目前執行中任務。
- 新增 `/cancel <job_id>` 指令。
- 長任務執行時定期檢查取消旗標。
- 任務完成或失敗時記錄摘要、錯誤類型與時間。

### 驗證方式

- 測試 pending 任務可取消。
- 測試 running 任務收到取消請求後能安全停止或標記不可中斷。
- 測試通訊平台回覆格式不超過平台長度限制。

## P1：每日/每週摘要

### 目標

透過排程主動產生每日或每週摘要，讓 Agent 能回顧近期行為與重要記憶。

### 建議交付物

- 新增摘要任務類型 `maintenance.summary`。
- 摘要內容包含：
  - 最近完成的任務。
  - 新增或更新的語義記憶。
  - 新增或更新的程序記憶。
  - 失敗任務與錯誤原因。
  - 需要使用者確認的低信心記憶或衝突。
- 支援輸出到 CLI、檔案或 Telegram。
- 摘要產生需受請求預算限制，避免背景摘要消耗過多遠端 LLM 配額。

### 驗證方式

- 測試無遠端 LLM 時仍能產生基本規則摘要。
- 測試摘要只包含指定時間窗內的資料。
- 測試通訊推送失敗時有重試或錯誤紀錄。

## P2：記憶自然語言修正

### 目標

讓使用者能直接用自然語言管理記憶，而不必操作 SQLite 或記住 observability 命令。

### 範例指令

- 「列出你記得關於我的事。」
- 「忘記我剛剛說的那件事。」
- 「把我偏好的語言改成 Python。」
- 「這條記憶是錯的。」
- 「不要再把這個專案當成測試專案。」

### 建議交付物

- 新增記憶治理 intent 分類。
- 將自然語言修正轉換為既有操作：
  - confirm
  - archive
  - contradict
  - supersede
- 高風險或不明確的記憶修改需二階段確認。
- 每次修改都寫入審計事件，保留來源訊息。

### 驗證方式

- 測試自然語言能找到正確記憶候選。
- 測試不明確修正會要求確認。
- 測試封存與取代不會硬刪除資料。

## P2：Web 管理介面

### 目標

提供本地 Web UI，讓維護者能快速檢查 Agent 狀態。

### 建議頁面

- Overview：系統健康、任務隊列、記憶健康。
- Memory：語義記憶、程序記憶、低信心記憶、衝突。
- Communication：最近 inbound/outbound 訊息與 job 狀態。
- Scheduler：排程列表、最近執行紀錄、失敗任務。
- Skills：已安裝 Skill、執行紀錄、權限 allowlist。

### 驗證方式

- Web UI 預設只綁定 `127.0.0.1`。
- 測試主要 API 回傳穩定 JSON 結構。
- 測試空資料庫時頁面不報錯。

## P3：Skill 管理 CLI

### 目標

讓本地 Skill 能更容易被管理，並降低不安全 Skill 被誤執行的風險。

### 建議交付物

- CLI：
  - `skills list`
  - `skills inspect <name>`
  - `skills enable <name>`
  - `skills disable <name>`
  - `skills validate <name>`
- 檢查 `skill.json` 與 `SKILL.md` 是否完整。
- 顯示每個 Skill 的工具 allowlist。
- 記錄 Skill 啟用、停用與執行審計事件。

### 驗證方式

- 測試缺少必要欄位的 Skill 會被拒絕。
- 測試停用 Skill 後 `run_skill` 不可執行。
- 測試 Skill 執行紀錄可被 `observability.py` 查詢。

## P3：LINE / Discord Adapter

### 目標

在 Telegram 之外支援更多通訊入口。

### 建議順序

1. LINE：適合個人使用與手機通知。
2. Discord：適合團隊協作與 slash command。

### 建議交付物

- LINE：
  - 驗證 `X-Line-Signature`。
  - 解析文字訊息。
  - 支援 reply message 與 push message。
- Discord：
  - 支援 slash command。
  - 驗證 interaction signature。
  - 支援 deferred response。

### 驗證方式

- 使用假 payload 測試簽章驗證。
- 測試平台訊息可轉成統一 `InboundMessage`。
- 測試出站訊息分段與錯誤回報。

## P3：權限模型強化

### 目標

對外部通訊入口、排程任務與危險工具建立一致權限模型。

### 建議角色

- `viewer`：只能提問、查詢狀態。
- `operator`：可執行低風險任務與部分 Skill。
- `admin`：可寫檔、執行命令、建立排程、修改記憶。

### 建議交付物

- 在通訊身分中加入角色欄位。
- 對工具呼叫加入權限檢查。
- 對排程任務加入建立者與執行權限。
- 高風險操作要求二階段確認。
- 權限拒絕需記錄審計事件。

### 驗證方式

- 測試 viewer 無法執行命令或建立排程。
- 測試 operator 可執行允許清單內的 Skill。
- 測試 admin 危險操作仍需確認。

## 建議實作順序

1. 撰寫 `papers/cronjob.md` 詳細設計。
2. 實作最小可用 Cronjob 排程系統。
3. 將每日摘要建立為第一個內建排程任務。
4. 補上任務狀態查詢與取消。
5. 擴充記憶自然語言修正。
6. 建立本地 Web 管理介面。
7. 擴充 Skill 管理與更多通訊 Adapter。

