# PythonAI 功能差距與開發待辦

盤點日期：2026-10-09。現有功能、實作來源與限制見 [README.md](../README.md)。本檔於本次新增；原有 `docs/todos.md` 與其他規劃文件保留。以下記錄功能差距及逐項實作／驗收結果；勾選代表完成該項本機驗收，外部驗收未通過的項目仍保留未勾選。不將已有 Hooks、記憶、Skills、MCP、排程與驗收重新列成未實作。

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
  - 實作完成（2026-10-09，commit `d8c6257`，外部驗收待辦）：真實 Chat Completions stream、分段工具呼叫組裝、隔離 AgentRuntime、模型／工具／驗收／批准事件、busy 保護、取消與清理、owner-targeted Telegram adapter fallback；SDK 關閉串流並限制 timeout，慢 consumer 有界 queue。
  - 驗證：server／hooks／streaming 49 passed、1 skipped；新增 SDK 本機 HTTP SSE、批准 project／device 與 busy 測試後相關 23 passed、1 skipped。包含實際檔案讀取與 host 子程序取消；語法／diff 檢查通過。
  - 保持未勾選：真實 provider opt-in 測試失敗，2026-10-09 配置 endpoint DNS 成功但 TCP 443 連線逾時；尚需恢復 provider 後驗收工具串流。Telegram fallback 以 adapter stub 驗證路由與送出，正式帳號實際送達未驗收。建立模型連線中取消仍受最長 60 秒 timeout 約束。

- [x] **T07 — 附件與可靠訊息持久化**
  - 現況：`communication_store.py` 的附件 bytes 存於 _PENDING_ATTACHMENTS，WebSocket outbox／sessions 存於記憶體。
  - 工作：附件落地或物件儲存、雜湊／MIME／大小與存取檢查、TTL；可靠事件持久化、去重、容量限制、backpressure、過期補送政策。
  - 驗收：入列後重啟仍能處理圖檔及文件；斷線／重啟補送無重複副作用；未授權附件不可讀，過期資料可清理。依賴 T05。
  - 完成（2026-10-09，commit `6f47254`）：附件 bytes／雜湊／MIME／大小／期限 SQLite、原子 inbound/job/attachment ingest、owner 範圍讀取與過期拒絕；可靠事件持久化 seq／ACK／TTL floor、容量拒絕／backpressure、chat ID 去重與 crash 後 uncertain；平台 delivery intent／原子 claim／lease，未知送達不盲目重送。
  - 完整驗證：`python -m pytest tests agent_execution/tests -q`（Docker／Chromium 開啟）414 passed、1 skipped（外部 provider opt-in）、3 subtests passed；語法及 diff 檢查通過。相關驗證：67 passed、1 skipped；新增 8 項持久化測試通過，包含新 Python 程序讀回圖檔及中文文件、竄改／越權／到期、容量不覆寫、重啟 ACK 與輸入不重複執行。
  - 邊界：平台回覆沒有 exactly-once 承諾；uncertain 需人工查詢，不會重跑 Agent。WS 對話 messages／執行 checkpoint 由 T08 處理。MIME magic 不是惡意文件掃描；服務 DB 仍受主機 OS 權限控制。過期 bytes 可清理，附件 metadata tombstone 達配置上限後須依管理保留政策整理。


