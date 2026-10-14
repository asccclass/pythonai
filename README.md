# PythonAI Agent

PythonAI 是以 OpenAI-compatible Chat Completions 為模型介面的 Python Agent，提供本機 CLI、Telegram long polling，以及整合 webhook、管理網頁、REST API、WebSocket 的 FastAPI 服務。核心包含工具呼叫、Skills、長期記憶、背景維護、排程、任務驗收及可擴充 Hooks。

MyAI／WebSocket 任務若出現 `Role 'viewer' cannot execute ...`，代表連線身份沒有該工具權限。請核對實際服務使用的 `ROLES_CONFIG` 與 token 身份；無身份 token 對應 `websocket:user`，助手自動啟動的 token 對應 `websocket:desktop`。角色檔內容於新指令重新讀取，修改角色檔路徑或更新後端程式後需重新啟動服務。`viewer` 不允許 Skills；需要 Skills 的身份可配置 `operator`，管理工具依既有政策要求 `admin`。模型工具清單依角色及任務 allowlist 過濾，執行端仍檢查權限。

任務驗收只檢查當次任務的對話與工具呼叫，保留此邊界供中斷後續跑使用；先前對話的程式操作不會讓待辦查詢被要求提供程式測試。被權限拒絕的程式工具呼叫也不構成程式開發證據，但真正的程式需求仍須通過測試。權限拒絕停止 Critic 自動重試，保留失敗狀態並回報權限原因；不會誤稱已用盡修正次數。

任務 token 預算在送出請求前保留保守估計，成功回應或正常結束的串流若有有效 `usage.total_tokens`，會以實際回報量結算；失敗、串流中斷或沒有有效 usage 的請求仍保留預估費用。請求次數不退回，成本上限仍採保守結算，既有任務的限制與期限也不會因重啟而增加。耗盡訊息會指出 requests、tokens、cost 或 deadline；不再只顯示混合錯誤。

Go 程式可使用 `project_scaffold(framework="go_basic")` 建立獨立目錄，內含程式、`go.mod`、測試與 README。`run_test_suite` 預設 auto，對 `.go` 檔案或含 `go.mod` 的目錄執行 `go test -json ./...`，以 JSON 測試事件驗證通過數；零測試或僅有成功文字不算通過。驗收會辨識 `_test.go` 目標。宿主需安裝 Go；容器模式的映像也需提供 Go。

以下盤點以 **2026-10-09 的初始盤點及 2026-10-10 的增修**為準。「已實作」表示有可追溯程式路徑，不代表已通過所有外部服務或正式環境驗證；「基礎實作」表示尚有整合、隔離或可靠性限制。後續改善及驗收條件見 [papers/todo.md](papers/todo.md)。

## 現有功能表

| 功能領域 | 現有能力 | 狀態與限制 | 實作依據 |
| --- | --- | --- | --- |
| Agent 執行迴圈 | 多輪模型／工具呼叫、讀取 AGENTS.md、規劃與反思提示、取消檢查 | 已實作；15 次迭代、持久化 task／checkpoint／循序依賴、總 token／request／deadline／子代理預算；跨代理 DAG、獨立 Reviewer 與結果合併 | `server.py`, `agent_runtime.py`, `agent_execution/budgets.py` |
| Provider 相容與重試 | OpenAI-compatible endpoint、文字／視覺／embedding 模型設定、能力探測、Retry-After | 已實作；關閉 SDK 隱藏重試，task 預算涵蓋重試；成本上限須配置模型費率，多 provider failover 尚未提供 | `server.py`, `agent_execution/budgets.py`, `agent_execution/model_probe.py` |
| 檔案與命令 | 讀寫、刪除、字串替換、列目錄、curl 擷取、命令執行 | 已實作；共用政策檢查角色與路徑／symlink，本機命令有條件確認；遠端通訊執行命令自動批准 | `base.py` |
| Skills | skill.json + SKILL.md、觸發詞匹配、輸入驗證、工具 allowlist、共用政策、步驟與結果紀錄、啟停 CLI | 已實作；唯一明確且可執行的匹配直接執行；多個匹配／指引型 Skill 交給模型；執行步驟目前為 tool_sequence | `skills.py`, `skills_cli.py`, `agent_runtime.py` |
| 動態工具／Skill 版本 | builtin inventory、生成版本 stage、來源／digest、Docker tests、人工核准啟用／撤回／rollback | 已實作；未批准不 import；啟用 Python 仍屬 trusted host code，需重啟刷新 schema | `base.py`, `agent_execution/extensions/` |
| MCP | stdio／remote Streamable HTTP、exact route、schema／結果驗證、health、明確 reconnect、OAuth discovery／PKCE／refresh | 已實作本機協定案例；缺 server 不阻塞其他工具；401／不確定工具結果不重播；正式帳號尚待驗收 | `mcp_manager.py`, `agent_execution/extensions/` |
| Episodic memory | SQLite episodes／events、訊息、工具與驗收事件追蹤 | 已實作；task checkpoint 另存 TaskStore 並保留 episode 關聯；稽核事件 redact，原對話／checkpoint 保留於私有儲存 | `memory.py`, `agent_runtime.py` |
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
| Companion WebSocket | hello token、seq／ack／SQLite 補送、模型 delta／工具事件、interrupt、裝置與 STT／TTS 音訊 | owner 隔離、有序有界 frames／WAV／取消與重連；本機 SDK HTTP contract 通過，正式語音品質／裝置播放仍待驗收 | `websocket_server.py`, `agent_execution/media/audio.py` |
| Agent／Skill 排程 | cron／精確自然語句、Taipei 時區、command／script／Agent／Skill／watch、lease／fencing、quiet follow-up | 人工批准、owner 還原、timeout／misfire／有界 retry；通訊入口原會話通知、不確定發送不重送；正式平台送達待驗收 | `scheduler.py`, `schedule_cli.py`, `agent_execution/scheduling/` |
| Guard 與 RBAC | 可選 Laya 請求／命令判斷、viewer／operator／admin、ROLES_CONFIG、API Bearer 登入 | 已實作共用政策；遠端缺角色配置預設 viewer，管理 API 寫入限 admin；Laya 不可用仍可繼續 | `laya_guard.py`, `permissions.py`, `agent_execution/api.py` |
| Hook Pipeline | Pre/PostModelCall、Pre/PostToolExecute；六項內建政策、可配置啟停／排序／fail-closed、啟動診斷、阻擋／提前回覆／回饋與身份稽核 | 已實作；模型、直接工具、Skill／排程共用政策；核心權限／路徑／批准不受停用 Hook 影響 | `hooks.py`, `agent_execution/hook_policies.py`, `server.py` |
| 任務驗收與自修正 | 凍結驗收契約、結構化工具結果、檔案／語法、JUnit／process、Judge 與 Critic | 明確條件及證據才能 verified_completed；Judge 無效／失效拒絕、不可修正失敗直接交人工；直接 Skill 亦驗收 | `task_evaluator.py`, `agent_execution/acceptance.py`, `agent_execution/results.py` |
| Sandbox | 明確 host/container 模式、Docker-only、唯讀工作副本、禁網路、CPU／RAM／PID／tmpfs 配額、安全 artifacts 匯出 | 已實作；container 無 Docker 即拒絕，不轉交主機 credentials，限 Linux 指令 | `sandbox.py`, `tests/test_sandbox.py` |
| 子代理 | 持久化 DAG、planner／coder／reviewer 獨立程序、follow-up／等待／取消／合併 | 獨立工具範圍、checkpoint、worktree；共享根預算；Coder 須通過獨立 Reviewer，內容衝突禁止覆寫 | `agent_execution/coordinator.py`, `tools/manage_subagent.py` |
| 搜尋、網頁與視覺 | Wikipedia／DuckDuckGo、網頁正文、Playwright owner profile／cookies、斷言／私有下載、圖片模型與 DOM fallback | 實際 Chromium 登入續跑／隔離通過；有界網路政策；host 瀏覽器仍需部署隔離，未提供桌面操作 | `base.py`, `agent_execution/media/`, `tools/browser_session.py` |
| 文件附件／OCR | PDF／DOCX／PPTX／XLSX 文字與來源單位、scanned PDF Tesseract OCR | 原生 worker／ZIP／XML／輸出限額；未知二進位明確不支援；DOCX 以段落來源、不杜撰頁碼 | `agent_execution/media/documents.py`, `agent_runtime.py` |
| 程式碼理解與修改 | Python／Go／JS／TS 增量符號／引用、版本化向量、精確文字搜尋、LSH ANN、Jedi／可配置 LSP、AST 替換與回復 | 每次查詢同步；ignore／秘密過濾；語法候選與語意解析分開標示；一般 workspace 內索引、任務 worktree 使用外部 cache | `agent_execution/code_index/`, `build_codebase_index.py`, `tools/code_symbols.py`, `tools/search_codebase.py`, `tools/go_to_definition.py`, `tools/find_references.py` |
| 開發工具 | Git status／diff、選檔 commit／批准後選檔 rollback、task worktree／封存／恢復、pytest／Python runner、venv／pip／npm、四種 scaffold | 已有 worktree 隔離與本機交付；測試 runner timeout 為 30 秒 | `agent_execution/worktrees.py`, `tools/git_*.py`, `tools/run_test_suite.py` |
| Patch／PR／CI | 多檔內容雜湊驗證、diff 預覽、GitHub PR／GitLab MR、確切 SHA 發布、CI／審查門檻、同 PR 修復與交付摘要 | 發布／合併各需一次性批准；平台 API 以本機 fixture 驗證，外部帳號尚未實測 | `agent_execution/patches.py`, `agent_execution/git_delivery.py`, `agent_execution/git_provider.py`, `tools/project_delivery.py` |
| IDE 整合與任務審查 | VS Code 提案 diff、明確單次套用／undo、Ghost Text；Dashboard 計畫／工具／驗收／批准／artifact | 已實作 T16；Token／角色／會話隔離、UTF-8 串流完整性與取消、文件版本檢查；跨通道身份連結與外部 provider 仍待驗證 | `ide_server.py`, `vscode-extension/` |
| 可控改善／記憶品質 | Doctor stage、owner persona／Skill 版本、來源／矛盾／TTL、retrieval 評測、固定案例 A/B／核准／撤回 | 改善且零回歸／非預期副作用才允許批准；未啟用 production persona，較廣 provider 品質仍待驗收 | `skill_doctor.py`, `agent_execution/improvement/`, `retrieval.py` |
| 觀測與運維 | owner task／episode／node trace、token／費用／latency／retry、redaction、readiness／doctor、SQLite backup／restore／migration、fixed benchmark／分層 CI | 十五項本機失敗案例基準；未知費率不當零成本；備份驗證後還原新目錄；GitHub CI 與正式服務尚待執行 | `observability.py`, `agent_execution/operations/`, `.github/workflows/verification.yml` |

