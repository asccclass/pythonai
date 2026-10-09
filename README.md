# PythonAI Agent

PythonAI 是以 OpenAI-compatible Chat Completions 為模型介面的 Python Agent，提供本機 CLI、Telegram long polling，以及整合 webhook、管理網頁、REST API、WebSocket 的 FastAPI 服務。核心包含工具呼叫、Skills、長期記憶、背景維護、排程、任務驗收及可擴充 Hooks。

以下盤點以 **2026-10-09 的程式與測試**為準。「已實作」表示有可追溯程式路徑，不代表已通過所有外部服務或正式環境驗證；「基礎實作」表示尚有整合、隔離或可靠性限制。後續改善及驗收條件見 [papers/todo.md](papers/todo.md)。

## 現有功能表

| 功能領域 | 現有能力 | 狀態與限制 | 實作依據 |
| --- | --- | --- | --- |
| Agent 執行迴圈 | 多輪模型／工具呼叫、讀取 AGENTS.md、規劃與反思提示、取消檢查 | 已實作；預設最多 15 次迭代，規劃主要靠提示，尚無持久化任務 DAG | `server.py`, `agent_runtime.py` |
| Provider 相容與重試 | OpenAI-compatible endpoint、模型／embedding 設定、暫時性 API 錯誤重試與 Retry-After | 已實作；主要使用 Chat Completions，未建立多 provider 路由及總成本上限 | `server.py`, `request_budget.py` |
| 檔案與命令 | 讀寫、刪除、字串替換、列目錄、curl 擷取、命令執行 | 已實作；共用政策檢查角色與路徑／symlink，本機命令有條件確認；遠端通訊執行命令自動批准 | `base.py` |
| Skills | skill.json + SKILL.md、觸發詞匹配、輸入驗證、工具 allowlist、共用政策、步驟與結果紀錄、啟停 CLI | 已實作；唯一明確且可執行的匹配直接執行；多個匹配／指引型 Skill 交給模型；執行步驟目前為 tool_sequence | `skills.py`, `skills_cli.py`, `agent_runtime.py` |
| 動態工具 | 載入 tools/*.py + JSON schema、create_dynamic_tool 建立並重新載入 | 基礎實作；載入會執行 Python 模組，尚無發布前測試、版本審核及撤回流程 | `base.py`, `tools/` |
| MCP | stdio JSON-RPC 子程序、初始化、工具發現、schema 轉換、呼叫路由與 shutdown | 已實作；啟動失敗記於 skipped_servers；未見 remote HTTP/OAuth 與自動重連管理 | `mcp_manager.py`, `mcp_config.json` |
| Episodic memory | SQLite episodes／events、訊息、工具與驗收事件追蹤 | 已實作；一般 Agent 對話仍以 runtime 記憶體持有，事件紀錄不等於完整任務續跑 | `memory.py`, `agent_runtime.py` |
| Semantic memory | 主詞／關係／受詞、user_profile／agent_persona 等類型、scope、信心、來源、實體關聯 | 已實作；遠端採身份／專案實體 DB 隔離，通訊另依會話隔離；本機既有資料保留 | `semantic_extractor.py`, `memory.py`, `agent_execution/sessions.py` |
| Procedural memory | 重用工作流程、相似度比對、成功／失敗計數、驗收狀態控制保存 | 已實作；不代表模型訓練或任意任務成功保證 | `memory_review.py`, `procedure_similarity.py`, `server.py` |
| 記憶檢索 | embedding／cosine similarity、Hashing fallback、關鍵詞、實體與程序檢索、可選 Laya 排序 | 已實作；本機候選與向量計算，尚無大型資料 ANN 索引 | `retrieval.py`, `retrieval_ranker.py`, `vector_search.py` |
| 工作記憶與遺忘 | 對話摘要壓縮、保留候選、TTL、矛盾／替代、衝突處理、程序淘汰 | 已實作；壓縮尚非完整 token 預算及無損任務狀態保存 | `working_memory.py`, `forgetting.py` |
| 背景記憶維護 | 持久化 review／embedding queue、請求預算、跨程序 worker lease、429 cooldown、stale embedding 更新 | 已實作；限制背景記憶流量，非全 Agent 的成本／併發治理 | `memory_worker.py`, `request_budget.py`, `observability.py` |
| 記憶管理 | confirm／contradict／archive／supersede、CLI 匯入匯出、網頁 CRUD／restore | 已實作；高風險模型工具與 API 更新要求身份／會話／參數綁定的一次性批准 | `memory_tools.py`, `agent_execution/approvals.py`, `web_api.py` |
| Telegram | webhook／polling、getMe 診斷、訊息去重、佇列、長訊息分段、圖片／document 接收 | 已實作；圖片轉 image_url、文字解碼及截斷；二進位附件以 Base64 文字傳入，非通用 PDF／Office 解析 | `communication_adapters/telegram_adapter.py`, `telegram_polling_worker.py`, `agent_runtime.py` |
| Discord／LINE | 簽章與時效驗證、去重、Discord deferred response／回覆閘門／原始回覆更新與 follow-up、LINE push／分段 | 已實作本機協定測試；production 缺配置／PyNaCl 拒絕，正式帳號 round trip 尚待驗證 | `communication_adapters/`, `communication_server.py`, `tests/test_webhook_security.py` |
| 通訊任務管理 | SQLite inbound／outbound／command、背景執行、/status、/cancel、/clear／/reset、ask_user 暫停及回覆續跑 | 已實作；暫停／續跑機制已實作，但 ask_user 未列入預設工具 schema；取消屬合作式檢查，附件 bytes 暫存於記憶體，shared runtime 尚需隔離 | `communication_store.py`, `communication_worker.py`, `exceptions.py` |
| 統一 ASGI 與 Dashboard | /health、管理網頁、統計／任務 API、記憶 CRUD、三平台 webhook、/ws/v1 | 已實作；REST 管理路由尚未整合登入與角色授權；/health 僅回傳存活狀態 | `communication_server.py`, `web_api.py`, `web/index.html` |
| Companion WebSocket | hello token 驗證、seq／ack、同程序重連補送、ping、chat interrupt、裝置事件及主動訊息路由 | 協定原型；brain_reply_stream 為示範回覆，Telegram fallback 僅記入清單；audio.end 回空字串，未串接 STT／TTS | `websocket_server.py`, `tests/test_websocket_server.py` |
| Cron 排程 | croniter 計算、SQLite jobs／runs、工作鎖、背景 worker、command 與 maintenance.summary | 已實作；沒有通用 script／Skill／Agent job 執行器，保存建立者角色／身份，command 經共用政策以 argv 執行；共用 runner 有 timeout／取消／輸出上限 | `scheduler.py`, `schedule_cli.py`, `summary.py`, `add_default_jobs.py` |
| Guard 與 RBAC | 可選 Laya 請求／命令判斷、viewer／operator／admin、ROLES_CONFIG、API Bearer 登入 | 已實作共用政策；遠端缺角色配置預設 viewer，管理 API 寫入限 admin；Laya 不可用仍可繼續 | `laya_guard.py`, `permissions.py`, `agent_execution/api.py` |
| Hook Pipeline | Pre/PostModelCall、Pre/PostToolExecute；優先序、過濾、fail-open／closed、修改上下文、阻擋／提前回覆／回饋與稽核 | 已實作；default_hook_manager 是空 manager，需註冊政策 hook 才會執行政策 | `hooks.py`, `server.py`, `tests/test_hooks.py` |
| 任務驗收與自修正 | 檔案／Python 語法、測試輸出、Git 前後狀態、LLM Judge、Critic feedback | 已實作；預設最多 2 次驗收修正；規則／文字輸出及 Judge 有侷限，人工交接不等於完整審批 UI | `task_evaluator.py`, `tests/test_task_evaluator.py` |
| Sandbox | 明確 host/container 模式、Docker-only、唯讀工作副本、禁網路、CPU／RAM／PID／tmpfs 配額、安全 artifacts 匯出 | 已實作；container 無 Docker 即拒絕，不轉交主機 credentials，限 Linux 指令 | `sandbox.py`, `tests/test_sandbox.py` |
| 子代理 | planner／coder／reviewer daemon thread、JSON 狀態、invoke／check | 基礎實作；繼承角色／身份／執行 context，但共享 workspace／工具，無 worktree、依賴 DAG、取消、重啟恢復及結果合併治理 | `tools/invoke_subagent.py`, `tools/check_subagent.py` |
| 搜尋、網頁與視覺 | Wikipedia 搜尋（search_web）／DuckDuckGo（web_research）、網頁正文、Playwright 點擊／輸入／截圖、圖片模型分析及 DOM fallback | 基礎實作；需瀏覽器與相容模型，每次 browser_vision 建立新瀏覽器，尚無持久登入／桌面操作 | `base.py`, `tools/web_research.py`, `tools/browser_vision.py` |
| 程式碼理解與修改 | Python Tree-sitter 節點／embedding 索引、語意搜尋、Jedi 定義／引用、AST 替換及語法／flake8 失敗回復 | 基礎實作；偏 Python、需先建索引；工具統一 workspace 邊界，索引位於 workspace/codebase_index.db | `build_codebase_index.py`, `tools/search_codebase.py`, `tools/ast_edit.py`, `tools/go_to_definition.py`, `tools/find_references.py` |
| 開發工具 | Git status／diff／commit／rollback、pytest／Python 測試 runner、venv／pip／npm、四種 scaffold | 基礎實作；Git rollback 具破壞性，尚無 PR／CI／worktree 全流程；測試 runner timeout 為 30 秒 | `tools/git_*.py`, `tools/run_test_suite.py`, `tools/env_manager.py`, `tools/project_scaffold.py` |
| IDE 整合 | VS Code Inline Chat、Ghost Text、HTTP 串流替換選取程式碼 | 原型；獨立 ide_server.py，非完整 Agent 任務 UI，尚無修改預覽、驗收與 robust cancellation | `ide_server.py`, `vscode-extension/` |
| 觀測與診斷 | episodes／hooks／Skills／記憶健康 CLI、Skill Doctor 產生 agent_persona 建議 | 已實作；Doctor --apply 可寫記憶，尚無品質 A/B 評測及完整分散式 tracing／成本報表 | `observability.py`, `skill_doctor.py` |

附帶資源包括 `skills/story/` 的敘事分析／motion 程式與測試，以及 `skills/security_audit/` 的安全審查指引及 Node 報告驗證器。它們是個別工作流程或應用；目錄存在不表示全部已註冊為可執行 Skill。`patch*.py`、`fix*.py`、`drill.py`、`demo_scenario.py` 等維護／示範腳本也不視為 Agent 正式入口。

## 與 Codex、通用 Agentic Agent 的差距

「Agentic Agent」在此指能規劃、使用工具、驗證、保存狀態並持續完成任務的通用自主代理架構，不是特定產品。Codex 比較以官方文件的工作流程為基準；帳號、平台與版本可影響可用功能。本次是實作盤點，不是模型品質或成功率 benchmark。

| 比較面向 | PythonAI 現況 | 相對差距與下一步 |
| --- | --- | --- |
| 編碼交付／Codex | 已有編輯、Git、測試與 Judge | 補 worktree、diff 審查、patch 套用、PR／CI、範圍控制及可回復交付；不能把 Git commit 工具視為完整開發流程 |
| 多代理／Codex | thread 委派與輪詢 | 補獨立上下文／權限／工作目錄、follow-up／cancel、結果收斂與衝突管理 |
| 執行安全／Codex | 路徑檢查、RBAC、可選 sandbox／Guard | 補所有工具一致的政策、真正環境隔離、網路控制、遠端最小權限及不可偽造批准紀錄 |
| 自動化／Codex | cron command 與每日摘要 | 補自然語言 Agent／Skill 任務、會話 follow-up、只於有意義變更通知、停止條件及審查結果 |
| 長任務／通用 Agentic | 有多輪、摘要、通訊暫停／續跑 | 補持久化計畫與 checkpoint、程序重啟恢復、冪等副作用、deadline／token／成本預算及重規劃 |
| 完成度驗證／通用 Agentic | 已有 Critic loop 與來源事件 | 補使用者驗收條件、結構化工具結果、端到端測試、獨立 Reviewer 與任務品質評測 |
| 通訊／互動 | 多平台 adapters、WebSocket 協定、IDE 原型 | 優先將 WebSocket 串接真實 runtime，隔離會話並補即時工具事件、附件持久化、批准 UI |
| 記憶／個人化 | 本專案已有完整度較高的本機記憶維護與稽核模組 | 保留此設計，補租戶／專案隔離、檢索品質測試及版本治理；不推論 Codex 缺少記憶能力 |
| 擴充／整合 | Skills、動態工具、stdio MCP | 補 schema／結果驗證、受審核工具發布、remote MCP／OAuth、健康檢查、版本相容與故障恢復 |

建議依序完成：**執行政策與會話隔離 → 真實串流與持久任務 → worktree／多代理交付 → 評測、整合與多模態**。可直接開工的清單見 [papers/todo.md](papers/todo.md)。

官方比較來源（查核日期：2026-10-09）：[Git worktrees](https://learn.chatgpt.com/docs/environments/git-worktrees)、[Subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents)、[Scheduled tasks](https://learn.chatgpt.com/docs/automations?surface=app)、[Agent approvals & security](https://learn.chatgpt.com/docs/agent-approvals-security)。

## Requirements

- Python 3.11 or newer
- Dependencies in `requirements.txt` (FastAPI, Uvicorn, croniter, OpenAI client and optional workflow libraries)

Install dependencies:

```powershell
python -m pip install -r requirements.txt
# Optional: download the local Laya checkpoint
python download_model.py
```

Optional Laya guard layer:

```powershell
python -m pip install laya
```

When installed, Laya classifies each user request before it is sent to the agent and prints a warning for risky or confirmation-worthy requests. By default, the guard loads the local checkpoint from `models/laya-english`. Set `LAYA_MODEL_DIR` to use a different local model path. If Laya is not installed or cannot load, the agent continues without the guard.

## Configuration

Create a local `.env` file in the project root:

```env
OLLAMA_BASE_URL=https://api.example.com/
OLLAMA_MODEL=your-model-name
OLLAMA_EMBEDDING_MODEL=your-embedding-model-name
OLLAMA_API_KEY=sk-your-litellm-virtual-key
SERVER_HOST=127.0.0.1
SERVER_PORT=8000
```

`OLLAMA_API_KEY` must be a LiteLLM virtual key that starts with `sk-`. `OLLAMA_EMBEDDING_MODEL` is optional and defaults to `OLLAMA_MODEL`. The `.env` file is ignored by Git and should not be committed.

## Usage

Run the mini agent:

```powershell
python .\server.py
```

Type a message at the `You:` prompt. Type `exit` or `quit` to stop.

## Entry Points

Use `server.py` when you want to chat with the agent directly from the local command line. It starts the interactive `You:` prompt, builds the shared agent runtime, loads Skills and memory, and runs the background memory review worker when this process can acquire the worker lease.

Use `telegram_polling_worker.py` when you want Telegram integration during local development or on a machine without a public HTTPS URL. It calls Telegram `getUpdates` from your machine, queues incoming text messages, runs them through the shared agent runtime, and sends replies back to the Telegram chat. This mode does not require ngrok, a public IP, or webhook registration.

Use `communication_server.py` when Telegram should deliver messages to the agent through HTTP webhooks, when you want the web dashboard, or when companion clients need WebSocket access. It starts a unified FastAPI / ASGI app with these endpoints:

- `POST /webhooks/telegram`: verifies and queues Telegram webhook updates, then lets the communication worker process pending commands.
- `GET /health`: returns a lightweight health response for local checks or tunnel monitoring.
- `GET /`: serves the memory management web interface from `web/index.html`.
- `/api/...`: serves authenticated dashboard and memory management JSON APIs (Bearer token required).
- `WebSocket /ws/v1`: accepts companion protocol connections.

`communication_server.py` creates the same agent runtime used by `server.py`, starts background processing for queued Telegram messages, and can be deployed behind a public tunnel or reverse proxy for webhook and WebSocket traffic.

In short, use one of these three entry points:

- Local terminal chat: run `python .\server.py`.
- Telegram long polling without a public URL: run `python .\telegram_polling_worker.py`.
- Unified HTTP webhook, dashboard, API, and WebSocket ASGI app: run `python .\communication_server.py`.

For the unified ASGI service, configure host, port, and WebSocket tokens as needed:

```powershell
$env:COMMUNICATION_HOST="127.0.0.1"
$env:COMMUNICATION_PORT="8000"
$env:COMPANION_WS_TOKENS="desktop-token:andy,mobile-token:andy"
python .\communication_server.py
```

Configure WebSocket tokens with one of these environment variables:

```env
COMPANION_WS_TOKENS=desktop-token:andy,mobile-token:andy
COMPANION_WS_TOKEN=single-token:andy
```

Do not put the token in the WebSocket URL. Send it in the first `hello` message payload.

## Telegram Webhook

Create a Telegram bot with `@BotFather`, then put the bot token in `.env`. For local testing without a public URL, use long polling:

```env
COMMUNICATION_DB_PATH=communication/communication.db
TELEGRAM_BOT_TOKEN=123456789:your-bot-token
TELEGRAM_POLL_TIMEOUT=30
TELEGRAM_POLL_IDLE_SLEEP=0.2
COMM_ALLOWED_SENDERS=telegram:123456789
```

`COMM_ALLOWED_SENDERS` is optional for local testing, but should be set before regular use. Use Telegram's numeric user id, formatted as `telegram:<sender_id>`.

Start the long polling worker:

```powershell
python .\telegram_polling_worker.py
```

This mode does not require ngrok, a public IP, or webhook registration because the worker calls Telegram `getUpdates` from your machine.

If you prefer webhook mode, also set a webhook secret and host/port:

```env
COMMUNICATION_DB_PATH=communication/communication.db
COMMUNICATION_HOST=127.0.0.1
COMMUNICATION_PORT=8000
TELEGRAM_BOT_TOKEN=123456789:your-bot-token
TELEGRAM_WEBHOOK_SECRET=your-random-secret
COMM_ALLOWED_SENDERS=telegram:123456789
```

Start the communication server:

```powershell
python .\communication_server.py
```

Expose it with a tunnel such as ngrok:

```powershell
ngrok http 8000
```

Register the Telegram webhook with the public HTTPS URL:

```powershell
curl.exe -X POST "https://api.telegram.org/bot$env:TELEGRAM_BOT_TOKEN/setWebhook" `
  -H "Content-Type: application/json" `
  -d "{\"url\":\"https://your-tunnel.example/webhooks/telegram\",\"secret_token\":\"$env:TELEGRAM_WEBHOOK_SECRET\"}"
```

Check webhook status:

```powershell
curl.exe "https://api.telegram.org/bot$env:TELEGRAM_BOT_TOKEN/getWebhookInfo"
```

If you need your Telegram sender id before enabling `COMM_ALLOWED_SENDERS`, temporarily leave it empty, send a message to the bot, then inspect the communication database or recent inbound rows. After confirming the id, set `COMM_ALLOWED_SENDERS` and restart the server.


## Memory

The agent records episodic memory in a local SQLite database at `memory\memory.db`. This file is ignored by Git. Set `MEMORY_DB_PATH` to use a different database location.

The memory store also supports semantic memories as auditable subject-predicate-object facts linked back to source episode events.

Procedural memories store reusable workflows with success and failure counts linked back to source episodes.

Memory review and embedding backfill run as bounded background jobs. The job queue is stored in SQLite, so pending review and embedding work can resume after restarting the agent. Provider-wide 429 cooldown state is also stored in SQLite and honors `Retry-After` before retrying background memory work.

When multiple entry points run at the same time, such as `server.py` and `telegram_polling_worker.py`, only one process acquires the background memory worker lease. Other processes continue to handle Agent turns and enqueue review jobs, but leave background memory review and embedding backfill work to the lease owner.

Inspect memory from the command line:

```powershell
python .\observability.py overview
python .\observability.py episode --episode-id 1
python .\observability.py messages --limit 50
python .\observability.py messages --episode-id 1
python .\observability.py review-candidates --limit 20
python .\observability.py procedures
python .\observability.py procedures --name run_command
python .\observability.py facts --memory-type user_profile
python .\observability.py low-confidence --threshold 0.35
python .\observability.py conflicts
python .\observability.py memory-health
python .\observability.py embedding-candidates --limit 20
python .\observability.py embedding-candidates --stale-before "2026-02-01 00:00:00"
python .\observability.py backfill-embeddings --limit 20
python .\observability.py backfill-embeddings --stale-before "2026-02-01 00:00:00"
python .\observability.py archive-fact --memory-id 1 --reason obsolete
python .\observability.py confirm-fact --memory-id 1
python .\observability.py contradict-fact --memory-id 1
python .\observability.py supersede-fact --memory-id 1 --subject user --predicate prefers --object TypeScript --memory-type user_profile
python .\observability.py export
python .\observability.py export-md --path .\memory.md
python .\observability.py import --path .\memory-export.json
```

Useful health fields:

- `memory_review_queue_depth`: pending persisted review jobs ready to run.
- `embedding_backfill_queue_depth`: pending persisted embedding backfill jobs ready to run.
- `embedding_backfill_candidate_depth`: active semantic memories missing embeddings.
- `skipped_missing_embedding_backfills`: retrieval candidates that used local fallback embeddings because foreground remote backfill budget was capped.
- `rate_limit_429_errors`: recent provider 429 errors observed by memory-related operations.

## Cronjob Scheduling

The system includes a cron-like scheduler backed by SQLite. You can manage jobs using the CLI and execute them with the background worker.

To add a new scheduled job:
```powershell
python .\schedule_cli.py add --name "test_job" --cron "*/5 * * * *" --type "command" --payload '{"command": "echo Hello"}' --desc "Test command job"
```

To list all scheduled jobs:
```powershell
python .\schedule_cli.py list
```

*Note: The background scheduler worker runs automatically when you start `server.py`, `communication_server.py`, or `telegram_polling_worker.py`.*

To set up the default daily summary job:
```powershell
python .\add_default_jobs.py
```

## Tools

Core tools exposed to the model (additional tools are loaded from `tools/*.py` and matching JSON schemas):

- `read_file`: read a text file
- `list_files`: list files in a directory
- `write_file`: write text to a file
- `edit_file`, `delete_file`: replace text or delete a workspace file
- `run_command`: run a command with conditional local confirmation; communication turns use automatic approval
- `fetch_url`: fetch a URL using `curl`
- `run_skill`: run a local registered Skill
- `manage_memory`: confirm, contradict, archive or supersede memory
- `search_web`, `browse_webpage`, `analyze_image`: web search, browser text and image analysis
- `create_dynamic_tool`: create and reload a Python tool with its schema

## Skills

Local Skills live under `skills\<skill_name>\` with a `skill.json` metadata file and a `SKILL.md` instruction file. The first implementation supports deterministic `tool_sequence` Skills that call existing tools through an explicit allowlist.

Inspect Skills and Skill runs from the command line:

```powershell
python .\observability.py skills
python .\observability.py skill --name example_skill
python .\observability.py skill-runs --limit 20
```

## Tests

Run the test suite:

```powershell
python -m unittest discover -s tests -p "test_*.py"
```

Run syntax checks:

```powershell
python -m py_compile base.py server.py forgetting.py laya_guard.py memory.py memory_classifier.py memory_review.py observability.py procedure_similarity.py request_budget.py retrieval.py retrieval_ranker.py semantic_extractor.py skills.py vector_search.py working_memory.py tests\test_base.py tests\test_forgetting.py tests\test_laya_guard.py tests\test_memory.py tests\test_memory_classifier.py tests\test_memory_review.py tests\test_observability.py tests\test_procedure_similarity.py tests\test_retrieval.py tests\test_retrieval_ranker.py tests\test_semantic_extractor.py tests\test_skills.py tests\test_vector_search.py tests\test_server.py tests\test_working_memory.py
```

## 共用執行政策（T01）

`agent_execution/` 將模型工具、Skill 步驟、註冊動態工具、MCP、子代理及排程接入相同角色與路徑檢查。拒絕事件包含來源與身份；operator 不可藉由 run_skill 執行寫入／命令工具。Skill 的 trusted_asset_roots 僅由執行器指定，模型／Skill 參數不能自行擴大信任範圍。

Git 工具只操作 workspace 內的 repository。設定 `AGENT_WORKSPACE_ROOT` 指向欲開發的 repository；`build_codebase_index.py` 與 `search_codebase` 共用該 workspace 的 `codebase_index.db`。排程 command 建議使用 JSON argv，如 `{ "command": ["python", "job.py"] }`；舊的簡單 command 字串仍會轉成 argv，shell operator 字串會拒絕。舊排程以 local/admin 遷移，新排程保存建立者身份與角色。

應用政策不等同 OS 隔離；可執行 Python、shell 或第三方 MCP 的內部行為可用以下 T02 容器模式隔離命令。

## 命令執行與沙箱（T02）

```env
AGENT_EXECUTION_MODE=container
AGENT_COMMAND_TIMEOUT_SECONDS=60
```

`AGENT_EXECUTION_MODE` 接受 `host`／`container`。未設定時保留舊配置：`AGENT_USE_SANDBOX=1` 選 container，其餘為 host；執行時印出模式。host 為主機執行，不具 OS 隔離。所有內建 subprocess helper 使用有界 runner，命令預設 60 秒期限及每個 stdout／stderr 2 MiB 上限；超時、取消、輸出超限分別回傳 exit code 124、130、125。

container 必須有運作中的 Docker Linux daemon 及 `python:3.12-slim` image。沙箱建立不含 `.env`、envfile、私鑰、已知 credential 檔案／目錄與 symlink 的獨立唯讀工作副本；預設 network none、256 MiB RAM（無 swap）、1 CPU、64 PID，根檔案系統唯讀。可寫區僅有 `/tmp`（16 MiB）及 `/artifacts`（32 MiB），不提供主機可寫掛載。

腳本應將輸出寫至 `/artifacts`。`run_command` 可帶 `artifacts_directory` 指定 workspace 內的匯出目錄；只匯出有界、非敏感的 regular files，拒絕 archive traversal／symlink／覆寫。未指定時產出隨沙箱清理。provider credentials 不會透過 env_file 送入容器；需要第三方套件的任務須先準備相容 image／腳本，目前不在容器啟動時自動安裝。

Windows 使用 Job Object 清理子程序；Linux 使用程序群組。Docker 執行在結束、取消或超時後移除具唯一名稱的容器。內建 Python helper 為受信任 host 程式；第三方 MCP 及生成工具的發布、執行信任邊界仍列於 T18。

執行真實 Docker 回歸測試：

```powershell
$env:PYTHONAI_DOCKER_TEST="1"
python -m pytest tests agent_execution/tests -q
```

Docker 實作依據：[Resource constraints](https://docs.docker.com/engine/containers/resource_constraints/)、[tmpfs mounts](https://docs.docker.com/engine/storage/tmpfs/)。

## 遠端登入與批准（T03）

遠端身份未列於 `ROLES_CONFIG` 時為 viewer；本機 CLI 維持 admin。角色檔為 JSON，例如 `{"telegram:123":"admin","api:alice":"admin","api:observer":"viewer"}`。無效／無法讀取的設定不會授予 admin。

管理 API 須配置 `AGENT_API_TOKENS`，內容為 JSON 的「token → 身份名稱」對照；每個 token 至少 32 字元，建議以 `secrets.token_urlsafe(32)` 產生。使用身份 `alice` 時，角色檔的 key 為 `api:alice`。請將真實 token 放在私有環境設定，對外部署以 HTTPS 傳送。Dashboard 的 API Token 欄位支援登入，token 僅存於頁面輸入欄，不寫入 localStorage 或 URL。

API 請求帶 `Authorization: Bearer <token>`；未登入回 401，非 admin 的寫入回 403。`/health` 與 Dashboard HTML 可公開讀取。記憶與任務資料依遠端身份／專案隔離，admin 角色亦不會自動取得其他身份的資料。

`manage_memory` 的 archive／contradict／supersede 不接受模型自行以 `confirm=True` 授權。工具先回 `approval_required` 與 ID，人類可用 `/approvals` 查看原始操作，使用 `/approve <id>` 或 `/deny <id>` 決定，再要求 Agent 以該 `approval_id` 重試相同參數。CLI 會話具有獨立 ID；通訊會話綁定 platform／conversation 及 sender。批准期限 5 分鐘，綁定身份、會話、工具與操作參數，原子消耗一次；變更參數、跨會話、過期及重放均拒絕。此流程不會讀取遠端 stdin；授權後的普通遠端命令仍採非互動執行。

API 修改／封存／還原既有記憶先回 409 與操作內容。Dashboard 顯示確認後呼叫 `POST /api/approvals/<id>/approve`，再帶 `X-Agent-Approval` 重試；拒絕則呼叫 `/deny`。`X-Agent-Session` 綁定本頁會話，`GET /api/approvals` 僅列本身份與會話的 pending 操作。

批准資料及稽核預設保存於 Windows `%LOCALAPPDATA%/PythonAI/approvals.db`，其他 OS 為 `~/.local/share/PythonAI/approvals.db`；可由受信任部署設定 `AGENT_APPROVAL_DB`。請使用 workspace 外的路徑。批准政策限制模型工具入口及 HTTP 請求；具有使用者主機權限的 shell、已安裝 Python／MCP 程式仍是受信任程式，不能以應用層 SQLite 政策對抗它們，須使用 container 並完成 T18 的工具信任管理。

## Webhook 驗證與平台回覆（T04）

`COMM_WEBHOOK_MODE` 預設 `production`，未知值亦不允許跳過驗證。Telegram 缺 webhook secret、LINE 缺 channel secret、Discord 缺 public key／PyNaCl 時，請求回 403，不能入列。只有明確設定 `COMM_WEBHOOK_MODE=development` 才允許缺 secret 的本機測試；此模式不可用於公開部署。已有 secret 的 development 入口仍會檢查 secret／簽章。

```env
COMM_WEBHOOK_MODE=production
COMM_WEBHOOK_MAX_AGE_SECONDS=300
LINE_CHANNEL_ACCESS_TOKEN=<private-token>
LINE_CHANNEL_SECRET=<private-secret>
DISCORD_BOT_TOKEN=<private-token>
DISCORD_PUBLIC_KEY=<application-public-key>
```

`requirements.txt` 包含 `PyNaCl==1.6.2`。Discord 檢查真實 Ed25519 簽章與 timestamp；Telegram 的 message／edit_date 與 LINE 的 event timestamp 檢查預設 300 秒，容許最多 30 秒時鐘超前。較舊 LINE redelivery 會受此期限拒絕，可由部署者依業務需要調整上限；不宜以 development 模式繞過。各平台使用持久化 event ID 去重，重新送達可取得 ACK，但不能重複建立 job。Telegram polling 是獨立的已驗證 Bot API 入口，無需 webhook secret；Webhook 不會共用其驗證豁免。

Discord PING 回 `{"type":1}`，application command 回 `{"type":5}` deferred response；任務由送出 ACK 後的持久化 delivery gate 放行，避免先送 follow-up。interaction 的 application ID／token 保存於原始訊息，只供 adapter 回覆；14 分鐘內以 PATCH 更新原始回覆，後續分段使用 follow-up webhook，逾時採 Bot channel fallback，長訊息分成最多 2000 字元並停用自動 mentions。LINE 非同步任務使用 push API 回覆，避免過期 reply token，最多 5000 字元分段；需確保官方帳號有向使用者／群組 push 的資格及額度。平台拒絕／無效 JSON 回覆會視為傳送失敗。

本機已有實際 Ed25519 簽章、HTTP deferred response、重放去重、重啟後 follow-up metadata、polling 與 webhook 邊界測試；正式 Discord／LINE 帳號 round trip 尚待部署驗證。協定依據：[Discord interactions](https://docs.discord.com/developers/interactions/receiving-and-responding)、[LINE Messaging API](https://developers.line.biz/en/reference/messaging-api/)、[Telegram Bot API](https://core.telegram.org/bots/api)。

## 遠端會話與資料隔離（T05）

通訊 runtime／對話訊息／鎖以「platform + sender + conversation + project」區分。同一會話串行執行，不同會話可並行；取消 callback 只讀原 job 狀態，取消後不會被完成結果覆寫。`/status`、`/cancel`、`/clear`、ask_user 恢復皆限同一身份與會話／專案。`/clear` 取消該範圍的舊工作並重設自己的對話，保留長期記憶；自訂 runner 的 session_clearer 會收到原 AgentCommand，應依此清理。

`COMM_PROJECT` 選擇通訊服務專案，預設 `default`。多專案的命令 workspace 須由受信任設定 `AGENT_PROJECT_WORKSPACES` 指定 JSON 對照，例如 `{"default":"C:/projects/a","other":"C:/projects/b"}`；只接受絕對路徑，配置存在時缺專案即拒絕。未配置僅允許 default 使用既有 AGENT_WORKSPACE_ROOT；不以修改 process env 的方式切換不同會話的工作路徑。

遠端記憶保存於 `%LOCALAPPDATA%/PythonAI/tenants/<scope-hash>/memory.db`（其他 OS 為 `~/.local/share/PythonAI/tenants/`）。`AGENT_TENANT_ROOT` 可指定受信任 storage 根目錄。通訊 DB 範圍包含穩定的 conversation；REST 管理 DB 依 API 身份與 project 分配，Dashboard 重新登入仍可讀自己的記憶。`X-Agent-Project`／Dashboard 專案欄預設 default；批准亦綁定 project。舊 `MEMORY_DB_PATH` 仍屬本機資料，未自動复制給遠端使用者；不同平台身份不自動合併。

WebSocket session／outbox key 包含 user、project、device，使用者不能以同名 device 讀取他人事件；proactive 選擇限原 user／project。同一裝置重連會關閉舊連線，舊連線退出不會清除新連線。未配置 WebSocket tokens 時拒絕登入，沒有通用預設密碼。任務專用 worktree 仍待 T12。
