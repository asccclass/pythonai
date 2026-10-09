# PythonAI 功能差距與開發待辦

盤點日期：2026-10-09。現有功能、實作來源與限制見 [README.md](../README.md)。本檔於本次新增；原有 `docs/todos.md` 與其他規劃文件保留。以下皆為**尚待完成的改善**，不將已有 Hooks、記憶、Skills、MCP、排程與驗收重新列成未實作。

比較基準：Codex 的編碼、worktree、多代理、批准與排程工作流程；「Agentic Agent」為規劃、工具執行、驗收、持久狀態及自主續跑的通用架構，非特定產品。此分析不主張模型品質等同或優於 Codex，也不以文件宣稱代替外部端到端驗證。

## P0：執行政策、身份與資料隔離

- [x] **T01 — 所有執行路徑共用工具政策與 workspace 邊界**
  - 現況：`base.py` 核心路徑檢查與 RBAC 已存在；`tools/` 部分程式直接使用 cwd／Path，`SkillExecutor` 直接呼叫工具；排程命令直接 `shell=True`。
  - 工作：建立共用執行入口，涵蓋一般工具、Skill 步驟、MCP、動態工具、子代理及排程；解析路徑、符號連結、cwd、來源身份與工具權限。
  - 驗收：viewer／operator 不可透過 Skill、動態工具或排程繞過權限；跨 workspace 與路徑穿越均拒絕且留下可追蹤事件；合法 trusted Skill 資源仍可執行。
  - 完成（2026-10-09，commit `328bcd8`）：新增 `agent_execution/` 共用政策與來源／身份 context；接入模型工具、Skill、動態工具、MCP、排程及子代理 context。工具路徑解析、Git repository 邊界、命令 option 路徑、索引位置亦已修正。
  - 驗證：完整 pytest 349 passed；後續 MCP／索引補強及新增測試後相關 pytest 77 passed（含 18 項政策回歸）；變更檔案語法與 diff 檢查通過。Windows junction 越界拒絕已實測；未進行真實 MCP provider round trip。
  - 邊界：此為應用層的角色與參數政策，不能限制已授權任意 Python／shell／第三方 MCP 內部副作用，OS 隔離由 T02 處理。舊排程遷移為 local/admin，新工作保存建立者身份／角色；shell operator 字串須改成 argv 或明確腳本。

- [x] **T02 — 真正的沙箱與程序生命週期管理**
  - 現況：`sandbox.py` Docker 可寫掛載 workspace，fallback 是主機 subprocess；`run_command` 未設通用 timeout。
  - 工作：明確區分 container 與 host 模式；需要隔離時無 Docker 應拒絕執行；加入唯讀／可寫範圍、secret 排除、網路政策、CPU／記憶體／磁碟限制、timeout、子程序終止及 artifacts 匯出。
  - 驗收：容器不能讀取主機 secret／未授權路徑；超時與取消可終止整棵程序樹；隔離失效不可靜默退回主機；Windows 與 Docker 實測分別記錄。
  - 完成（2026-10-09，commit `a95bb7e`）：Docker-only sandbox、唯讀 secret-filtered snapshot、禁網路、256 MiB／1 CPU／64 PID、16 MiB tmpfs 暫存及 32 MiB artifacts；安全 tar 匯出及拒絕覆寫。明確 host/container 模式、60 秒預設命令期限、2 MiB／stream 輸出上限、取消、Windows Job Object／程序樹清理。
  - 驗證：完整 pytest（開啟 Docker integration）366 passed；Windows 原生成功／取消／超時／輸出超限／子程序清理，以及 Docker 網路、secret、唯讀掛載、資源上限、artifact 匯出與超時後容器移除均實測通過。
  - 使用限制：container 適用 Linux 指令及唯讀 workspace，產出寫至 /artifacts；不轉交 env_file 中的 provider credentials。內建應用 helper 仍是受信任 Python host 程式，子程序統一使用有界 runner；第三方 MCP 與動態工具的可信發布流程由 T18 處理。