- [x] **T08 — 持久化任務計畫、checkpoint 與重啟恢復**
  - 現況：ReAct 規劃靠提示與 15 次迭代上限；通訊已有 ask_user 暫停／恢復，未涵蓋任意執行點，且 ask_user 未列入預設工具 schema。
  - 工作：註冊 ask_user schema 並驗證真實暫停／續跑入口；保存 task／plan／steps、依賴、輸入與驗收、執行狀態及 checkpoint；重啟恢復 pending／running；不可重放操作使用 idempotency key 或轉人工確認。
  - 驗收：工具前／後、驗收前／後中斷程序仍可安全續跑；外部寫入不重複；失敗步驟可有限重試或重規劃；保留 episode 關聯。依賴 T01、T05。
  - 完成（2026-10-09，commit `468cb5e`）：TaskStore goal／criteria／step 輸入、結果、依賴、episode 與 checkpoint；原子 lease／heartbeat／fencing、3 次 crash 恢復、模型／工具前後／驗收前後、直接 Skill 結果；CLI／通訊／WS 接入，ask_user schema／持久問答與人類 tasks／steps／resume／cancel／resolve。
  - 相關驗證：54 passed、1 skipped（外部 provider）、4 subtests passed；使用真正退出的子程序，在 model_received、tool_done、evaluation_pending／done 中斷後仍僅一次寫入。tool_started 中斷會轉 uncertain，不猜測外部副作用；WS runtime 重建後問答成功，episode／tool pairing 保留。
  - 完整測試首次 421 passed、1 failed（Docker rm 清理的 5 秒 timeout）、1 skipped、7 subtests passed；修復為 20 秒有界 runner 並忽略任務取消，單獨實際 Docker／程序測試 19 passed，修復 commit `dc68d4c`。完整測試重跑（Docker／Chromium 開啟）427 passed、1 skipped（外部 provider）、7 subtests passed；其後 /clear 持久暫停取消補強相關測試 55 passed、1 skipped、4 subtests passed。語法／diff 檢查通過。
  - 邊界：plan 是現有 ReAct 實際產生的循序步驟與依賴，跨代理 DAG 由 T13 處理；task completed 不等同 verified_completed，T10 補明確條件。Skill 中途副作用須人工查核／取消，不自動重播。舊版無 task checkpoint 的 running job 保留人工處理，安全恢復須等待 180 秒 lease 過期。


- [x] **T09 — 任務總預算、模型路由與上下文治理**
  - 現況：`request_budget.py` 主要限制記憶請求；工作記憶為摘要，所有開發任務主要使用 OLLAMA_MODEL。
  - 工作：task／子代理總 token、成本、deadline、併發與 retry 預算；模型能力探測、文字／視覺／embedding 路由；依 token 壓縮並保留目標、批准、工具呼叫／結果配對。
  - 驗收：連續 429／timeout／子代理遞迴不會突破預算；不支援 vision 時明確降級；壓縮後仍保持驗收條件與合法訊息結構。依賴 T08。
  - 實作（commit `ed5abb8`）：根任務持久化 token／request／成本／deadline／子代理總數与深度；前景模型、embedding、Judge 與重試扣額，SDK 隱藏重試關閉；全域 SQLite model slots／串流 wall deadline、通訊背景入列上限與控制指令通道。子代理獨立 checkpoint 防止覆寫父任務；仍共享父預算。
  - 模型／上下文：文字／視覺／embedding 配置、明確能力拒絕與 opt-in bounded probe；失敗能力標 unknown。UTF-8 bytes 保守 token 估算，整組工具配對裁剪、保留 goal／criteria／approval／最新輸入；不裁剪權威 checkpoint、不將摘要提高權限。
  - 邊界：成本上限預設關閉，啟用須提供受信任費率；保留額度不退還，usage 超估向上記帳後停止後續工作，不是供應商帳單保證。圖片 Base64 保守計入，較大圖片需配置已驗證視窗，超限明確拒絕。真實 provider 能力探測仍受目前 TCP 443 連線逾時限制；不據本機 fixture 宣稱部署模型具備視覺品質。DAG／child crash coordinator 由 T13 處理。
  - 完整驗證（2026-10-09）：`python -m pytest tests agent_execution/tests -q`，開啟實際 Docker／Chromium：444 passed、1 skipped（外部 provider）、7 subtests passed。新增 16 項預算／上下文測試，包含真實 SDK HTTP 429 無隱藏重試、Agent 連續 429 停止、Windows host 程序 deadline、blocked stream 關閉、SQLite 併發扣額與成本／重啟／child checkpoint；語法與 diff 檢查通過。前次相關測試 96 passed、1 skipped、4 subtests passed。
  - 後續修復（2026-10-10，commit `aee5455`）：完整測試壓力下發現取消與 monotonic deadline 的微小競態，空 EOF 可能誤作正常完成；新增 explicit interrupted flag 與 EOF budget check。預算相關 17 passed，含新取消 EOF 回歸；完整重驗納入 T10。