附帶資源包括 `skills/story/` 的敘事分析／motion 程式與測試，以及 `skills/security_audit/` 的安全審查指引及 Node 報告驗證器。它們是個別工作流程或應用；目錄存在不表示全部已註冊為可執行 Skill。`patch*.py`、`fix*.py`、`drill.py`、`demo_scenario.py` 等維護／示範腳本也不視為 Agent 正式入口。

## 與 Codex、通用 Agentic Agent 的差距

「Agentic Agent」在此指能規劃、使用工具、驗證、保存狀態並持續完成任務的通用自主代理架構，不是特定產品。Codex 比較以官方文件的工作流程為基準；帳號、平台與版本可影響可用功能。本次是實作盤點，不是模型品質或成功率 benchmark。

| 比較面向 | PythonAI 現況 | 相對差距與下一步 |
| --- | --- | --- |
| 編碼交付／Codex | 已有編輯、增量多語言索引、選檔 Git、測試、Judge、task worktree、patch、跨代理合併與 PR／CI 門檻 | 外部平台／正式 LSP 實測、跨通道身份連結與完整品質基準仍待完成 |
| 多代理／Codex | 持久化 DAG、獨立上下文／權限／worktree、follow-up／cancel、Reviewer／衝突管理 | 正式多代理任務品質及不同 provider 配置仍需 benchmark |
| 執行安全／Codex | 共用工具政策、Docker-only 隔離、禁網路、遠端最小權限與綁定批准 | 補工具信任／發布、網路按目的授權與更完整審查 UI；host 任意程式仍屬受信任執行 |
| 自動化／Codex | Agent／Skill／watch 排程、時區／精確自然語句、停止條件與原會話通知 | 真實 provider 追蹤及正式平台送達；其他通道推送需明確收件人設計 |
| 長任務／通用 Agentic | 有多輪、token 治理、持久暫停／續跑與總預算、PR／CI 修復流程 | 已有步驟／checkpoint、跨代理 DAG、lease 與保守副作用恢復；品質 benchmark 待 T21 |
| 完成度驗證／通用 Agentic | 驗收契約、結果／來源事件、Critic、獨立 Reviewer 及固定失敗基準 | 正式模型端到端品質評測；本機 fixture 成功率只代表明確案例範圍 |
| 通訊／互動 | 多平台 adapters、WebSocket 協定、IDE 提案與任務審查 | WebSocket 已串接隔離 runtime 與工具／批准事件；附件／可靠事件已持久化；已有執行 checkpoint 與 T16 審查 UI；跨通道身份連結仍需明確配置設計 |
| 記憶／個人化 | 租戶／專案隔離、來源／矛盾／過期測試、版本化改善與 A/B gate | 較廣標記資料集、正式 provider 改善量與長期漂移；不推論 Codex 缺少記憶能力 |
| 擴充／整合 | 工具／Skill 核准生命週期、schema／結果驗證、remote MCP／OAuth／health | 正式 issuer 帳號 consent／callback 驗收；dynamic registration／confidential client 尚未提供 |

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

Polling startup checks `getWebhookInfo` and obtains a per-bot native lock. A configured webhook is rejected; the worker does not delete it or discard pending updates. One polling consumer must own a Bot across all hosts. A getUpdates-consumer HTTP 409 can be a short handoff from an earlier long poll. The worker retains its local lock and permits two delayed retries by default; each wait is at least the configured poll timeout plus five seconds (normally 35 seconds). It logs successful recovery. The conflict budget is for the entire run, including intervening successful polls, so two active consumers cannot retry forever. A configured webhook, unknown conflict or exhausted conflict budget stops with code 1 and cleans scheduler/memory workers. Stop any persistently active other polling instance, including remote services, before restarting. `TELEGRAM_POLL_CONFLICT_RETRIES` accepts 0..3 (0 keeps immediate conflict exit); optional `TELEGRAM_POLL_CONFLICT_SLEEP` accepts 1..300 seconds. Transient polling failures such as HTTP 502 still retry. Hook policy and loaded-Skill lines are normal startup diagnostics.