- [x] **T03 — 遠端身份驗證與不可偽造的批准流程**
  - 現況：`permissions.py` 未設定 ROLES_CONFIG 時預設 admin；通訊命令自動批准；`manage_memory(confirm=True)` 依賴模型遵守提示；REST 管理 API 未整合身份／角色。
  - 工作：遠端預設最小權限、API 登入／授權、綁定身份與操作內容的 approval ID、到期／一次性使用及稽核；保留遠端非互動執行，透過通訊／UI 批准，不使用 stdin。
  - 驗收：未登入不能修改記憶；模型自行傳 confirm=True 不足以批准；批准只適用原始身份／參數，跨會話、重放及過期操作皆拒絕。
  - 完成（2026-10-09，commit `b87d1ce`）：遠端預設 viewer、API Bearer token 驗證／RBAC（ASGI 與 HTTP）、SQLite 人類批准紀錄、5 分鐘 TTL、身份／會話／參數綁定、原子一次性消耗與稽核。CLI／通訊支援 /approvals、/approve、/deny；Dashboard 登入與操作批准流程。模型 confirm=True 無法授權。
  - 驗證：完整 pytest 開啟 Docker／Chromium integration：377 passed；補強已批准後過期及 malformed UTF-8 後相關測試 11 passed、1 skipped（browser 已另以實際 Chromium 3 passed）。身份／會話／參數變更、重放、併發、拒絕及 API 無登入寫入均有回歸測試；語法與 diff 檢查通過。
  - 邊界：批准 SQLite 預設存 workspace 外；任意 host 程式與第三方 MCP 仍須視為受信任程式，應用層批准不能對抗使用者 OS 權限。API 租戶隔離待 T05；通訊批准測試使用 adapter stub，尚未真實平台 round trip。

- [ ] **T04 — Webhook 驗證缺失時拒絕與平台回覆完整性**
  - 現況：未配置 Telegram／LINE secret 或 Discord public key 可接受請求；Discord 缺 PyNaCl 會略過驗章；Discord command 路徑尚需確認符合 interaction 回覆時限。
  - 工作：分開開發與對外模式，對外缺 secret／驗證依賴拒絕啟動或請求；補 timestamp／重放限制、Discord deferred response／follow-up 與 LINE 回覆策略。
  - 驗收：缺少或錯誤簽章、過期／重放請求不能入列；Discord／LINE 真實帳號 round trip 成功；平台依賴及配置寫入安裝文件。
  - 實作完成（2026-10-09，commit `bcecff0`，外部驗收待辦）：production 缺配置拒絕，development 明確豁免；真實 Ed25519 與 event timestamp 驗證、持久化去重、單次解析／附件下載、Discord deferred ACK 後放行 job／follow-up／過期 channel fallback、LINE push 與分段、API delivery error 檢查。新增 PyNaCl 依賴及部署設定。
  - 相關測試：`python -m pytest tests/test_webhook_security.py tests/test_communication.py tests/test_communication_server.py tests/test_telegram_polling_worker.py -q`：53 passed；包含真實密碼學簽章驗證及缺套件拒絕，平台 HTTP POST 使用 stub。語法／diff 檢查通過。
  - 保持未勾選：Discord／LINE 正式帳號 round trip 尚未驗證，須有配置完成的測試帳號與對外 HTTPS webhook。實作可供 T05 後續隔離工作使用，不能將本機協定測試等同正式平台交付。
  - 完整驗證：開啟 Docker／Chromium integration 的 pytest：388 passed；其後補原始 Discord 回覆 PATCH 與原生 HTTP ACK 測試：相關 11 passed。無外部平台訊息傳送。

- [x] **T05 — 會話、使用者、專案與取消狀態隔離**
  - 現況：`CommunicationWorker` 背景 threads 共用 runtime.messages／check_cancelled；記憶 scope 欄位尚非端到端隔離；/status、/cancel、/clear 需完整身份範圍。
  - 工作：以 platform + conversation + identity + project 分配 runtime／鎖／記憶查詢範圍；每個 job 保存獨立取消 token，管理指令依身份授權。
  - 驗收：兩使用者同時發送、取消或清除任務時互不影響；不可查看／取消他人 job；記憶檢索與管理 API 不會跨租戶洩漏。
  - 完成（2026-10-09，commit `be2dcd4`）：獨立 runtime／messages／search state／會話 lock、身份／會話／專案分庫、可信 workspace map／ContextVar 根目錄；保存 command project、原子 claim／cancel、禁止取消狀態被完成覆寫，status／cancel／clear／resume owner filter。API 資料、任務計數及批准隔離到身份／project。WebSocket device／outbox／proactive key 加入 owner／project，移除預設 token，修正舊 socket 清除新連線。
  - 驗證：完整 pytest 開啟 Docker／Chromium integration：395 passed；後續 workspace 與 WebSocket 補強相關測試 33 passed（含 8 項 sessions 測試），Chromium 登入／批准實測相關 27 passed。語法與 diff 檢查通過。
  - 邊界：legacy 本機記憶不自動複製到遠端，新租戶由空 DB 開始；不同平台身份不自動合併。對話 runtime 仍是程序記憶體，重啟續跑由 T08 處理；各 task 的專用 worktree 由 T12 處理。自訂 runner 須實作 scoped clear callback。