- [x] **T10 — 結構化工具結果與明確完成條件**
  - 現況：工具混用字串／dict／CompletedProcess；驗收會解析文字測試輸出，部分 Skill 回傳非零 returncode 未必成為步驟失敗。
  - 工作：統一 success、exit_code、error、artifacts、side_effects；任務起始明確驗收條件，避免只靠關鍵詞或 Judge；Skill 直跑路徑也接入驗收及稽核。
  - 驗收：工具回失敗、測試沒跑、只有成功文字、直接 Skill 失敗等情境不能標為 verified_completed；Critic 只針對可修正失敗重試。依賴 T01、T08。
  - 實作（2026-10-10，commit `d5f2a0d`）：ToolResult 正規化、實際 exit code／JUnit 測試證據、Skill 失敗停止後續步驟；起始凍結的 versioned file／hash／tests 契約、goal／project evaluator 修正。Judge schema／網路錯誤拒絕，無證據預設 completed／unverified，TaskStore 另存 verification。Skill 執行與驗收分 checkpoint，skill_executed 復原只續驗收；真實退出程序驗證副作用一次。
  - 相關測試：103 passed、7 subtests passed；新增 crash 與 tool audit 補強 25 passed、4 subtests passed；補上 code 契約必須 tests、文件編輯分類、Judge 不重試及避免重跑同一測試後 31 passed、3 subtests passed。語法與 diff 檢查通過。
  - 完整驗證首次 451 passed、2 failed（tool JSON 顯示的舊測試期待值、T09 cancel EOF 競態）、1 skipped、7 subtests passed；兩者修正後，Docker／Chromium 完整重驗：461 passed、1 skipped（外部 provider）、7 subtests passed。其後補通訊 runtime 工廠的 TaskEvaluator，工廠／結果／project 隔離相關 43 passed。新增 15 項 results 測試與 1 項真實 Skill 程序退出測試；語法／diff 檢查通過。
  - 邊界：驗收依部署者配置的 checks，不能證明測試本身品質或未列出的外部行為；舊契約／資料不回溯標 verified。保留 legacy 工具显示 API，但字串不可當測試證據；side_effects 未知時不猜測。獨立 Reviewer、完整 artifacts 與品質 benchmark 分別由 T13／T14／T21 擴充。

- [x] **T11 — 將 Hook 框架接成可配置政策**（commit `c083809`）
  - 現況：四個 lifecycle 與稽核已有測試，default_hook_manager 尚無內建 hook；Guard／權限／驗收不是全部經由共用 hook。
  - 工作：加入政策註冊配置與啟動診斷，將參數驗證、身份／預算、結果正規化接入適當事件；補 Skill／排程路徑，避免雙重執行。
  - 驗收：啟停、排序與 fail-closed 設定有效；無政策時明確顯示；每條執行路徑有一致稽核，阻擋不產生副作用。依賴 T01、T03、T10。
  - 實作（2026-10-10）：六項內建政策及 AGENT_HOOK_POLICIES JSON 設定、啟動診斷；模型 request／response、native signature／基本 schema、角色／路徑／預算、結構化結果稽核。直接工具、Skill／排程與模型路由接共用事件，委派避免雙重執行；核心授權不因停用 Hook 而取消。
  - 相關驗證：90 passed；checkpoint／結果補強 45 passed、4 subtests passed；型別／enum／array 驗證補強 98 passed、4 subtests passed；修正 SDK 訊息 mock 後相關 35 passed、3 subtests passed。完整重驗另記錄於下。
  - 完整 Docker／Chromium 重驗：473 passed、1 skipped（外部 provider）、7 subtests passed。首次 470 passed／2 failed 的舊 SDK mock 與第二次 472 passed／1 failed 的舊 command 字串 fixture 均已修正成實際 SDK／工具格式；新增 12 項政策測試，語法／diff 檢查通過。
  - 邊界：完整外部 MCP schema 仍由 T18 補；有 memory episode 的 Hook 事件持久化於該 episode，無記憶時保留 HookContext 稽核。custom hook 是可信 Python，既有 tool_done 恢復不重播自訂 post-tool hook，不承諾其外部副作用 exactly-once；完整 tracing 由 T21 補。

## P2：縮小與 Codex 的開發交付差距