The default lock directory is `%LOCALAPPDATA%/PythonAI/telegram-polling` on Windows, with hashed Bot IDs; locks release on exit/crash. The lock file also records an in-flight request deadline without credentials. If a process is interrupted or loses its connection before a response, its next request or restarted worker waits for the recorded timeout plus ten seconds before polling again. Successful responses and explicit HTTP errors clear the record immediately. Recovery waits are capped at 310 seconds; `TELEGRAM_POLL_TIMEOUT` accepts 0..300 seconds. `AGENT_TELEGRAM_POLL_LOCK_DIR` can override this directory. Keep the same directory for processes that should exclude each other; different users/hosts need deployment-level single-consumer coordination. A 409 does not identify the other consumer or guarantee it will expire after 35 seconds; repeated conflicts require stopping the other deployment. See [Telegram getUpdates](https://core.telegram.org/bots/api#getupdates) and [getWebhookInfo](https://core.telegram.org/bots/api#getwebhookinfo).

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

指定 `ROLES_CONFIG` 時，以 JSON 角色檔為準，例如 `{"telegram:123":"admin","api:alice":"admin","api:observer":"viewer"}`；身份未列出、角色無效或檔案無法讀取皆為 viewer。未指定角色檔時，`COMM_ADMIN_SENDERS` 中精確列出的 `telegram:<sender ID>`、`discord:<sender ID>`、`line:<sender ID>` 為 admin，其餘遠端身份仍為 viewer；本機 CLI 維持 admin。`COMM_ALLOWED_SENDERS` 只控制訊息准入，不授予管理員角色。修改 `.env` 後須重新啟動 worker 以載入設定。

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

WebSocket session／outbox key 包含 user、project、device，使用者不能以同名 device 讀取他人事件；proactive 選擇限原 user／project。同一裝置重連會關閉舊連線，舊連線退出不會清除新連線。未配置 WebSocket tokens 時拒絕登入，沒有通用預設密碼。Git 專案可依 T12 設定任務專用 worktree。


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

CLI、通訊 runtime、WebSocket 以 transport 指定的 task ID 保存 goal、criteria（原始目標及 T10 起始驗收契約）、messages、episode、模型迭代／Critic 次數、驗收 pre-state、循序工具輸入／結果／依賴及 checkpoint。`AGENT_TASK_DB` 預設 `%LOCALAPPDATA%/PythonAI/tasks.db`（其他 OS 為 ~/.local/share/PythonAI），按 source／identity／project／session 授權。新工具任務不直接重播舊對話中未配對的 tool call。

checkpoint 包含 model_pending／model_received、tool_started／tool_done、evaluation_pending／evaluation_done、waiting_user、skill_started／skill_executed／skill_done。工具結果保存後恢復會略過已完成 call；驗收結果保存後不重跑 Judge／測試。程序在 tool_started／skill_started 階段消失時，可能已產生副作用，任務轉 uncertain；通訊 job 顯示 recovery_required，不自動重做寫入。直接 Skill 在 skill_executed 後 crash 只續驗收，恢復／快取維持乾淨 stdout；Skill 中途 crash 需人工確認，不能盲目重跑整段。

每任務原子 claim、180 秒 lease、20 秒 heartbeat 及 fencing token；過期舊 worker 不能再提交 checkpoint，新 worker 不會接手仍有 lease 的工作。取消檢查亦讀持久狀態；模型連線建立中仍有 T06 所述 timeout 限制。通訊 loop 自動恢復有 checkpoint、lease 已過期的安全 running 工作；未保存任何 task checkpoint 的舊版 running job 不猜測執行狀態，保留人工處理。crash 恢復最多 3 次；正常人類回答不消耗此恢復次數。

人類入口（不提供給模型工具 schema）：

- `/tasks`：列出自己的 task ID、goal、state／phase、episode 與 attempts。
- `/steps <task_id>`：列出自己的工具步驟、輸入／結果與依賴。
- `/resume <task_id>`：續跑安全 checkpoint 或讀取已完成結果。
- `/cancel-task <task_id>`：取消自己的持久任務。
- `/clear`／`/reset`：取消本 session 尚未完成的持久任務並清除對話；不會在下一次回答恢復舊暫停任務。
- `/resolve <task_id> <tool_call_id> <observed_result>`：admin 在查核真實副作用後填入對應不確定工具的結果，再 /resume；不重做原工具，也不把人類文字直接視為已通過驗收。整段 Skill 中途失敗尚不可逐步 resolve，須查核後取消及另開工作。

`ask_user` 已列入預設 schema 並經工具政策驗證問題長度。CLI／WebSocket 重啟後，對同一身份／project／session 的普通回答會續接最近的 suspended task；通訊亦沿用持久化問答機制。tool call/result 配對及原 episode ID 保留。CLI `AGENT_SESSION_ID` 預設 local，需分開多個終端時設定不同且穩定的 session ID，重啟使用相同值。未知平台身份不自動合併。

task 最多 10000 筆／256 MiB（`AGENT_TASK_LIMIT`、`AGENT_TASK_STORAGE_BYTES`），單 checkpoint 64 MiB、人類回答／resolve 1 MiB。達上限拒絕新工作／checkpoint，不刪舊資料；管理者須規劃保留與匯出。task state=completed 表示迴圈已回覆，verification=verified_completed 另要求 T10 契約與證據；`/tasks` 可見 verification，舊資料預設 unverified。多代理 DAG 與 worktree 已分別接入 T13／T12。

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

模型輸入超限時只刪完整舊訊息組，保留 system／developer、最新使用者輸入、task goal／criteria、批准及其工具配對；hook feedback 排在完整工具結果後。壓縮資料保持 user 權限，不提升為 system 指令。無法在上限內保留必要內容則拒絕，權威 checkpoint 不被裁剪。未知副作用與完整交付品質仍分別依 T08／T10 處理；可啟用 T12 worktree 避免檔案互覆，T13 提供 durable coordinator、合併衝突治理與 Reviewer gate。

完整驗證（Docker／Chromium 開啟）：444 passed、1 skipped（外部 provider）、7 subtests passed；新增預算與上下文測試包含真實 SDK 的 HTTP 429 無隱藏重試、root deadline 取消 Windows host 程序與 blocked stream、併發扣额、重啟與子代理額度／checkpoint。實際部署模型能力及費率未由這些 fixture 驗證。

## 結構化結果與明確驗收（T10）

模型工具、Skill 步驟、task step／checkpoint 與事件使用 `success`、`exit_code`、`error`、`output`、`artifacts`、`side_effects`、`evidence` 紀錄執行事實。CompletedProcess 不再以物件 repr 當工具結果；非零 exit code／明確失敗停止 Skill 後續步驟，presentation hook 不能抹掉已觀察的失敗。legacy 字串仍供相容顯示，成功文字只代表工具回覆，不能證明測試或整體驗收；side_effects 預設 unknown，不捏造外部送達或副作用保證。

在新任務前配置 workspace 內 `.agent/acceptance.json`，或以 `AGENT_ACCEPTANCE_FILE` 指向 workspace 內其他檔案。範例見 [agent_execution/examples/acceptance.json](agent_execution/examples/acceptance.json)：version=1、checks 陣列，可檢查 file_exists、file_contains、file_sha256、tests（target、min_tests）。最多 32 checks、契約 64 KiB、單檔讀取 8 MiB；越界／junction 拒絕。契約與原始目標在 task 建立時保存；執行中改文件或 /resume 不會降低原條件。

`run_test_suite` 使用實際 process status 與暫存 JUnit XML 的 passed／failed／errors／skipped。exit=0 但沒有通過案例、只印「All Tests Passed」、只有普通 Python script 成功，均不能成為測試證據。同一驗收的規則與契約共用一次測試結果。程式檔驗收契約須含 tests；文件編輯不因 write_file 或「撰寫」字詞誤判為必須測程式。

TaskEvaluator 必須通過起始 checks、沒有失敗工具步驟，以及配置的 Judge，才可 verified。Judge JSON 不合法、success 不是 bool、score 非有限 0–1 或呼叫失效均拒絕；不可用的 Judge 直接交人工，不反覆指示模型改程式。一般可修正失敗仍最多 2 次 Critic。驗收使用目前 task goal，而非對話第一個歷史目標；各 project runtime 的 evaluator workspace 亦獨立。

沒有明確 checks 的一般回覆可 completed，但不會因成功文字自動 verified_completed；程式任務缺測試仍需補齊。直接 Skill 接入驗收／事件，skill_executed 保存原結果後才驗收，crash 後不重做整段。state 與 verification 分開保存，unverified 回覆不自動沉澱成已驗證的 procedural memory。通過標記只涵蓋已配置的檢查，測試品質及外部 provider／平台行為仍須另行評測。

完整 Docker／Chromium 驗證：461 passed、1 skipped（外部 provider）、7 subtests passed；後續通訊工廠接入 TaskEvaluator，工廠／結果／project 隔離相關 43 passed。包含真實 pytest 成功／失败／空收集／只打印成功、Skill 非零停止副作用、真正退出後只續驗收、原始 goal、無效 Judge 與成功文字不能覆蓋失敗。

## 可配置 Hook 政策（T11）

`default_hook_manager()` 預設啟用六項 fail-closed 政策：模型請求驗證、模型預算、模型工具回覆驗證、工具參數驗證、工具准入及結果稽核。CLI／通訊服務啟動列出全部政策、優先序及啟停狀態。`AGENT_HOOK_POLICIES` 接受 JSON object；範例見 `agent_execution/examples/hook_policies.json`。各政策可設 `enabled`、`priority`、`fail_closed`，未知名称／欄位或不合法型別拒絕啟動。

模型、直接工具、Skill 子步驟與排程共用 Hook Pipeline，委派不重複執行同一工具；巢狀實際工具各自稽核。停用 Hook 不停用核心角色、workspace、批准與預算限制。參數驗證涵蓋 native signature、基本型別、enum、array items；完整外部 MCP schema 待 T18。PreModel Hook 的 model／tools 修改會傳入實際請求。

稽核含 source、identity、session、project、task_id、role。有 memory episode 時保存 `hook_execution`，無記憶時保留在 HookContext。自訂 Hook 是可信 Python；tool_done 重啟恢復不重播自訂 post-tool Hook，其外部副作用需自行設計恢復。相關完整 Docker／Chromium 驗證：473 passed、1 skipped（外部 provider）、7 subtests passed。

## 任務 Git worktree 與選檔交付（T12）

Git 專案可設定 `AGENT_TASK_WORKTREE=1`，在 durable task 取得 lease 後建立 owner／task 專用 detached worktree。來源必須是配置 workspace 的 repository root；從已提交 HEAD 開始，原 checkout 的 staged、未提交與 untracked 內容保留在原處。預設為 0，以支援一般非 Git workspace。綁定保存於 TaskStore，重啟、更改 storage 設定或關閉開關後，既有任務仍使用原 worktree；Skill、模型工具及驗收均使用該 workspace。

預設 worktree／registry／archives 位於 `%LOCALAPPDATA%/PythonAI/worktrees`，可由部署者設定 `AGENT_WORKTREE_ROOT`，必須在 repository 外。兩任務可修改同一檔案而不互覆；不自動合併成果到來源 checkout。使用 `/worktree <task_id> preview` 查看 base、HEAD、狀態及包含任務提交的 diff；untracked 內容需另外讀取檔案。

`git_commit(message, files=[...])` 只提交明確選中的個別檔案，用獨立 index 保留其他 staged changes；`add_all=true` 拒絕，`create_branch` 限 `codex/` 前綴。為確保選檔範圍，本次 commit 明確停用 repository hooks；測試／驗收須先完成。回傳 commit SHA 與選檔證據。`git_rollback(files=[...])` 需要 `/approve` 的一次性批准，綁定身份、session、project、task 與相同參數；先封存 working-copy bytes、選檔 index.patch、原 HEAD，再只 restore 選中的 tracked files。untracked 檔案不刪除，預設備份在 `%LOCALAPPDATA%/PythonAI/rollback`，可設定 `AGENT_ROLLBACK_ROOT` 至 repository 外。

完成／失敗／取消且 lease 已結束後，可使用 `/worktree <task_id> archive` 或 `restore`，兩者均限 admin，先產生批准 ID；`/approve <id>` 後以相同指令附加該 ID 執行。封存包含 committed history.bundle、working-copy files.zip、SHA-256 manifest、忽略檔及刪除狀態；成功驗證後才由 Git 移除 worktree。restore 恢復 HEAD 與未提交內容。封存失敗保留目錄；損壞的 restore 保留已建立目錄供人工檢查，不覆寫既有目錄。

最多 256 個 active worktree、每次快照 10000 檔／256 MiB、單檔 64 MiB；repository objects 與 history.bundle 上限各 256 MiB。symlink／junction、submodule、嵌套 Git repository 拒絕自動封存，需人工保存。封存保留在本機，不自動刪舊成果；維運者須管理磁碟保留。封存期間須停止其他編輯者，系統會重檢狀態、HEAD 與內容；host 模式的任意程式仍受 T02 已記載的隔離邊界限制。

最終 Docker／Chromium 完整驗證：492 passed、1 skipped（外部 provider）、13 subtests passed；新增 19 項 worktree 測試，涵蓋真實 Git、選檔 hook 防繞過、批准、archive／restore、真正程序退出後恢復與模型／Skill 驗收 workspace。

## 持久化多代理協作（T13）

`invoke_subagent(persona, task, dependencies=[...])` 建立 SQLite 節點，`check_subagent(agent_id)` 查詢結果，`manage_subagent` 支援 `wait`、`cancel`、`followup`、`merge`。依賴只能指向同一根任務已建立的節點，形成 DAG；取消涵蓋後代及該節點的 Reviewer。`wait` 最多 30 秒，並檢查父取消與剩餘預算。CLI／通訊服務啟動會恢復未完成佇列，停止時關閉工作程序。

每個節點使用獨立程序、對話、checkpoint 及 detached worktree；工具範圍由可信 persona 固定。Planner 只能讀取，Reviewer 可讀取及執行測試，Coder 可修改與委派。遠端身份執行時重新檢查角色，沒有藉由子代理提升權限。獨立程序方便恢復與取消，host 命令及測試的 OS 隔離邊界仍依 T02。

模型請求只提供 persona 允許的工具，執行端再次檢查。Windows 原生程序先以 CREATE_SUSPENDED 建立，加入 Job Object 後才恢復執行；加入失敗不執行命令，快速退出父程序的後代仍由 Job 清除。可交付的 Coder 任務需先配置 T10 acceptance checks；沒有明確契約的回覆不會通過驗證 gate。

協作需要 Git repository root，使用 private index 將目前父 workspace 的 working-copy bytes 建成不可變快照；保留父 HEAD、index 與 staged changes，忽略檔不複製。依賴 Coder 的成果 patch 只套入下游 worktree，衝突會停止。此快照與 T12 一般任務從已提交 HEAD 開始的預設不同。

建立快照前拒絕越界、symlink／junction 與非普通檔案，限制 10000 檔／256 MiB、單檔 64 MiB；路徑檢查先於 Git 添加內容。

`AGENT_SUBAGENT_CONCURRENCY` 預設 2、範圍 1–8，SQLite lease 跨 supervisor 控制工作數；lease 90 秒、每 15 秒續租。`AGENT_SUBAGENT_TIMEOUT_SECONDS` 預設 120、最大 600 秒，仍受根任務 deadline 限制。子代理共用 T09 根 request／token／費用額度；每次委派、follow-up 與自動 Reviewer 均扣累計 children，Reviewer 與所審 Coder 同深度。預設 children=4、depth=2，可容納兩組 Coder／Reviewer；無額度時不繞過審查。節點最多 10000、邏輯資料 64 MiB，單結果 1 MiB。

Coder 只有 frozen acceptance checks 通過才进入 review_pending；獨立 Reviewer 使用同一契約並回傳嚴格的 approve／findings JSON。Reviewer 失敗、逾時、不合法 JSON 或有 finding，均禁止交付。`merge` 限 immediate parent／admin，確認 Reviewer 驗證通過，逐檔比對父內容與原 base，再交付最多 128 檔／32 MiB。父內容變更會標記 conflict；保留合併前備份，HEAD／index 不變。多檔寫入不是全域原子交易，中途失敗保守回復仍屬本次寫入的內容；程序 crash 標 needs_resolution，保留現場供人工查核。

安全 checkpoint 可恢復且舊 lease 被隔離；tool_started／skill_started 不自動重播。`/agents <root_task> list` 列狀態，`cancel <agent_id>` 取消。Admin 可用 `answer <agent_id> <回答>` 回覆暫停，或 `resolve <agent_id> <tool_call_id> <觀察結果>` 保存已查核結果後續跑。合併／準備中斷無工具結果可補時，先人工檢查父內容與 worktree，待 lease 結束，用 `abandon <agent_id> <查核紀錄>` 結束舊節點，再建立 follow-up；不重播未知效果。取消不留執行程序，但會保留 worktree／證據供查核與 T12 封存。

舊 daemon JSON 狀態不自動遷移，需人工查核後建立新節點。模型 Reviewer 的品質仍需 T21 benchmark；本機測試使用離線 SDK fixture 驅動真實工作程序、Git worktree 與取消流程，不能據此宣稱外部模型或平台已驗證。完整 Docker／Chromium 回歸為 510 passed、1 skipped（外部 provider）、17 subtests passed；最後快照路徑補強的模組重驗另記錄於 `papers/todo.md`。

## Patch／PR／CI 審查閉環（T14）

`prepare_project_patch(changes)` 接受 `path`、`before_sha256`、`content`，保存不可變提案並回傳完整 diff；建立檔案的前置雜湊為 null，刪除的 content 為 null。`apply_project_patch(patch_id)` 先核對所有檔案，再逐檔套用；過期內容拒絕，失敗只補償仍符合自身寫入的檔案，保留同時發生的人類修改。已套用提案不重播；程序退出後留下 applying 或 needs_resolution 時須人工檢查。支援 UTF-8 普通檔案，拒絕 symlink／junction、越界及 `.git`；每提案 32 檔、單檔 8 MiB、原／新內容合計 16 MiB、diff 2 MiB。多檔套用不是全域原子交易；建議使用 T12 task worktree。範例見 `agent_execution/examples/project_patch.json`。

平台部署設定由 `AGENT_GIT_PROVIDER_CONFIG` JSON 提供，範例見 `agent_execution/examples/git_provider.json`。GitHub 使用 HTTPS API、`OWNER/REPOSITORY`、`GITHUB_TOKEN`；GitLab 使用 API v4 URL、namespace/project、`GITLAB_TOKEN`。`token_env` 可改用其他環境變數名稱；token 不存入提案。Git 原生 push 另外使用主機已配置的 credential helper／SSH，remote URL 不可嵌入 HTTP 帳密。`remote_url` 必須吻合 repository 的指定 remote；`base_ref` 必須是已取得的基底 commit/ref，服務不自動 fetch。必要 CI 名稱使用 GitHub `check:名稱`／`status:名稱` 或 GitLab `job:名稱`，`required_reviewers` 設定審查帳號，兩者均須非空。

操作順序：

1. 完成 patch、測試與選檔 commit，在乾淨 worktree 呼叫 `project_delivery(action="prepare", files=[...], branch="codex/...", title="...", body="...")`。會重新執行 T10 frozen acceptance；程式碼異動必須有實際通過的 tests check。files 必須涵蓋基底至 HEAD 的全部新 commit 所改過的檔案，包含後來刪除或還原的內容；最多 256 檔／256 個 commit。回傳 plan_id、SHA、最終 diff、history_diff 與驗收證據，便於審查完整發布範圍。
2. `action="publish", plan_id="..."` 產生批准請求。人類 `/approve <approval_id>` 後，以相同參數及 approval_id 執行；批准綁定身份、session、project、task、提案 digest 與 SHA，僅可用一次。只 push 確切 SHA 到指定 codex/ 分支，使用 lease 防止覆蓋外部變動，並停用 pre-push hook；接著建立 GitHub PR／GitLab MR。
3. `action="inspect"` 查詢當前 SHA 的 CI 與 review findings（檔案、行號、原始 comment／重現說明）。失敗、缺少必要 CI、未批准審查或過期 head 均不 ready。修復後 commit，使用 `action="prepare", previous_id="舊 plan_id", ...`，保存既有 PR、原始驗收與 feedback；重新批准 publish，再查 CI。`reproduction` 是審查者提供的內容，系統不宣稱已實際重現。
4. ready 後 `action="merge"` 另需新的人類批准；執行前再次查詢 head、CI 與 review，再以確切 SHA 請求 squash merge。只有平台回傳明確合併 commit 才產生 `verified_delivery=true` 摘要；published、ready 或單純工具成功均不等同完整交付。摘要含 PR URL、head／merge SHA、本機測試、CI 與審查證據。

GitHub 審查要求當前 SHA 的 APPROVED；REST review comments 沒有 thread resolved 欄位，因此採「同一作者在 comment 更新後對當前 SHA 明確批准」作為 finding 已確認的條件。GitLab 要求 resolved DiffNote、指定 reviewer、approvals_left=0、穩定的 approval 狀態及專案 `reset_approvals_on_push=true`；不支援該 approvals API 的方案／權限會拒絕合併。尚不支援跨 fork PR／MR。平台 branch protection 仍須由管理者設定，以執行伺服器端的原子合併門檻；本機查詢與 merge API 之間並非同一交易。

Patch 套用與 plan 操作必須留在原 task／workspace；等待人類審查或批准時，Agent 應使用 T08 `ask_user` 暫停該 task，批准後回答待處理問題以續跑。一般新請求會建立另一個 task，不能直接套用舊提案；若原 task 已結束，須在新 task 的工作目錄重新準備。repair 的 previous_id 可讀同 owner 的舊 plan，但仍要求原發布 head 是新 commit 的祖先。

發布／合併發生逾時、取消或不明結果時保存 needs_reconciliation，不自動重試寫入。`action="reconcile"` 只讀 API 與 remote，避免重複 PR／push；不明合併即使查到已合併，也只標 merged_unverified。人類 `/delivery <artifact_id> status` 可跨 task 查自己的提案；`/delivery <artifact_id> abandon <查核說明>` 限 admin，需距最後操作 180 秒並先檢查尚在執行的 worker，不回放或清除本機／遠端成果。提案保存於 workspace 外的 TaskStore，限制 10000 筆／64 MiB，達上限拒絕新增，保留與清理需人工管理。

API 每次操作最多 32 請求／30 秒，單請求最多 10 秒、回應 2 MiB，最多 10 頁；拒絕 redirect、HTML 登入頁及不完整 checks，不將錯誤 body/token 寫入診斷。HTTP loopback 僅供明確啟用的本機測試。協作 Coder 可用上述工具，Planner／Reviewer 不取得發布／合併工具。

實作依據：[GitHub Pull Requests](https://docs.github.com/en/rest/pulls/pulls)、[Checks](https://docs.github.com/en/rest/checks/runs)、[Reviews](https://docs.github.com/en/rest/pulls/reviews)、[GitLab Merge Requests](https://docs.gitlab.com/api/merge_requests/)、[Jobs](https://docs.gitlab.com/api/jobs/)、[Approvals](https://docs.gitlab.com/api/merge_request_approvals/)。測試使用真實 Git bare remote、localhost HTTP fixture 與實際 pytest；CI／review／合併 API 回應是 fixture，尚未對外部帳號建立或合併 PR／MR。

最終完整回歸（2026-10-10，Docker／Chromium opt-in 開啟）：538 passed、1 skipped（外部 provider）、25 subtests passed，耗時 548.94 秒。新增 27 項 T14 測試，亦驗證 Windows Git metadata 路徑別名、空檔 diff、未知合併的 SHA 核對與慢速 HTTP 期限；package／tools 語法及 Git diff 檢查通過。

## 增量程式碼索引與跨語言理解（T15）

`refresh_code_index()` 或既有 `python build_codebase_index.py` 以檔案內容 SHA-256 同步配置 workspace。每次搜尋亦同步本機內容；修改／刪除檔案、刪除函式、改 ignore 規則、切換 workspace／grammar／embedding 版本均會移除過期資料，未變內容重用向量。建立期間內容改變則拒絕本次結果。舊的無版本 code_index 表在首次同步時丟棄並重建；codebase_index.db 是可重建資料，不再追蹤於 Git，原有本機檔案保留。

一般 workspace 保留既有 codebase_index.db 路徑；T12／T13 任務 worktree 自動將 cache 放在 workspace 外的 TaskStore 同層，以 workspace 雜湊分檔，避免索引讓 Git 交付目錄變 dirty。其他 repository 可設定 `AGENT_CODE_INDEX_DIR` 為絕對、workspace 外的目錄；未設定則須自行加入 codebase_index.db 的 ignore 規則。切換外部 cache 不刪原本機 DB，亦不讀取舊向量。

`search_codebase(query, limit=5, mode="semantic")` 回傳 JSON，mode 可選 `text`（精確、大小寫敏感的 literal）或 `symbol`（精確名稱／qualified name）。`code_symbols(file_path="", backend="syntax", limit=100)` 列出 Python、Go、JS／JSX、TS／TSX 的語法宣告與位置。預設離線 hashing-64 向量，256 筆以下 exhaustive cosine，較大索引使用持久化 LSH buckets、最多 512 候選；回傳 backend／approximate／candidate_count／版本／同步統計，不宣稱近似結果為完整搜尋。

`go_to_definition`／`find_references` 的輸入為 1-based line、0-based Unicode column。Python 保留 Jedi，改由有界 worker 執行；其他語言預設回傳 `syntax_candidates`、`resolved=false` 的同名候選，JS／TS 共用語言家族，不能據此判斷型別或遮蔽。設定 `AGENT_CODE_LSP_SERVERS` JSON（例如 `{"go":{"command":["gopls"],"timeout":10}}`）可改用已安裝的 LSP server；工具不接受模型提供的 command。支援 initialize／didOpen／definition／references／documentSymbol／shutdown、UTF-16 位置轉換，過濾 workspace 外與已排除檔案。worker 預設 10 秒、最多 30 秒、2 MiB 輸出，沿用程序樹取消與清理；Jedi 固定 10 秒。

ignore 採巢狀 `.gitignore`／`.agentignore`；另強制排除 Git metadata、venv、node_modules、build／dist／temp、symlink／junction、`.env*`、金鑰／credentials 名稱與已知秘密內容。秘密辨識有漏判／誤判的可能，應補專案規則。只索引 UTF-8 普通文字，單檔 1 MiB、10000 檔／64 MiB、20000 symbols／200000 identifiers，DB 上限 256 MiB。語法錯誤移除舊符號，允許文字搜尋；超過總額度拒絕，不回傳旧索引。完整配置與邊界见 `agent_execution/code_index/README.md`。

遠端 embedding 為部署者 opt-in：`AGENT_CODE_EMBEDDING_CONFIG={"kind":"openai","model":"MODEL","dimensions":768,"revision":"deployment-v1","max_requests":16}`，沿用既有 server client／認證。dimension 須符合服務輸出（8..2048）；endpoint／model／revision／dimension 納入版本，同名模型換版本時須更新 revision。明確 refresh 最多 32 次 API，首個錯誤即停止；搜尋不做遠端 backfill，有合法向量時只查一次 query embedding。無重試、不把失敗降級成另一模型的向量，並受 T09 request／token／cost 預算約束。預設 hashing 只提供離線可重現檢索，不保證訓練模型的同義詞理解品質。

離線基準可執行 `python -m agent_execution.code_index.benchmark --symbols 1000 --queries 10`；結果保存於 `agent_execution/code_index/benchmark_baseline.json`。本機 1017 symbols，語法 symbol recall 與 ANN recall@5 均為 1.0；build 1301.994 ms、無改動 refresh 35.907 ms、query median 59.504 ms／max 81.722 ms。此基準使用完整合成 chunk 查詢、比對 exhaustive cosine；不代表自然語言搜尋品質或正式專案延遲。真實 grammar／Jedi 與本機 LSP protocol fixture 均有測試，正式 gopls／TS server 與遠端模型品質尚待部署環境實測。

技術依據：[Tree-sitter](https://tree-sitter.github.io/py-tree-sitter/)、[LSP 3.17](https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/)、[gitignore](https://git-scm.com/docs/gitignore)。

T15 最終驗證（2026-10-10）：專項測試 27 passed、5 subtests passed；開啟 Docker／Chromium 的完整回歸 565 passed、1 skipped（外部 provider）、30 subtests passed，耗時 620.86 秒，另有 1 項既有 AnyIO 棄用警告。package／tools 語法與 Git diff 檢查通過。

## IDE 提案與任務審查（T16）

VS Code Inline Chat 改為完整接收後預覽 readonly diff，透過「Apply Reviewed Proposal」單次套用；支援原生 Undo 與具版本檢查的「Undo Proposal」。生成、斷線、取消、UTF-8 拆包及並行串流均不直接改動原始文件；同一文件同時生成會拒絕，文件有修改則拒絕舊提案與 undo。提案限 32 筆、僅保存在擴充套件記憶體，套用後由使用者儲存。

`python ide_server.py` 仍是 IDE 入口（預設 127.0.0.1:11435），實作收攏於 `agent_execution/ide/`。現在必須使用既有 `AGENT_API_TOKENS` 與 `ROLES_CONFIG` 的 admin API 身份；不再接受匿名請求或 wildcard CORS。擴充套件「Set API Token」保存於 SecretStorage，設定 `pythonai.ideUrl`／`dashboardUrl`／`project`／`session`，Dashboard 填入相同專案與會話。遠端 URL 需 HTTPS，HTTP 僅允許 loopback。

IDE 使用有界 NDJSON start／delta／done 協定，只有完整 done 才形成提案；provider 錯誤不混入替換內容。請求上限 128 KiB、選取 64 KiB、prompt 8 KiB、輸出 1 MiB，客戶端期限 100 秒；接入 T09 token／request／cost／concurrency 預算與無自動重試的 SDK。server 不讀磁碟內容作上下文、不修改檔案，僅接收既有 workspace 文件的編輯器選取。生成保存 T08 任務，完成仍是 unverified，不代表已套用、測試或交付。

Dashboard 新增會話欄與 `/api/review/context`、`/api/review/tasks`、`/api/review/tasks/<id>`，按 API 身份／project／session 顯示計畫、實際工具步驟與依賴、驗收條件／結果、人工問題／回覆、artifact diff／history／證據，以及待批准操作。事件最多 100、artifacts 最多 20、各文字區最多 65536 字元，截斷明確標示；文字不執行 HTML。資料切換清除舊畫面並拒絕過期回覆。Admin 可取消自己的 task、批准／拒絕同會話操作，viewer 僅能閱讀；批准不自動重播工具或 publish／merge，須由原任務入口續跑。API、WebSocket、Telegram 等通道身份仍分離，未提供跨通道身份推定、任意 artifact 檔案下載或背景輪詢。

使用／測試細節見 `vscode-extension/README.md`；技術依據：[VS Code API](https://code.visualstudio.com/api/references/vscode-api)、[diff command](https://code.visualstudio.com/api/references/commands)、[Extension Host testing](https://code.visualstudio.com/api/working-with-extensions/testing-extension)。

T16 最終驗證（2026-10-10）：新增 11 項 Python 測試；最終 UI／API 與舊 Dashboard 整合重驗 14 passed（24.70 秒），Node 9 passed，實際 VS Code 1.141.0 Extension Host 14 案例通過。開啟 Docker／Chromium 的完整回歸 576 passed、1 skipped（外部 provider）、30 subtests passed（884.64 秒），另有 1 項既有 AnyIO 棄用警告；Python／Node 語法及 Git diff 檢查通過。證據摘要保存於 vscode-extension/tests/verification_baseline.json，原始 log／截圖保留於忽略的 temp/。

## Agent／Skill 排程與追蹤（T17）

Watch 未完成而達執行上限、或耗盡失敗重試時，會轉成 needs_input 並通知；相同觀察不會遮蔽停止事件。失敗去重包含有界錯誤證據，錯誤改變會顯示新通知。

排程實作收攏於 `agent_execution/scheduling/`，支援 command、workspace Python script、Agent、Skill、唯讀 watch 及摘要維護。`schedule_cli.py add` 可選五欄 `--cron` 或 `--schedule "每天 09:30"`／`"每 5 分鐘"`，預設 `--timezone Asia/Taipei`；不明確的自然語句會拒絕。`--timeout` 限 1–300 秒，`--max-runs` 限 1–1000。`list`、`disable JOB_ID`、`notifications` 僅操作自身 source／identity／project／session 的工作；模型使用 `manage_schedule` 建立排程須先取得對應參數的一次性人工批准。

Worker 使用 SQLite 原子領取、10 秒 lease 與 2 秒續租；舊執行者不能提交結果或清除新鎖。每次工作由有期限、可取消的原生子程序執行，恢復原會話與 workspace，重新查核遠端角色。錯過多個時間點合併為一次；未知寫入效果暫停待人工查核，唯讀 watch 最多重試兩次。舊遠端排程缺少可信會話資料時停用，不轉給本機身份。

Watch 保存最近四次觀察，回傳 waiting／completed／needs_input；第一次等待與相同狀態保持安靜，變更、失敗或完成寫入 owner 範圍的通知佇列。完成、人工處理、取消或執行次數上限會停止工作。通訊服務透過已配置 Telegram／Discord／LINE adapter 發送到原會話，重新檢查身份權限與 allowlist；原子認領、單一有界訊息、不確定結果不重送。其餘入口由工具／CLI 讀取，不能猜測跨平台收件人；外部模型與實際平台送達不由離線測試證明。排程 DB 限 64 MiB、1000 工作、10000 runs／notifications，達上限須依管理政策整理。

## MCP 與可信擴充套件（T18）

MCP 使用明確路由表，server／tool 名稱含底線及名稱碰撞均不靠字串切割判斷；參數與 structuredContent 以有界、離線 JSON Schema 驗證，工具錯誤不再混成成功文字。stdio 支援 EOF、取消及有界請求，remote 支援 HTTPS Streamable HTTP 的 JSON／SSE、session 與 access token；也提供人工 OAuth discovery／PKCE／一次性 callback／refresh rotation。未知寫入結果與 401 工具呼叫不自動重播。協定版本固定為 2025-11-25。[MCP transport](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)。

OAuth 使用預先註冊的 public client、明確 issuer 與原始 redirect URI；CLI 支援手動 begin／finish，以及 `login server.json --timeout 120` 的 loopback callback listener。Listener 保留原始 HTTP loopback IP／port／path，檢查 Host／一次性 state、限制請求與等待時間；退出清理新 state，保留既有 grant。Token 依 owner 保存在私有 SQLite，不跨身份繼承授權。配置與操作見 `agent_execution/extensions/README.md`；正式 provider 登入／同意流程仍待實測。[MCP authorization](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization)。

`create_dynamic_tool` 改為只保存版本提案，不寫入 tools/ 或立即執行。Python 工具與 Skill 版本保存在 owner 隔離的提案庫，通過 Docker unittest 與一次性人工批准後啟用；可撤回或批准切回舊版本。自動載入僅限提交的 builtins 清單。已批准 Python 是受信任 host 程式；來源、雜湊與測試不代替程式審查。CLI、package 格式、容量及限制見 `agent_execution/extensions/README.md`。

## 瀏覽器、文件與語音（T19）

`browser_session` 使用隔離的 Chromium profile／cookie state，可跨工作程序續跑登入流程，動作支援明確文字／URL 斷言及下載；失敗停止後續動作，取消會清除程序樹。下載和截圖保存於私有 owner 儲存。Profile 保存 cookies／storage，沒有保留仍執行的頁面 JS／未提交表單；host 瀏覽器不是 OS sandbox。

PDF／DOCX／PPTX／XLSX 附件現在使用有界解析程序，標示頁碼、段落、投影片或工作表列來源；掃描 PDF 可使用本機 Tesseract OCR。容量或格式不支援會明確回報，不以 Base64 假裝已理解內容。

WebSocket 在配置 STT／TTS model 與 audio endpoint 後提供真正 SDK 語音請求，支援有序音訊 frame、轉錄及可播放 WAV 回傳；限制容量／期限，取消與重連丟棄舊音訊結果。設定與協定見 `agent_execution/media/README.md`。本機已驗證 Chromium 登入續跑、原生文件／Tesseract，以及 SDK 對本機 HTTP fixture 的音訊 contract；正式語音品質／裝置播放仍待驗收。

## 可評測、可撤回的改善（T20）

Skill Doctor 只保存 owner 隔離的 persona／Skill 建議，不再透過 `--apply` 注入永久規則。Persona 須經固定案例 A/B 評測：驗收成功率增加、沒有案例回歸或非預期副作用，再以一次性人工批准啟用；可撤回或批准回復舊版本。Skill 建議另須通過 T18 測試／批准。提案、來源、評測／suite digest、目前 baseline 與版本都保存供審查。設定及 CLI 見 `agent_execution/improvement/README.md`。

檢索標示來源角色，排除未審查 legacy persona，以及尚未解決的明確單值矛盾；保留 T05 租戶隔離及既有過期／封存／supersede 過濾。原始記憶以引用資料放在真正最新使用者請求之前，不再取得 system 指令權限。Labeled precision／recall／forbidden-hit 評測可測淘汰品質；離線 gate fixture 不代表正式模型的品質已提升。

## Trace、評測與運維（T21）

本次最終驗證：乾淨測試環境完整回歸 634 passed、7 skipped、35 subtests passed；另開啟 Docker／Chromium 的相關整合 40 passed、無 skip，Node 9 及實際 VS Code Extension Host 14 案例通過。語法檢查 222 個 tracked Python 檔、CI YAML、pip check 與 Git diff 檢查通過；詳細證據及外部限制見 `agent_execution/operations/verification_baseline.json` 與 `papers/todo.md`。

`python -m agent_execution.operations doctor|readiness|traces|syntax|benchmark` 提供依賴診斷、storage readiness、owner 範圍 trace 與十五項固定任務／失敗注入評測。Task／tool／model／retry／audio 保留 trace、父 span、task、episode 與子代理 node 關聯；token／費用採 T09 保守預算記帳，未知費率明確標記，不能當成 provider 帳單。Admin API `/api/ops/readiness`、`/api/ops/traces` 受既有身份／專案／會話限制。

SQLite `backup` 保存一致的單庫 snapshot、雜湊及 schema 證據，`restore` 只還原至新目錄；跨資料庫備份需先停止 workers。`migrate --offline` 先備份再使用正式 store migration。稽核／trace 與觀測 CLI 進行去密；原始對話與權威 checkpoint 仍是私有資料。操作與限制见 `agent_execution/operations/README.md`。

正式 pytest 已設定 testpaths，不再收集 temp 舊實驗；維護腳本含既有 `patch_server.py` 的語法檢查納入 CI。GitHub workflow 分 Windows unit/native、Docker／Chromium、VS Code Host 與手動啟用外部 provider；本機結果與未執行的遠端 CI 分開記錄於 `papers/todo.md`。

### 2026-10-11 本機增修驗證

排程補齊 watch 上限／重試耗盡與錯誤變更通知；OAuth CLI 提供有界、可取消的 loopback callback 登入。固定驗收擴充為 15 項，涵蓋 OAuth 原生 CLI／PKCE、slow-client、語音 SDK 契約及人工審核回滾；全部通過，provider 品質與費用未測量。

乾淨 requirements-ci 完整回歸：642 passed、7 skipped、41 subtests passed（652.13 秒）。opt-in 整合首輪 55 passed、1 failed（Docker availability probe）；引擎確認可連線後，該案例單獨重驗通過。224 個 tracked Python 檔案語法及 pip check 通過。詳細證據見 `agent_execution/operations/verification_2026-10-11.json`；正式平台送達、OAuth 帳號 consent、語音品質、production A/B 與 GitHub CI 仍待驗收。

排程停止／失敗原因會置於觀察內容之前，避免長輸出讓需人工處理的原因被通知容量限制截掉；原始 run output 仍分開保留。

OAuth discovery 的授權網址若已有 state／client_id／redirect_uri／PKCE／resource／scope 等 client 管理參數，會在建立待登入 state 前拒絕；issuer 的其他 query 參數維持原樣，既有授權不受影響。

第二輪補強後，乾淨 requirements-ci 完整回歸為 **645 passed、7 skipped、52 subtests passed（539.05 秒）**，包含 15 項固定驗收；224 檔 Python 語法、pip check 及 diff check 通過。詳細紀錄見 `agent_execution/operations/verification_2026-10-11_followup.json`。本轮未執行正式平台／外部 provider／遠端 CI 驗收。

### 待辦完成標註（2026-10-11）

`papers/todo.md` 已分別標註本機功能完成與正式驗收待辦。T04、T06、T17–T21 的本機實作已完成並有測試紀錄，各項增加 [x] 子項與 commit；主項目仍維持 [ ]，正式平台／provider／品質／遠端 CI 的 7 個任務驗收条件依序集中列出。本輪只核對與標註文件，沒有新增程式或重跑全套測試。T18 的 dynamic registration／confidential client 仍未支援，未標記為已實作。

### Telegram polling 409 首輪修正驗證（2026-10-11）

新增 receiving-mode preflight、per-bot 原生程序鎖及安全的 409 原因判讀；衝突停止並清理 worker，暫時 polling 錯誤仍重試。乾淨 requirements-ci 完整回歸 **651 passed、7 skipped、58 subtests passed（557.53 秒）**，225 檔語法及 pip check 通過。實測包括本機 HTTP 409 與程序 crash 後鎖釋放；正式 Bot 僅唯讀 getWebhookInfo，未消費 update 或發送訊息。證據保存於 `agent_execution/operations/verification_telegram_polling_2026-10-11.json`。

### Telegram polling 短暫交接恢復（2026-10-11）

遇到另一個 getUpdates 請求尚未結束時，不再第一次 409 就停止；保留本機鎖，預設兩次／每次 35 秒等待，成功後顯示 resumed。衝突預算不因成功輪詢重置；webhook／unknown／用完預算仍停止。正式 Bot 原生 loop 兩次 10 秒輪詢成功、0 updates，未帶 offset、未處理或發送訊息。完整回歸 **655 passed、7 skipped、68 subtests passed（674.18 秒）**；證據見 `agent_execution/operations/verification_telegram_handoff_2026-10-11.json`。另一個長期活動的 consumer 仍需由部署者停止。