## P1：真實 Agent 體驗與長任務可靠性

- [ ] **T06 — WebSocket 接入真實 Agent 串流及任務控制**
  - 現況：`brain_reply_stream` 是示範 echo，主 Agent 使用非串流 Chat Completions；Telegram fallback 僅 append 到清單。
  - 工作：/ws/v1 使用獨立 AgentRuntime，輸出模型 delta、工具開始／結果、驗收、批准及錯誤事件；實作真正平台 fallback 與 interrupt 傳遞。
  - 驗收：一個需要工具的請求可串流執行並回實際結果；中斷可停止模型／工具工作；fallback 真正送達；協定測試、真實 provider 測試分開紀錄。依賴 T02、T05。
  - 實作完成（2026-10-09，外部驗收待辦）：真實 Chat Completions stream、分段工具呼叫組裝、隔離 AgentRuntime、模型／工具／驗收／批准事件、busy 保護、取消與清理、owner-targeted Telegram adapter fallback；SDK 關閉串流並限制 timeout，慢 consumer 有界 queue。
  - 驗證：server／hooks／streaming 49 passed、1 skipped；新增 SDK 本機 HTTP SSE、批准 project／device 與 busy 測試後相關 23 passed、1 skipped。包含實際檔案讀取與 host 子程序取消；語法／diff 檢查通過。
  - 保持未勾選：真實 provider opt-in 測試失敗，2026-10-09 配置 endpoint DNS 成功但 TCP 443 連線逾時；尚需恢復 provider 後驗收工具串流。Telegram fallback 以 adapter stub 驗證路由與送出，正式帳號實際送達未驗收。建立模型連線中取消仍受最長 60 秒 timeout 約束。

- [ ] **T07 — 附件與可靠訊息持久化**
  - 現況：`communication_store.py` 的附件 bytes 存於 _PENDING_ATTACHMENTS，WebSocket outbox／sessions 存於記憶體。
  - 工作：附件落地或物件儲存、雜湊／MIME／大小與存取檢查、TTL；可靠事件持久化、去重、容量限制、backpressure、過期補送政策。
  - 驗收：入列後重啟仍能處理圖檔及文件；斷線／重啟補送無重複副作用；未授權附件不可讀，過期資料可清理。依賴 T05。

- [ ] **T08 — 持久化任務計畫、checkpoint 與重啟恢復**
  - 現況：ReAct 規劃靠提示與 15 次迭代上限；通訊已有 ask_user 暫停／恢復，未涵蓋任意執行點，且 ask_user 未列入預設工具 schema。
  - 工作：註冊 ask_user schema 並驗證真實暫停／續跑入口；保存 task／plan／steps、依賴、輸入與驗收、執行狀態及 checkpoint；重啟恢復 pending／running；不可重放操作使用 idempotency key 或轉人工確認。
  - 驗收：工具前／後、驗收前／後中斷程序仍可安全續跑；外部寫入不重複；失敗步驟可有限重試或重規劃；保留 episode 關聯。依賴 T01、T05。

- [ ] **T09 — 任務總預算、模型路由與上下文治理**
  - 現況：`request_budget.py` 主要限制記憶請求；工作記憶為摘要，所有開發任務主要使用 OLLAMA_MODEL。
  - 工作：task／子代理總 token、成本、deadline、併發與 retry 預算；模型能力探測、文字／視覺／embedding 路由；依 token 壓縮並保留目標、批准、工具呼叫／結果配對。
  - 驗收：連續 429／timeout／子代理遞迴不會突破預算；不支援 vision 時明確降級；壓縮後仍保持驗收條件與合法訊息結構。依賴 T08。

- [ ] **T10 — 結構化工具結果與明確完成條件**
  - 現況：工具混用字串／dict／CompletedProcess；驗收會解析文字測試輸出，部分 Skill 回傳非零 returncode 未必成為步驟失敗。
  - 工作：統一 success、exit_code、error、artifacts、side_effects；任務起始明確驗收條件，避免只靠關鍵詞或 Judge；Skill 直跑路徑也接入驗收及稽核。
  - 驗收：工具回失敗、測試沒跑、只有成功文字、直接 Skill 失敗等情境不能標為 verified_completed；Critic 只針對可修正失敗重試。依賴 T01、T08。