- [x] **T12 — 任務專用 Git worktree 與安全交付**
  - 現況：有 git status／diff／commit／rollback，但任務與子代理共享 checkout。
  - 工作：建立／回收 task worktree、保留使用者 dirty changes、限定修改／staging 範圍、diff 預覽、選檔回復、commit 與 artifacts；對破壞性 rollback 明確批准。
  - 驗收：兩任務可獨立修改同檔且不互覆；失敗回復不刪使用者既有變更；只有任務檔案入 commit；清理前可恢復成果。依賴 T02、T08。
  - 實作（2026-10-10，commit `bfdde21`）：可選 AGENT_TASK_WORKTREE=1、owner/task detached worktree、TaskStore 固定綁定與 scoped evaluator；選檔獨立 index commit、批准後選檔 rollback 與備份。人類 `/worktree` preview／archive／restore；清理前保存 committed bundle、dirty／ignored／deleted 檔及 digest manifest。來源 dirty changes 不複製進 worktree，保留原 checkout。
  - 相關測試：底層 8 passed、6 subtests passed；接入任務／驗收後 41 passed、10 subtests passed；增加真正退出程序及防覆寫／ZIP 逃逸／digest 驗證後 worktree 16 passed、6 subtests passed。完整初次 486 passed、1 skipped、13 subtests passed；補強 hook 選檔與 archive limits 後完整重驗另記錄。
  - 最終 Docker／Chromium 完整驗證：492 passed、1 skipped（外部 provider）、13 subtests passed；新增 19 項 worktree 測試，包含實際 Git hook 添加未選檔案的對照、staged 刪除回復、真正退出程序恢復與 Skill 的 worktree 契約驗收。批准／任務／worktree 相關 40 passed、10 subtests passed；語法／diff 檢查通過。
  - 邊界：預設支援非 Git workspace 不開啟 worktree；不自動合併任務成果。scoped commit 停用 repository hooks，驗收需先執行；symlink／junction、submodule、nested repo、過大成果拒絕自動清理。封存需無其他編輯者，壞 archive／中途 crash 的殘留保留供人工處理；封存磁碟保留待 T21，合作結果合併待 T13、外部 PR 待 T14。

- [x] **T13 — 可管理的多代理協作**
  - 原況：planner／coder／reviewer daemon thread 與 JSON 狀態，無 durable coordinator。
  - 工作：父子任務與 DAG、並行上限、獨立上下文／工具／預算／worktree、follow-up、等待／取消／超時、Reviewer 驗收、結果合併與衝突治理。
  - 驗收：子代理 crash／程序重啟／衝突時主代理可收斂；取消無殘留工作；Reviewer 未通過不交付；不自動遞迴超過限制。依賴 T05、T08、T09、T12。
  - 實作（2026-10-10）：SQLite 父子／依賴 DAG、跨 supervisor lease／併發與啟動恢復；獨立工作程序、persona 工具範圍、checkpoint 與 immutable working-copy snapshot／worktree，保留父 HEAD／index。invoke／check／manage 提供 follow-up、wait、子樹 cancel、deadline 及 Reviewer 通過後明確 merge；子代理與 Reviewer 共享且扣除根額度，限制深度與總數。
  - 收斂：safe checkpoint 恢復不重播已完成工具，舊 lease publication 拒絕；未知工具／準備／合併效果停止，`/agents` 提供 answer／resolve／人工查核後 abandon。Reviewer 失敗、逾時、未驗證或 findings 未清，均不交付；父內容衝突不覆寫，合併前保存備份，失敗保守回復並保留現場。
  - 相關驗證：16 passed、4 subtests passed；離線 SDK fixture 驅動真實 subprocess／Git worktree，測試實際 crash 後副作用計數一次、程序重啟、取消後 PID 退出、DAG／follow-up、owner／工具隔離、並行／深度上限、Reviewer 拒絕／程序失敗、晚到結果拒絕、衝突與 IO 失敗備份。語法／diff 檢查通過。
  - 回歸修正：首次完整測試發現旧 daemon test 需遷移，以及快速 Git 程序在 Job Object assignment 前退出的 Windows race。改為 CREATE_SUSPENDED、加入 Job 後 ResumeThread；加入失敗不執行命令，父程序快速退出亦清除後代。執行器／政策／協作相關重驗 48 passed、4 subtests passed；模型端工具清單亦限制於 persona，執行端再次檢查。
  - 快照補強：添加 Git 內容前驗證候選檔案與每層路徑，拒絕越界／symlink／junction，限制 10000 檔／256 MiB／單檔 64 MiB；使用真實 Windows junction 的前置拒絕與 DAG 相關測試 2 passed。
  - Docker／Chromium 完整回歸：510 passed、1 skipped（外部 provider）、17 subtests passed。最後快照前置檢查補強另以最終協作模組重驗，不將新增測試混算為本輪完整回歸數量。
  - 最終協作模組重驗（含快照補強）：17 passed、4 subtests passed；最終語法與 staged diff 檢查通過。
  - 邊界：需要 Git root；忽略檔不複製，舊 daemon JSON 不自動遷移，需人工查核後重建節點。原生工作程序不是 OS sandbox，仍依 T02。多檔 merge 非全域原子；未知效果不自動重播，需人工查核，取消保留 worktree／證據供 T12 封存。模型 Reviewer 品質與外部模型／平台未由離線測試證明，品質 benchmark 待 T21；外部 PR／CI 待 T14。

