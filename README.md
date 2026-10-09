# PythonAI Agent

PythonAI 是以 OpenAI-compatible Chat Completions 為模型介面的 Python Agent，提供本機 CLI、Telegram long polling，以及整合 webhook、管理網頁、REST API、WebSocket 的 FastAPI 服務。核心包含工具呼叫、Skills、長期記憶、背景維護、排程、任務驗收及可擴充 Hooks。

以下盤點以 **2026-10-09 的程式與測試**為準。「已實作」表示有可追溯程式路徑，不代表已通過所有外部服務或正式環境驗證；「基礎實作」表示尚有整合、隔離或可靠性限制。後續改善及驗收條件見 [papers/todo.md](papers/todo.md)。

## 現有功能表

| 功能領域 | 現有能力 | 狀態與限制 | 實作依據 |
| --- | --- | --- | --- |
| Agent 執行迴圈 | 多輪模型／工具呼叫、讀取 AGENTS.md、規劃與反思提示、取消檢查 | 已實作；15 次迭代、持久化 task／checkpoint／循序依賴、總 token／request／deadline／子代理預算；跨代理 DAG 待增修 | `server.py`, `agent_runtime.py`, `agent_execution/budgets.py` |
| Provider 相容與重試 | OpenAI-compatible endpoint、文字／視覺／embedding 模型設定、能力探測、Retry-After | 已實作；關閉 SDK 隱藏重試，task 預算涵蓋重試；成本上限須配置模型費率，多 provider failover 尚未提供 | `server.py`, `agent_execution/budgets.py`, `agent_execution/model_probe.py` |
| 檔案與命令 | 讀寫、刪除、字串替換、列目錄、curl 擷取、命令執行 | 已實作；共用政策檢查角色與路徑／symlink，本機命令有條件確認；遠端通訊執行命令自動批准 | `base.py` |
| Skills | skill.json + SKILL.md、觸發詞匹配、輸入驗證、工具 allowlist、共用政策、步驟與結果紀錄、啟停 CLI | 已實作；唯一明確且可執行的匹配直接執行；多個匹配／指引型 Skill 交給模型；執行步驟目前為 tool_sequence | `skills.py`, `skills_cli.py`, `agent_runtime.py` |
| 動態工具 | 載入 tools/*.py + JSON schema、create_dynamic_tool 建立並重新載入 | 基礎實作；載入會執行 Python 模組，尚無發布前測試、版本審核及撤回流程 | `base.py`, `tools/` |
| MCP | stdio JSON-RPC 子程序、初始化、工具發現、schema 轉換、呼叫路由與 shutdown | 已實作；啟動失敗記於 skipped_servers；未見 remote HTTP/OAuth 與自動重連管理 | `mcp_manager.py`, `mcp_config.json` |
| Episodic memory | SQLite episodes／events、訊息、工具與驗收事件追蹤 | 已實作；task checkpoint 另存 TaskStore 並保留 episode 關聯；跨任務對話與摘要治理待 T09 | `memory.py`, `agent_runtime.py` |
| Semantic memory | 主詞／關係／受詞、user_profile／agent_persona 等類型、scope、信心、來源、實體關聯 | 已實作；遠端採身份／專案實體 DB 隔離，通訊另依會話隔離；本機既有資料保留 | `semantic_extractor.py`, `memory.py`, `agent_execution/sessions.py` |
| Procedural memory | 重用工作流程、相似度比對、成功／失敗計數、驗收狀態控制保存 | 已實作；不代表模型訓練或任意任務成功保證 | `memory_review.py`, `procedure_similarity.py`, `server.py` |
| 記憶檢索 | embedding／cosine similarity、Hashing fallback、關鍵詞、實體與程序檢索、可選 Laya 排序 | 已實作；本機候選與向量計算，尚無大型資料 ANN 索引 | `retrieval.py`, `retrieval_ranker.py`, `vector_search.py` |
| 工作記憶與遺忘 | token 上限、工具配對、目標／驗收／批准保留、TTL、矛盾／替代、程序淘汰 | task 模型輸入依保守 UTF-8 bytes 估算裁剪完整訊息組；權威 checkpoint 保留，legacy 無 task 入口仍使用摘要 | `agent_execution/context.py`, `working_memory.py`, `forgetting.py` |
| 背景記憶維護 | 持久化 review／embedding queue、請求預算、跨程序 worker lease、429 cooldown、stale embedding 更新 | 已實作；限制背景記憶流量，非全 Agent 的成本／併發治理 | `memory_worker.py`, `request_budget.py`, `observability.py` |
| 記憶管理 | confirm／contradict／archive／supersede、CLI 匯入匯出、網頁 CRUD／restore | 已實作；高風險模型工具與 API 更新要求身份／會話／參數綁定的一次性批准 | `memory_tools.py`, `agent_execution/approvals.py`, `web_api.py` |
| Telegram | webhook／polling、getMe 診斷、訊息去重、佇列、長訊息分段、圖片／document 接收 | 已實作；圖片轉 image_url、文字解碼及截斷；二進位附件以 Base64 文字傳入，非通用 PDF／Office 解析 | `communication_adapters/telegram_adapter.py`, `telegram_polling_worker.py`, `agent_runtime.py` |
| Discord／LINE | 簽章與時效驗證、去重、Discord deferred response／回覆閘門／原始回覆更新與 follow-up、LINE push／分段 | 已實作本機協定測試；production 缺配置／PyNaCl 拒絕，正式帳號 round trip 尚待驗證 | `communication_adapters/`, `communication_server.py`, `tests/test_webhook_security.py` |
| 通訊任務管理 | SQLite inbound／outbound／command、背景執行、/status、/cancel、/clear／/reset、ask_user 暫停及回覆續跑 | 已實作；ask_user schema、隔離 runtime、持久化附件／delivery／checkpoint；過期 lease 才恢復，副作用不確定轉 recovery_required | `communication_store.py`, `communication_worker.py`, `exceptions.py` |
| 統一 ASGI 與 Dashboard | /health、管理網頁、統計／任務 API、記憶 CRUD、三平台 webhook、/ws/v1 | 已實作；REST 管理路由具 Bearer／角色／資料隔離及人類批准；/health 僅回傳存活狀態 | `communication_server.py`, `web_api.py`, `web/index.html` |
| Companion WebSocket | hello token 驗證、seq／ack、SQLite 重啟補送、ping、chat interrupt、裝置事件及主動訊息路由 | 真實 AgentRuntime／模型 delta、工具、驗收、批准事件及模型／程序取消；owner-targeted Telegram adapter fallback；provider／實際送達驗收待辦，audio.end 尚未串接 STT／TTS | `websocket_server.py`, `tests/test_websocket_server.py` |
| Cron 排程 | croniter 計算、SQLite jobs／runs、工作鎖、背景 worker、command 與 maintenance.summary | 已實作；沒有通用 script／Skill／Agent job 執行器，保存建立者角色／身份，command 經共用政策以 argv 執行；共用 runner 有 timeout／取消／輸出上限 | `scheduler.py`, `schedule_cli.py`, `summary.py`, `add_default_jobs.py` |
| Guard 與 RBAC | 可選 Laya 請求／命令判斷、viewer／operator／admin、ROLES_CONFIG、API Bearer 登入 | 已實作共用政策；遠端缺角色配置預設 viewer，管理 API 寫入限 admin；Laya 不可用仍可繼續 | `laya_guard.py`, `permissions.py`, `agent_execution/api.py` |
| Hook Pipeline | Pre/PostModelCall、Pre/PostToolExecute；優先序、過濾、fail-open／closed、修改上下文、阻擋／提前回覆／回饋與稽核 | 已實作；default_hook_manager 是空 manager，需註冊政策 hook 才會執行政策 | `hooks.py`, `server.py`, `tests/test_hooks.py` |
| 任務驗收與自修正 | 檔案／Python 語法、測試輸出、Git 前後狀態、LLM Judge、Critic feedback | 已實作；預設最多 2 次驗收修正；規則／文字輸出及 Judge 有侷限，人工交接不等於完整審批 UI | `task_evaluator.py`, `tests/test_task_evaluator.py` |
| Sandbox | 明確 host/container 模式、Docker-only、唯讀工作副本、禁網路、CPU／RAM／PID／tmpfs 配額、安全 artifacts 匯出 | 已實作；container 無 Docker 即拒絕，不轉交主機 credentials，限 Linux 指令 | `sandbox.py`, `tests/test_sandbox.py` |
| 子代理 | planner／coder／reviewer daemon thread、JSON 狀態、invoke／check | 繼承角色／身份及父取消、共享根預算、独立 checkpoint；仍共享 workspace／工具，worktree、DAG、durable coordinator 及結果合併待增修 | `tools/invoke_subagent.py`, `tools/check_subagent.py` |
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
| 執行安全／Codex | 共用工具政策、Docker-only 隔離、禁網路、遠端最小權限與綁定批准 | 補工具信任／發布、網路按目的授權與更完整審查 UI；host 任意程式仍屬受信任執行 |
| 自動化／Codex | cron command 與每日摘要 | 補自然語言 Agent／Skill 任務、會話 follow-up、只於有意義變更通知、停止條件及審查結果 |
| 長任務／通用 Agentic | 有多輪、token 治理、持久暫停／續跑與總預算 | 已有循序步驟／checkpoint、lease 與保守副作用恢復；跨代理 DAG／結果收斂待 T13 |
| 完成度驗證／通用 Agentic | 已有 Critic loop 與來源事件 | 補使用者驗收條件、結構化工具結果、端到端測試、獨立 Reviewer 與任務品質評測 |
| 通訊／互動 | 多平台 adapters、WebSocket 協定、IDE 原型 | WebSocket 已串接隔離 runtime 與工具／批准事件；附件／可靠事件已持久化；尚需執行 checkpoint 及完整 UI |
| 記憶／個人化 | 本專案已有完整度較高的本機記憶維護與稽核模組 | 已補租戶／專案隔離，仍需檢索品質測試及版本治理；不推論 Codex 缺少記憶能力 |
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


## 真實 WebSocket Agent 串流（T06）

`/ws/v1` 使用服務的 AgentRuntime 模板，按 user／project／device 建立獨立對話與記憶。`chat.send` 呼叫模型 `stream=True`，組裝分段 function calls；收到完整 finish_reason 才執行工具，不執行截斷的參數。回傳 `model.start`、`chat.delta`、`tool.start`、`tool.result`、`task.evaluation`、`approval.required`、`error` 與 `chat.end`，工具仍經共用政策及 Hooks。直接 Skill 執行亦回傳工具事件；不產生虛構模型 delta。

同一 session 執行中再次 chat.send 回 chat_busy。`chat.interrupt`／斷線將取消傳入模型串流與有界程序 runner；清理完成才結束任務。已建立的模型串流會關閉；建立連線中的 SDK 請求有最長 60 秒 timeout，尚不能保證立即取消 DNS／connect。發送事件 timeout 為 5 秒，內部事件 queue 上限 128，避免慢客戶端無限堆積。音訊仍是協定 placeholder。

批准可用人類 `/approvals`、`/approve <id>`、`/deny <id>`，或 `approval.decide` payload `{"approval_id":"...","decision":"approve"}`。身份、project／device session 與角色均由 transport 決定；操作仍須以相同參數及 approval_id 重試。

高優先 proactive 在無在線裝置時呼叫 Telegram adapter，部署者须設定 `COMPANION_TELEGRAM_TARGETS={"alice:default":"<authorized-chat-id>"}` 並配置 Bot token。缺明確 owner／project 對照即拒絕，不將訊息送到其他帳號；API 失敗不記為成功 fallback。

驗證分開紀錄：SDK 本機 HTTP SSE 整合、完整 Agent 讀檔、分段工具參數、截斷拒絕、模型／實際 host 程序取消、批准隔離與忙碌拒絕測試通過；真實 provider opt-in 測試於 2026-10-09 因配置端點 TCP 443 連線逾時失敗（DNS 成功），外部 provider 與 Telegram 實際送達尚未驗收。重現 provider 測試：設定 `PYTHONAI_PROVIDER_TEST=1` 後執行 `python -m pytest agent_execution/tests/test_streaming.py -q`。串流參數依據 [OpenAI function calling](https://developers.openai.com/api/docs/guides/function-calling)。


## 附件與可靠訊息持久化（T07）

通訊附件已移除 process-local `_PENDING_ATTACHMENTS`，圖檔與一般文件的 bytes、SHA-256、MIME、檔名、platform file ID 及 TTL 保存於 communication SQLite。正式 ingest 將原始訊息、job 與附件寫在同一交易中；重複 event 不新增工作或覆寫附件。worker 在執行前載入原 job 的附件，驗證 owner／conversation／project、大小與 SHA-256；過期附件明確失敗，不能默默改成無附件請求。檔名拒絕路徑／NUL，MIME 檢查格式及常見 PNG／JPEG／GIF／WebP／PDF magic；這不是惡意文件掃描器，附件不會以其檔名寫到主機路徑。

預設每檔 20 MiB、每工作 16 檔／32 MiB，總附件 128 MiB／10000 筆，TTL 7 天。`COMM_ATTACHMENT_MAX_BYTES`、`COMM_ATTACHMENT_TOTAL_BYTES`、`COMM_ATTACHMENT_COUNT`、`COMM_ATTACHMENT_TTL` 可由部署者調整。Telegram下載亦按檔案及讀取上限拒絕過大資料。`cleanup_expired()` 清掉過期 bytes，保留過期 metadata 防止缺檔後悄悄執行；metadata 達上限須由管理者依保留政策整理。DB 是受信任服務資料，主機 OS 帳號仍可存取它，不提供模型可任意指定路徑的附件讀取 API。

WebSocket 可靠事件在傳送前存 SQLite，seq 單調遞增，ACK 原子刪除已確認事件。新程序以相同 user／project／device 回放未 ACK 事件；client 依穩定 event id／seq 去重，補送只回傳事件，不重跑工具。`chat.send` 須有 1–128 字元穩定 id；相同內容與 id 於保留期間不執行第二次，換內容則拒絕。crash 後未結束的 input 標為 uncertain，不能推測工具是否已完成。`welcome` 提供 last_seq／history_floor；TTL 造成歷史缺口時 resumed=false，client 須顯示缺口並以 history_floor 重新同步。

`COMPANION_EVENT_DB` 預設 workspace 外 `%LOCALAPPDATA%/PythonAI/companion.db`（其他 OS 為 ~/.local/share/PythonAI）。`COMPANION_EVENT_TTL=86400`、`COMPANION_INPUT_TTL=604800`、`COMPANION_OUTBOX_LIMIT=200`、`COMPANION_EVENT_BYTES=134217728`、`COMPANION_INPUT_LIMIT=10000`、`COMPANION_MAX_SESSIONS=10000`。可靠 outbox 額滿拒絕新事件並取消工作／關閉 4429，不靜默丟棄舊事件。單事件上限 2 MiB、單 session 20 MiB；client 輸入上限 1 MiB。audio metadata／client tool result 亦有記憶體筆數上限。

通訊回覆有持久化 delivery intent、原子 claim、120 秒 lease、內容去重及身份範圍。重啟可送出尚未 claim 的 pending；傳送逾時或 lease 過期記為 uncertain，保留模型成果且不自動重送，以免平台已收件但 response 遺失時重複送達。送達成功才寫 outbound sent；pending 超过 `COMM_DELIVERY_TTL`（預設 7 天）不再發送，成功紀錄亦依期限清理。`COMM_DELIVERY_LIMIT=10000`、`COMM_DELIVERY_BYTES=134217728` 限制儲存。正式平台沒有通用 exactly-once 契約，uncertain 需人工查平台後處理；不以模型成功代表送達成功。

相關測試涵蓋真正新程序讀回圖檔／中文文件、身份／專案／會話越界、竄改與到期、交易回滾、工作成果與傳送失敗分離、claim／重啟、WebSocket seq／ACK／歷史缺口與不重複執行。對話 runtime／模型執行 checkpoint 的恢復另由 T08 處理。


## 持久化任務與 crash 恢復（T08）

CLI、通訊 runtime、WebSocket 以 transport 指定的 task ID 保存 goal、criteria（目前為原始目標）、messages、episode、模型迭代／Critic 次數、驗收 pre-state、循序工具輸入／結果／依賴及 checkpoint。`AGENT_TASK_DB` 預設 `%LOCALAPPDATA%/PythonAI/tasks.db`（其他 OS 為 ~/.local/share/PythonAI），按 source／identity／project／session 授權。新工具任務不直接重播舊對話中未配對的 tool call。

checkpoint 包含 model_pending／model_received、tool_started／tool_done、evaluation_pending／evaluation_done、waiting_user、skill_started／skill_done。工具結果保存後恢復會略過已完成 call；驗收結果保存後不重跑 Judge／測試。程序在 tool_started／skill_started 階段消失時，可能已產生副作用，任務轉 uncertain；通訊 job 顯示 recovery_required，不自動重做寫入。完整直接 Skill 結果亦持久化，恢復／快取維持乾淨 stdout；Skill 中途 crash 需人工確認，不能盲目重跑整段。

每任務原子 claim、180 秒 lease、20 秒 heartbeat 及 fencing token；過期舊 worker 不能再提交 checkpoint，新 worker 不會接手仍有 lease 的工作。取消檢查亦讀持久狀態；模型連線建立中仍有 T06 所述 timeout 限制。通訊 loop 自動恢復有 checkpoint、lease 已過期的安全 running 工作；未保存任何 task checkpoint 的舊版 running job 不猜測執行狀態，保留人工處理。crash 恢復最多 3 次；正常人類回答不消耗此恢復次數。

人類入口（不提供給模型工具 schema）：

- `/tasks`：列出自己的 task ID、goal、state／phase、episode 與 attempts。
- `/steps <task_id>`：列出自己的工具步驟、輸入／結果與依賴。
- `/resume <task_id>`：續跑安全 checkpoint 或讀取已完成結果。
- `/cancel-task <task_id>`：取消自己的持久任務。
- `/clear`／`/reset`：取消本 session 尚未完成的持久任務並清除對話；不會在下一次回答恢復舊暫停任務。
- `/resolve <task_id> <tool_call_id> <observed_result>`：admin 在查核真實副作用後填入對應不確定工具的結果，再 /resume；不重做原工具，也不把人類文字直接視為已通過驗收。整段 Skill 中途失敗尚不可逐步 resolve，須查核後取消及另開工作。

`ask_user` 已列入預設 schema 並經工具政策驗證問題長度。CLI／WebSocket 重啟後，對同一身份／project／session 的普通回答會續接最近的 suspended task；通訊亦沿用持久化問答機制。tool call/result 配對及原 episode ID 保留。CLI `AGENT_SESSION_ID` 預設 local，需分開多個終端時設定不同且穩定的 session ID，重啟使用相同值。未知平台身份不自動合併。

task 最多 10000 筆／256 MiB（`AGENT_TASK_LIMIT`、`AGENT_TASK_STORAGE_BYTES`），單 checkpoint 64 MiB、人類回答／resolve 1 MiB。達上限拒絕新工作／checkpoint，不刪舊資料；管理者須規劃保留與匯出。task completed 表示迴圈已回覆，真正的 verified_completed 條件由 T10 結構化驗收補強；不把 checkpoint 等同任務品質證明。多代理計畫 DAG／worktree 分別由 T13／T12 擴充。

驗證包含以子程序 `os._exit` 在實際已提交的 checkpoint 結束、在新執行環境續跑；模型回覆後、工具結果後與驗收前後均不重複檔案寫入，工具開始但未保存結果時停待人工。另測試 lease／舊 token／owner、取消、快取、容量交易、Skill stdout、人類回答與原 episode、WebSocket runtime 重建後問答續接。

## 任務預算、模型路由與上下文（T09）

CLI／通訊／WebSocket 的 task 預算與 checkpoint 保存於同一 `AGENT_TASK_DB`，重啟及 `/resume` 不重設 token、request、成本、deadline 或子代理額度。子代理共用根任務額度，但使用獨立 checkpoint；記憶 embedding、前景抽取、Judge 與明確重試亦扣除額度。背景記憶維護仍使用既有 request_budget，沒有將無關背景工作算到另一位使用者的 task。

| 設定 | 預設 | 意義 |
|---|---:|---|
| `AGENT_TASK_MAX_TOKENS` | 256000 | 累計輸入及輸出保留額度 |
| `AGENT_TASK_MAX_REQUESTS` | 24 | 包含失敗、timeout 及明確重試 |
| `AGENT_TASK_DEADLINE_SECONDS` | 600 | 建立起算的 wall deadline，包含人工暫停 |
| `AGENT_TASK_MAX_CHILDREN`／`AGENT_TASK_MAX_DEPTH` | 4／2 | 累計子代理數與巢狀深度；0 禁止 |
| `AGENT_MODEL_MAX_OUTPUT_TOKENS` | 2048 | 每次生成上限 |
| `AGENT_MODEL_CONTEXT_TOKENS` | 32768 | 預設單模型輸入＋輸出視窗 |
| `AGENT_MODEL_CONCURRENCY` | 4 | 同一 task DB 的跨程序模型 slots |
| `AGENT_TASK_CONCURRENCY` | 4 | 每個 communication worker 的背景工作上限 |
| `AGENT_TASK_MAX_COST_USD` | 0 | 0 表示未啟用成本限制 |

每次請求先原子保留預估 token／成本；失敗、取消或缺 usage 不退還。供應商 usage 高於保留量時向上記帳並阻止後續模型／工具工作；估算不是帳單保證。token 使用 JSON UTF-8 bytes 的保守估算，包含 schemas 與圖片 Base64；較大圖片可能超出 context 並明確拒絕，需按已驗證的模型視窗調整設定，圖片不會被悄悄移除。若啟用成本上限，須配置 `AGENT_MODEL_RATES={"model-id":{"input":1,"output":5}}`（USD／百萬 tokens）；缺費率拒絕請求，費率與上限在根任務建立時保存。`/budget <task_id>` 僅顯示本人會話的用量、上限、成本是否啟用及剩餘秒數。

SDK `max_retries=0`、單請求 timeout 最多 60 秒且不超過剩餘 deadline；串流有 60 秒 wall 上限、usage chunk 記帳、取消關閉及 slot 清理。未知模型保留原有相容模式；`OLLAMA_MODEL`、`OLLAMA_VISION_MODEL`、`OLLAMA_EMBEDDING_MODEL` 分別選文字、圖像及 embedding。`AGENT_MODEL_CAPABILITIES={"model-id":{"chat":true,"vision":false,"embedding":false,"available":true,"context_tokens":32768,"output_parameter":"max_completion_tokens"}}` 可保存部署者已查證的能力。明確 vision=false 拒絕圖片分析；browser_vision 可標示為 DOM 文字 fallback。未配置能力代表未知，不代表已驗證支援。

預設使用 `max_completion_tokens`；不支援此參數的 compatible provider 須配置 capability `output_parameter=max_tokens` 或 `AGENT_OUTPUT_PARAMETER=max_tokens`。不因 HTTP 400 靜默移除輸出上限。串流要求 `stream_options.include_usage=true`；中斷可能缺 usage，因此仍保留扣額，API 參數依據 [OpenAI Chat Completions](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)。

可選的實際 provider 探測（每請求最長 15 秒、無隱藏重試、不傳 workspace 內容）：`python -m agent_execution.model_probe --model <id>`，加 `--vision` 測圖像，或 `--embedding` 測 embedding。成功僅證明請求可接受，不驗證語意品質；任何失敗回報 unknown 與錯誤類別／HTTP status，不洩露完整 provider error 或 credentials，也不自動更改配置。

模型輸入超限時只刪完整舊訊息組，保留 system／developer、最新使用者輸入、task goal／criteria、批准及其工具配對；hook feedback 排在完整工具結果後。壓縮資料保持 user 權限，不提升為 system 指令。無法在上限內保留必要內容則拒絕，權威 checkpoint 不被裁剪。未知副作用與完整交付品質仍分別依 T08／T10 處理；共享 workspace 的子代理衝突、durable coordinator 與 reviewer 收斂待 T12／T13。

完整驗證（Docker／Chromium 開啟）：444 passed、1 skipped（外部 provider）、7 subtests passed；新增預算與上下文測試包含真實 SDK 的 HTTP 429 無隱藏重試、root deadline 取消 Windows host 程序與 blocked stream、併發扣额、重啟與子代理額度／checkpoint。實際部署模型能力及費率未由這些 fixture 驗證。