- [ ] **T11 — 將 Hook 框架接成可配置政策**
  - 現況：四個 lifecycle 與稽核已有測試，default_hook_manager 尚無內建 hook；Guard／權限／驗收不是全部經由共用 hook。
  - 工作：加入政策註冊配置與啟動診斷，將參數驗證、身份／預算、結果正規化接入適當事件；補 Skill／排程路徑，避免雙重執行。
  - 驗收：啟停、排序與 fail-closed 設定有效；無政策時明確顯示；每條執行路徑有一致稽核，阻擋不產生副作用。依賴 T01、T03、T10。

## P2：縮小與 Codex 的開發交付差距

- [ ] **T12 — 任務專用 Git worktree 與安全交付**
  - 現況：有 git status／diff／commit／rollback，但任務與子代理共享 checkout。
  - 工作：建立／回收 task worktree、保留使用者 dirty changes、限定修改／staging 範圍、diff 預覽、選檔回復、commit 與 artifacts；對破壞性 rollback 明確批准。
  - 驗收：兩任務可獨立修改同檔且不互覆；失敗回復不刪使用者既有變更；只有任務檔案入 commit；清理前可恢復成果。依賴 T02、T08。

- [ ] **T13 — 可管理的多代理協作**
  - 現況：planner／coder／reviewer daemon thread 與 JSON 狀態，無 durable coordinator。
  - 工作：父子任務與 DAG、並行上限、獨立上下文／工具／預算／worktree、follow-up、等待／取消／超時、Reviewer 驗收、結果合併與衝突治理。
  - 驗收：子代理 crash／程序重啟／衝突時主代理可收斂；取消無殘留工作；Reviewer 未通過不交付；不自動遞迴超過限制。依賴 T05、T08、T09、T12。

- [ ] **T14 — Patch、PR／CI 與審查閉環**
  - 現況：edit_file 字串替換、Python AST 編輯與 Git 工具已有實作，未見完整 PR／CI 流程。
  - 工作：帶前置內容驗證的多檔 patch、衝突提示、diff 審查；可配置 GitHub／其他 Git 平台 PR、CI 狀態、review feedback 修復及交付摘要。
  - 驗收：過期 patch 不覆蓋新內容；review findings 有檔案／行號／重現；CI 未通過不宣稱完成；外部發布／合併遵守批准政策。依賴 T03、T10、T12。

- [ ] **T15 — 程式碼索引與跨語言理解**
  - 現況：Python Tree-sitter／Jedi、手動 embedding 建索引；語意搜尋讀取候選並算 cosine。
  - 工作：增量索引與檔案變更追蹤、embedding 模型版本、ignore／secret 過濾、精確文字搜尋、LSP／多語言符號、較大資料的 ANN。
  - 驗收：修改／刪除／更換模型不殘留舊索引；Python、Go、JS／TS 的符號與引用以 fixture 驗證；建立品質與延遲基準。依賴 T01、T09。

- [ ] **T16 — IDE 與 Dashboard 的任務審查介面**
  - 現況：VS Code 串流直接取代選取文字；Dashboard 主要管理記憶與觀察任務。
  - 工作：套用前 diff、atomic apply／undo、串流 UTF-8 與取消處理；統一顯示 task plan、工具事件、驗收、批准與 artifacts。
  - 驗收：斷線／取消／並行串流不破壞原始選取；UI 操作與身份政策一致；瀏覽器與 VS Code 實測並留證據。依賴 T03、T06、T10、T14。

## P3：自動化、整合、記憶與品質治理

- [ ] **T17 — Agent／Skill 排程與持續追蹤**
  - 現況：排程已有 command、maintenance.summary、SQLite 工作鎖，未實作 CLI help 提到的通用 script job。
  - 工作：新增 Agent／Skill job、自然語言排程、時區、會話 follow-up、停止條件、只在有意義變更通知；補 command timeout、鎖 lease、重啟／misfire 與 bounded retry。
  - 驗收：多入口同時啟動不重複執行；Asia/Taipei 排程時間正確；長工作／crash 不永久卡住；狀態未變保持安靜，完成或需人處理才通知。依賴 T01、T05、T08、T09。