- [x] **T14 — Patch、PR／CI 與審查閉環**
  - 原況：edit_file 字串替換、Python AST 編輯與 Git 工具已有實作，未見完整 PR／CI 流程。
  - 工作：帶前置內容驗證的多檔 patch、衝突提示、diff 審查；可配置 GitHub／其他 Git 平台 PR、CI 狀態、review feedback 修復及交付摘要。
  - 驗收：過期 patch 不覆蓋新內容；review findings 有檔案／行號／重現；CI 未通過不宣稱完成；外部發布／合併遵守批准政策。依賴 T03、T10、T12。
  - 實作（2026-10-10）：在既有 `agent_execution/` 加入 owner／task／workspace 綁定的 UTF-8 多檔 patch，完整 diff／前置 SHA-256／失敗補償；GitHub PR、GitLab MR、SHA 綁定的 CI／review 門檻、同 PR 修復、merge 摘要。沿用 T03 一次性批准，publish／merge 各自批准，修復保留原始 acceptance；納入 Coder 工具清單與人類 `/delivery` 查核命令。
  - 發布範圍：選檔涵蓋 base 至 HEAD 全部新 commit 的變更，包含已刪除／還原的歷史檔案；提供 final diff 與 history_diff。要求乾淨 worktree、本機驗收通過，確切 SHA／branch lease push，過期 head／remote／配置拒絕；未知發布／合併只讀 reconcile，不重播 API 寫入。
  - 最終驗證：新增 27 項 T14 測試，真實 Git bare remote、localhost HTTP server、實際 pytest 與真正退出程序，涵蓋過期 patch、並行修改、局部 IO 失敗、junction／Windows Git 路徑別名、身份／批准隔離、CI／review／過期 SHA、修復驗收不可弱化、不明結果、歷史選檔與慢速 HTTP 讀取期限。完整 Docker／Chromium 回歸 538 passed、1 skipped（外部 provider）、25 subtests passed（548.94 秒）；既有 outbox／批准早期整合另驗 40 passed、5 subtests passed，兩平台／慢速 HTTP 最終相關測試 3 passed。語法及 staged diff 檢查通過。
  - 邊界：支援 UTF-8 普通檔案，拒絕 links／越界／Git metadata，不宣稱多檔全域原子；patch 32 檔／8 MiB 單檔／16 MiB 合計，diff 2 MiB，delivery 256 檔／256 commit，artifact 10000 筆／64 MiB。中斷保留證據，人工 abandon 需 180 秒後查核 worker；不自動回復遠端或重試未知副作用。
  - 平台邊界：不支援 fork PR／MR。GitHub 以同作者在 comment 更新後對當前 SHA 的 APPROVED 確認 finding；GitLab 需 resolved DiffNote、approval reset-on-push 及 approvals API 權限。finding 重現欄位保存審查者原文，未自動執行。平台 branch protection 與實際帳號／CI／合併須部署者另驗；本次本機 fixture 不代表已建立外部 PR。`verified_delivery` 僅在明確確認 merge 後為 true，與 T10 task 驗收分開。

- [x] **T15 — 程式碼索引與跨語言理解**
  - 原況：Python Tree-sitter／Jedi、手動 embedding 建索引；語意搜尋讀取候選並算 cosine。
  - 工作：增量索引與檔案變更追蹤、embedding 模型版本、ignore／secret 過濾、精確文字搜尋、LSP／多語言符號、較大資料的 ANN。
  - 驗收：修改／刪除／更換模型不殘留舊索引；Python、Go、JS／TS 的符號與引用以 fixture 驗證；建立品質與延遲基準。依賴 T01、T09。
  - 實作（2026-10-10）：新增既有 `agent_execution/code_index/`，SHA-256 增量同步／刪除清理、workspace／grammar／embedding 版本、nested ignore／秘密過濾、literal／symbol／semantic 三種查詢、Python／Go／JS／TS／JSX／TSX 語法符號、接收者 qualified name、持久化 LSH ANN（最多 512 候選）。原 build／search／definition／references entry points 接入，新增 refresh_code_index／code_symbols，接入協作角色工具清單。舊無版本向量首次查詢即重建；generated DB 停止 Git 追蹤，原本機檔案保留。
  - 解析與治理：Python Jedi 與部署者配置的 stdio LSP 均用 bounded worker，支援 initialize／didOpen／definition／references／documentSymbol／shutdown、UTF-16／Unicode 轉換、越界／排除檔過濾、程序樹 timeout／cancel。未配置 LSP 的 Go／JS／TS 明確回傳 unresolved syntax candidates，不猜測 binding。Windows Tree-sitter 0.26 Point 屬性在 700 函式壓測觸發原生崩潰，改依 byte offsets 計算位置，保留大索引回歸測試。
  - 向量治理：預設離線 hashing；遠端 opt-in 身份包括 endpoint／model／revision／dimension。refresh 最多 32 API，首錯即停止、不 retry／fallback 混用；搜尋不做 remote backfill，保留 pending 狀態及 T09 預算。單檔 1 MiB／檔案合計 64 MiB／10000 files／20000 symbols／200000 identifiers／256 MiB DB，超過 aggregate 額度拒絕。
  - 品質基準：四語言 fixture + 1000 合成函式，1017 symbols、10 query；symbol recall 1.0、ANN recall@5 1.0，build 1301.994 ms／unchanged refresh 35.907 ms／query median 59.504 ms、max 81.722 ms。重現 `python -m agent_execution.code_index.benchmark --symbols 1000 --queries 10`，紀錄 `agent_execution/code_index/benchmark_baseline.json`；完整 chunk query 對照 exhaustive cosine，不代表自然語言模型品質或正式工程延遲。
  - 索引快照：查詢期間重驗原始內容，並行 refresh 不能令舊 LSP 結果通過；task worktree 的 cache 自動放在外部 TaskStore 同層，支援受信任 AGENT_CODE_INDEX_DIR，以 workspace 雜湊隔離，不讓搜尋破壞 T14 的 clean-worktree 門檻。
  - 驗證紀錄：新增 27 項索引測試；索引／政策階段相關 39 passed、5 subtests passed，與協作整合 56 passed、9 subtests passed；增量／秘密／Unicode／原生 grammar／實際 Jedi／本機 LSP protocol／取消／ANN／品質與延遲均有案例。最終索引專項 27 passed、5 subtests passed（20.94 秒）；開啟 Docker／Chromium 的完整回歸 565 passed、1 skipped（外部 provider）、30 subtests passed（620.86 秒），另有 1 項既有 AnyIO 棄用警告。package／tools 語法與 Git diff 檢查通過。
  - 邊界：秘密檢測是可誤判／漏判的規則，須另設專案 ignore；只處理有界 UTF-8，未支援全部語言型別推導。LSP command 是受信任部署設定，host 依 T02；正式 gopls／TS server 與遠端 embedding 語意品質尚待外部環境驗證。provider 模型同名換版須由部署者更新 revision，LSH 屬近似搜尋，真實任務 benchmark 待 T21。