- [ ] **T18 — MCP 與動態工具／Skills 的可信生命週期**
  - 現況：stdio MCP 與工具熱載入可用；動態 Python 模組直接載入；MCP 路由依工具名稱分割，需補 server 名含底線案例。
  - 工作：remote MCP transport／OAuth、timeout／重連／health、完整 schema 與結果驗證、明確工具到 server 映射；工具／Skill 安装、版本、來源、測試、核准啟用與 rollback。
  - 驗收：缺 server／網路斷線不阻塞其他工具；名稱碰撞／底線不誤路由；惡意 schema 或未通過測試的生成工具不能啟用。依賴 T01、T03、T10。

- [ ] **T19 — 持久瀏覽器、文件解析與語音**
  - 現況：Playwright action／截圖、文字及圖片附件可用；WebSocket audio 只解析 metadata，未提供 STT／TTS。
  - 工作：使用者隔離的持久 browser session、動作後斷言、下載 artifacts；PDF／Office 解析／OCR 與來源頁碼；真正 STT／TTS、音訊容量與取消。可分成三個獨立交付。
  - 驗收：多步登入／表單流程可續跑並驗證結果；PDF／Office 不以截斷 Base64 充作理解；語音可辨識及播放，重連／中斷不混流。依賴 T02、T05、T06、T07。

- [ ] **T20 — 記憶品質與可控自我改善**
  - 現況：三類記憶、遺忘、背景更新與 Skill Doctor 已存在。
  - 工作：租戶／專案隔離、retrieval precision／recall 評測、來源信任與矛盾測試；Doctor persona／Skill 建議經版本審查、A/B 驗證、啟用及撤回，避免錯誤準則無限累積。
  - 驗收：跨使用者記憶不能注入；過期／矛盾資料可測試淘汰；新 persona 必須有可量測改善且可回復。依賴 T05、T09、T10。

- [ ] **T21 — 端到端評測、成本觀測與運維**
  - 現況：已有 unittest 與 CLI events／health；缺整體成功率基準及分散式 tracing。
  - 工作：task／episode／tool／子代理 trace ID、token／費用／延遲／重試統計、redaction、readiness；CI 測試層級、migration／備份還原、依賴 doctor；建立固定任務集與失敗注入。
  - 維護腳本：本次靜態掃描發現既有 `patch_server.py:1` 含字面 `\n` 而無法解析；確認用途後修復或移除過期腳本，將維護腳本語法檢查納入 CI，避免只跑 tests 而漏掉。
  - 驗收：報告區分 mock unit、Docker／browser integration、真實 provider／平台測試；顯示完成率、非預期副作用、恢復成功率與成本；日誌無 secret；備份可還原。

## 開發與完成標準

1. 建議順序：T01–T05 → T06–T11 → T12–T16 → T17–T21；可依需求拆小交付，但不得繞過其隔離與授權依賴。
2. 沿用既有模組；新增獨立程式時依 AGENTS.md 建立專用目錄，集中程式碼、設定、需求及測試。
3. 每次程式變更增加／更新相關測試；執行相關驗證與 `git diff --check`，完成後建立範圍明確的 commit。
4. 勾選完成前記錄 commit、測試命令與結果、外部驗證限制，並同步 README；不以 mock 通過宣稱正式服務可用。

## 比較來源

OpenAI 官方文件，查核日期 2026-10-09；來源提供能力基準，本檔的優先序與待辦為本專案程式分析所得：

- [Git worktrees](https://learn.chatgpt.com/docs/environments/git-worktrees)：隔離任務及保留可審查改動。
- [Subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents)：專門角色、並行委派及 agent thread 控制。
- [Scheduled tasks](https://learn.chatgpt.com/docs/automations?surface=app)：背景任務、會話追蹤、Skills 與執行結果管理。
- [Agent approvals & security](https://learn.chatgpt.com/docs/agent-approvals-security)：sandbox、approval 與網路存取政策。

## 本次盤點驗證

- 僅修改 README.md 及新增本檔，未修改程式或新增測試。
- `venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"`：325 tests，OK。
- 檢查文件本機連結、功能對應模組及 `git diff --check`。
- 額外 AST 語法掃描根目錄、tools、tests、communication_adapters 的 107 個 Python 檔案：106 個通過；既有 `patch_server.py:1` 語法錯誤，記入 T21，未在本次文件變更中修復。
- 未進行真實 LLM provider、Telegram／Discord／LINE、Docker、瀏覽器／VS Code 或正式部署驗證；測試通過不代表這些整合已通過。