- [x] **T16 — IDE 與 Dashboard 的任務審查介面**
  - 原況：VS Code 串流直接取代選取文字；Dashboard 主要管理記憶與觀察任務。
  - 工作：套用前 diff、atomic apply／undo、串流 UTF-8 與取消處理；統一顯示 task plan、工具事件、驗收、批准與 artifacts。
  - 驗收：斷線／取消／並行串流不破壞原始選取；UI 操作與身份政策一致；瀏覽器與 VS Code 實測並留證據。依賴 T03、T06、T10、T14。
  - 實作（2026-10-10）：既有 vscode-extension 新增 readonly diff 提案、明確 atomic editor apply／undo、原生 Undo、UTF-8 incremental decoder／done 完整性、abort／斷線拒絕、文件版本與並行保護、SecretStorage Token。ide_server.py 保留入口，實作收攏 agent_execution/ide/，admin Token／workspace／JSON 限額、T08 checkpoint／T09 模型預算，不讀檔上下文或寫入來源。
  - 審查：新增 owner-bound review API／Dashboard 會話欄、計畫／工具事件／驗收／人工問題／artifact diff 與歷史證據／待批准操作；文字安全顯示、過期回覆拒絕、admin 取消／批准／拒絕，viewer 唯讀。批准仍從原 task 入口續跑，UI 不重播工具或發布／合併。
  - 邊界：提案 process-local、僅支援既有 workspace 文件；空／未變替換拒絕、最多 32 提案。API 任務資料不跨通道身份推定；無背景輪詢／任意 artifact filesystem 下載。完成生成僅 unverified，不等同已套用或交付；provider cancel 受下一寫入／SDK timeout 約束，正式外部 LLM 尚待驗證。
  - 驗證紀錄：T16 最終驗證（2026-10-10）：新增 11 項 Python 測試；最終 UI／API 與舊 Dashboard 整合重驗 14 passed（24.70 秒），Node 9 passed，實際 VS Code 1.141.0 Extension Host 14 案例通過。開啟 Docker／Chromium 的完整回歸 576 passed、1 skipped（外部 provider）、30 subtests passed（884.64 秒），另有 1 項既有 AnyIO 棄用警告；Python／Node 語法及 Git diff 檢查通過。證據摘要保存於 vscode-extension/tests/verification_baseline.json，原始 log／截圖保留於忽略的 temp/。

## P3：自動化、整合、記憶與品質治理

- [ ] **T17 — Agent／Skill 排程與持續追蹤**
  - 現況：排程已有 command、maintenance.summary、SQLite 工作鎖，未實作 CLI help 提到的通用 script job。
  - 工作：新增 Agent／Skill job、自然語言排程、時區、會話 follow-up、停止條件、只在有意義變更通知；補 command timeout、鎖 lease、重啟／misfire 與 bounded retry。
  - 驗收：多入口同時啟動不重複執行；Asia/Taipei 排程時間正確；長工作／crash 不永久卡住；狀態未變保持安靜，完成或需人處理才通知。依賴 T01、T05、T08、T09。
  - 實作（2026-10-10）：獨立 scheduling package、Agent／Skill／script／唯讀 watch、明確自然語句與 ZoneInfo、owner／會話／workspace 還原、人工批准；原子 claim／續租／fencing、原生 worker timeout／取消、misfire 合併、未知副作用停用與唯讀最多兩次 retry。通知去重、四次歷史與停止條件，舊遠端工作隔離停用。
  - 驗證：排程／policy 38 passed；最後排程／Hook／舊 scheduler 整合 32 passed。完整回歸先遇既有 CLI 測試載入本機 Laya 原生模型存取錯誤，改為離線 classifier fixture；後續舊 Hook fixture 需明確授予 owner 現行 admin，修正後相關案例通過。整體最終回歸結果集中記錄於 T21。
  - 邊界／後續：通知持久化且可透過工具／CLI 查詢；正式平台自動推送及真實 provider 追蹤尚待驗收，因此保留未勾選。

- [ ] **T18 — MCP 與動態工具／Skills 的可信生命週期**
  - 現況：stdio MCP 與工具熱載入可用；動態 Python 模組直接載入；MCP 路由依工具名稱分割，需補 server 名含底線案例。
  - 工作：remote MCP transport／OAuth、timeout／重連／health、完整 schema 與結果驗證、明確工具到 server 映射；工具／Skill 安装、版本、來源、測試、核准啟用與 rollback。
  - 驗收：缺 server／網路斷線不阻塞其他工具；名稱碰撞／底線不誤路由；惡意 schema 或未通過測試的生成工具不能啟用。依賴 T01、T03、T10。
  - 實作（2026-10-10）：exact MCP route／碰撞雜湊、離線有界 JSON Schema 與 structuredContent／isError 驗證、stdio EOF／取消／程序樹清理、四 worker startup、health／明確 reconnect；2025-11-25 remote POST JSON／SSE、session／token_env。生成工具只 stage，工具／Skill 版本來源與 digest、Docker tests、人工批准、撤回與批准 rollback；提交的 builtin inventory。
  - 驗證：相關 48 passed，包含原生 stdio fixture、實際 Docker 成功／失敗版本、一次性批准／回復／owner 隔離及惡意 schema。尚缺完整互動 OAuth discovery／PKCE／refresh 和正式 remote provider 驗收，保留未勾選；激活需重啟刷新 schema。

- [ ] **T19 — 持久瀏覽器、文件解析與語音**
  - 現況：Playwright action／截圖、文字及圖片附件可用；WebSocket audio 只解析 metadata，未提供 STT／TTS。
  - 工作：使用者隔離的持久 browser session、動作後斷言、下載 artifacts；PDF／Office 解析／OCR 與來源頁碼；真正 STT／TTS、音訊容量與取消。可分成三個獨立交付。
  - 驗收：多步登入／表單流程可續跑並驗證結果；PDF／Office 不以截斷 Base64 充作理解；語音可辨識及播放，重連／中斷不混流。依賴 T02、T05、T06、T07。
  - 實作（2026-10-10）：media package 的 owner profile／cookie state、嚴格動作斷言／私有下載、原生 worker timeout／取消；PDF／Office 來源單位、ZIP／XML 限制、實際 Tesseract OCR；真正 SDK STT／TTS worker、可播放 WAV、WebSocket 有序 frame／容量／取消／reconnect 隔離。
  - 驗證：相關 51 passed，實際 Chromium cookie 登入／續跑／異主隔離／assert／下載，原生 DOCX／PPTX／XLSX 中文、扫描 PDF OCR，本機 HTTP SDK 語音 contract／WAV／取消。正式外部語音品質、真實裝置播放與外部網站尚未驗收，保持未勾選。profile admission limits 不是硬磁碟配額；host browser／document parser 仍需部署層隔離。

- [ ] **T20 — 記憶品質與可控自我改善**
  - 現況：三類記憶、遺忘、背景更新與 Skill Doctor 已存在。
  - 工作：租戶／專案隔離、retrieval precision／recall 評測、來源信任與矛盾測試；Doctor persona／Skill 建議經版本審查、A/B 驗證、啟用及撤回，避免錯誤準則無限累積。
  - 驗收：跨使用者記憶不能注入；過期／矛盾資料可測試淘汰；新 persona 必須有可量測改善且可回復。依賴 T05、T09、T10。
  - 實作（2026-10-10）：owner 版本提案、來源／suite／評測 digest；固定案例 A/B 成功率改善＋零回歸／非預期效果 gate，baseline 防過期、人工批准／撤回／批准 rollback、有界原生評測 runner。Doctor --apply 改 stage；Skill 建議引用 T18 版本且不能自動啟用。來源標示、legacy persona／單值矛盾排除、quoted reference 低權限注入與 labeled precision／recall／forbidden-hit 評測。
  - 驗證：相關最終 58 passed；adapter scope 補強重驗 6 passed。涵蓋真實原生 adapter、Doctor 不注入、owner／過期／矛盾／來源、評測回歸／副作用／baseline／批准／rollback。正式 provider persona／Skill A/B 與較廣標記資料集品質尚待驗收，保留未勾選；未啟用任何 production persona。

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

## 後續驗證環境備註

- 2026-10-09：直接從根目錄 `pytest -q` 會收集到忽略於 Git 的 `temp/test_vision.py` 舊實驗檔，因舊 import 名称而 collection 失敗。正式驗證依 README 使用 `python -m pytest tests agent_execution/tests -q`。T21 應補 testpaths／排除實驗目錄，並處理已列出的 patch_server.py 語法問題。

## 比較來源

OpenAI 官方文件，查核日期 2026-10-09；來源提供能力基準，本檔的優先序與待辦為本專案程式分析所得：

- [Git worktrees](https://learn.chatgpt.com/docs/environments/git-worktrees)：隔離任務及保留可審查改動。
- [Subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents)：專門角色、並行委派及 agent thread 控制。
- [Scheduled tasks](https://learn.chatgpt.com/docs/automations?surface=app)：背景任務、會話追蹤、Skills 與執行結果管理。
- [Agent approvals & security](https://learn.chatgpt.com/docs/agent-approvals-security)：sandbox、approval 與網路存取政策。

## 原始盤點驗證（文件基線）

- 僅修改 README.md 及新增本檔，未修改程式或新增測試。
- `venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"`：325 tests，OK。
- 檢查文件本機連結、功能對應模組及 `git diff --check`。
- 額外 AST 語法掃描根目錄、tools、tests、communication_adapters 的 107 個 Python 檔案：106 個通過；既有 `patch_server.py:1` 語法錯誤，記入 T21，未在本次文件變更中修復。
- 未進行真實 LLM provider、Telegram／Discord／LINE、Docker、瀏覽器／VS Code 或正式部署驗證；測試通過不代表這些整合已通過。
