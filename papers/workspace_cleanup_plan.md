# Workspace 暫存目錄與對話 session 刪除設計

日期：2026-10-14。狀態：規劃，尚未實作、尚未刪除任何目錄。

使用者已決定：封存內容保留到使用者刪除所屬對話 session，不採 30 天或任何其他天數到期刪除。

## 1. 目標與現況

目標是管理 Agent 依使用者要求在 workspace 建立的暫存程式目錄，提供登記、預覽、可還原封存、還原、刪除 session 後永久清理、失敗恢復及稽核。實作待辦見 [todo.md](todo.md)。

目前已核對的程式：

- `base.py:workspace_root()` 決定配置／任務的 workspace；`delete_file()` 只刪除單一檔案，拒絕刪除目錄。
- `tools/project_scaffold.py` 建立程式目錄，但未保存使用者、session 與目錄的所有權登記。
- `agent_execution/tasks.py` 保存任務與工具步驟；owner 含來源、身份、專案、session，但不是程式目錄清冊。
- `agent_execution/approvals.py` 已有身份、session、動作、參數摘要、到期及一次性批准，可重用。
- `agent_execution/worktrees.py` 已有 Git worktree 封存／還原。一般暫存目錄須新增流程，不能混用任意資料夾與 Git worktree 的移除方式。
- MyAI `desktop_assistant/storage/history.py:delete_message()` 與 `controllers/chat.py:delete_message()` 只刪除本機訊息；PythonAI 上下文仍保留。目前有建立／切換對話，尚無完整對話 session 刪除入口。

刪除單則訊息、清空畫面、切換對話、斷線、退出 MyAI、任務結束、取消任務、`/clear`／`/reset` 均不等於使用者刪除 session，也不觸發本計畫的永久目錄刪除。

## 2. 生命周期與預設政策

| 類別 | 登記方式 | 保存政策 | 刪除 session 時 |
| --- | --- | --- | --- |
| 新建且明確作為 session 暫存的程式／scratch 目錄 | 建立前登記 `temporary` | active 或 archived 都保留至 session 刪除 | 無其他使用者／session 引用且未保護才永久清理 |
| 使用者選擇保留的正式程式 | 人工設定 `retained`／保護標記 | 不隨 session 清理 | 保留目錄，登記轉為使用者所有；移除該 session 引用 |
| 多個 session 共用的目錄 | 登記多筆引用 | 有有效引用就保留 | 僅移除本 session 引用；最後一個引用刪除時才評估清理 |
| 既有但未登記的目錄 | 初始 `unmanaged` | 不推測、不自動刪除 | 不納入清理，列出待人工確認 |
| 受管理 Git worktree | 使用既有 WorktreeStore | 依 worktree 封存／lease 政策 | 路由至 worktree 專用流程，不能一般遞迴刪除 |

「Agent 建立」不代表「可丟棄」。建立時須根據使用者需求決定用途；正式交付或用途不明時預設 retained，明確暫存／試作才登記 temporary。模型不能自行把未知目錄變成可刪除目錄，也不能在收到清理要求後偷偷改成 retained。

同一使用者跨 session 的共用須由受管理的開啟／繼續編輯入口新增引用。第一版禁止未登記的跨 session 寫入；只讀瀏覽不自動建立永久引用。共享引用與 retained 標記均須在預覽前重新查核。

## 3. 使用者流程

### 3.1 暫時移除程式目錄

1. 使用者選擇已登記目錄，要求「封存／移出 workspace」。
2. 後端產生預覽：目錄顯示名稱、workspace 相對路徑、來源任務、所屬 session、檔案數、大小、共用引用、保護狀態與可還原說明。不讀取或顯示完整程式／憑證內容。
3. 使用者確認精確清單。批准綁定清單摘要及版本，不能擴大為整個 workspace。
4. 原子移入服務管理的封存區，更新狀態為 archived，移除活躍索引／檔案連結。
5. 提供「還原」操作。封存沒有時間到期，持續保留至最後相關 session 被使用者刪除。

多 session 共用或 retained 目錄不允許透過單一 session 的暫存清理移走整個程式；改為解除本 session 引用，或另走人工正式專案管理流程。

### 3.2 還原

1. 使用者從封存清冊選擇項目，後端驗證所有權、封存完整性與原 workspace。
2. 預覽原位置及衝突。原位置已存在時拒絕覆寫，不自動合併；若使用者選擇新名稱，須重新產生批准。
3. 原子移回已批准位置，更新 registry／索引，保留 operation 記錄。
4. session 正在刪除或已刪除時不得還原。想保留內容，須在刪除 session 的確認之前選擇「保留程式」。

### 3.3 使用者刪除整個 session

1. MyAI 新增獨立「刪除對話」入口，與現有「刪除訊息」分開。
2. 後端回傳完整刪除預覽：對話、該 session 獨占的 active 暫存目錄、已封存目錄、共用保留項、retained 項、未知項與仍在使用的項目。
3. UI 明確說明：「刪除本對話會永久刪除列出的暫存程式及封存副本；刪除後無法透過本功能還原。」使用者可在此之前選擇保留程式，清單因此變動時重新預覽。
4. 使用者在同一確認中批准刪除對話與精確暫存目錄清單。這個批准就是後續永久清理的授權，不在背景另發模糊的全域確認。
5. 後端先持久化刪除意圖、tombstone 與 outbox，session 進入 deleting；拒絕新指令、新引用及舊訊息重播，不立即宣稱完成。
6. 有執行中的任務時先提示，使用者可返回等待，或明確同意取消。取消只處理登記為該任務建立的程序樹；等待任務、lease、子代理、排程與程序確實停止後才能移走目錄。無法證明已停止時標示 blocked，不強制刪除。
7. 執行預覽時凍結的每個目錄操作：active 獨占暫存目錄先原子移入隔離封存區，再永久清理；已有 archived 副本依批准永久清理。登記、檔案身份或內容變動即停止該項並要求新預覽。
8. 清除該 session 的後端對話上下文、待送事件、附件與批准，撤銷專屬排程／延後任務。所有清理完成後再清除 TaskStore checkpoint／tool input/result／reply 中的原始內容與該 session 的原始 episodic 對話，移除對應檢索索引；刪除進行中先保留必要恢復資料。budget 數字、lease/fence、操作狀態與去重 tombstone 可最小化保留。既有共用 semantic/procedure 記憶不以單一 session 清理全域刪除；依來源追蹤解除已刪原始內容的引用，無法確定來源時列為獨立資料保留政策問題。獨立被使用者保留的排程先轉移所有權／引用，不能留有指向已刪除目錄的工作。
9. MyAI 收到持久化接受回覆後可顯示「刪除中」並移出一般清單；只有後端回報全數處理完成才顯示「已刪除」。封鎖或部分失敗時保留管理入口、剩餘清單與重試方式。
10. 保留最小 tombstone／操作稽核，以拒絕離線舊指令、重複請求和遲到結果。不保留被刪除對話全文或檔案內容作為稽核。

沒有登記的既有目錄不會因刪除對話而消失。這些目錄需先完成人工接管；若沒有足夠所有權證據，維持 unmanaged。

## 4. 目錄登記與 session 身份

新增獨立模組 `agent_execution/workspace_lifecycle/`，集中 store、paths、registry、preview、archive、restore、purge、recovery、session_delete、README、測試 fixtures 與必要資源。相關測試集中 `agent_execution/tests/test_workspace_lifecycle*.py`。MyAI 只在既有 `desktop_assistant/` 修改入口與協定接收，不建立另一套清理器。

資料表建議：

- `workspace_roots`：root_id、配置專案、規範化實體路徑、volume/file identity、受信任封存根目錄。
- `managed_directories`：directory_id、root_id、相對路徑、建立者 source/identity、origin task_id、directory file identity、purpose、retained、state、generation、建立時間、最近使用時間。
- `directory_session_refs`：directory_id、canonical session_id、引用來源及狀態；支援多對多。
- `cleanup_plans`：plan_id、操作者、session_id、動作、精確項目、快照摘要、版本、到期、approval_id、狀態。
- `cleanup_operations`／`cleanup_items`：operation_id、項目狀態、lease/fence、原路徑、封存路徑、內容 manifest、錯誤、重試次數及最後確認結果。
- `session_deletions`／`deletion_outbox`：session tombstone、預覽摘要、持久化清理事件、操作關聯、發布與完成狀態。

優先使用 TaskStore 的 SQLite 儲存，與任務 lease／ownership 檢查整合；不用 LLM 對話文字充當所有權資料。schema migration 需版本管理，不改寫既有任務語意。

新增持久化 canonical session_id：由後端綁定登入身份、project 與對話，不信任客户端傳入 owner。TaskStore 現有 owner 的 transport session 作為映射來源；同一對話在不同裝置應映射到同一 canonical session。既有空 conversation_id 的「原有對話」須保留裝置範圍，不能把所有使用者／裝置的空 ID 合併。

token 字串不作目錄 owner；金鑰輪替不應改變資料所屬身份。尚未建立可信身份連結時，不把 Telegram、WebSocket 或不同帳號視為同一使用者。

## 5. 建立與持續使用的接入

1. `project_scaffold` 改走先登記 creating、保留 directory_id、再建檔的流程；成功轉 active，部分失敗保留 creating_failed 及已建立範圍供人工／恢復程序處理。
2. 一般 `write_file` 建立新程式必須先使用 `create_managed_directory` 或既有清冊選擇入口。修改已有目錄必須驗證 owner／用途／session 引用；不能只因 mkdir 已執行就猜測所有權。
3. 工具、Skills、子代理、附件匯出、coordinator、受管理排程及 worktree dispatch 共用 registry 接口。不要讓每個入口各自維護一份目錄歸屬。
4. 模型只可列出／預覽自己可見的目錄、提出封存請求；不能自行批准、purge 或以原始路徑指定遞迴刪除。
5. 透過已授權的任意 host 腳本或外部程式建立但未登記的目錄，列為 unmanaged，不在事後自動宣称「安全可刪除」。

## 6. 檔案操作安全與封存位置

- 請求只接收 directory_id／plan_id／operation_id，不接收任意絕對刪除路徑、glob、shell 刪除指令。
- 拒絕 workspace 根目錄、根的祖先、PythonAI／MyAI 原始碼與環境目錄、服務資料庫、封存根、未知根，以及與其他登記目錄重疊的目錄。
- 同時驗證規範化路徑與 OS file identity；預覽與執行前重新確認父鏈、目錄、volume、manifest。重點測試 Windows 大小寫、junction／reparse point、symlink、路徑置換及 TOCTOU。
- 第一版拒絕目錄樹內的 symlink／junction／reparse point、難以確認的 hard link 與不支援檔案類型；不跟隨連結去掃描、移動或清理其他目錄。
- 實作須使用不跟隨連結的列舉與盡可能以開啟的目錄 handle/file identity 操作，避免只做一次 `resolve()` 就遞迴刪除。無法建立平台所需保障時拒絕該項。
- 登記在原子建立／改名邊界前後核對，cleanup lease 與任務執行入口協調；registry 鎖不能阻止外部編輯器，內容變動、占用或開啟控制失敗時留下 blocked，不強行殺外部程序。
- 預設封存位置為已核准 workspace 同一實體磁碟的服務管理區：`<workspace.parent>/.pythonai-workspace-trash/<root_id>/<directory_id>/<operation_id>/payload`。封存位置只能由服務配置產生，不能讓模型授予此位置的讀寫權限。
- 封存根必須驗證非來源的祖先／子孫、非 reparse、在允許的服務管理父目錄、與來源同 volume，並使用使用者隔離 ACL。第一版不做跨磁碟複製後刪除；不符條件就拒絕，保留原目錄。
- 使用同磁碟原子 rename 隔離，再永久刪除隔離區的已登記目錄；不拼接 shell 指令。永久刪除可能有部分成功，必須逐項／逐檔記錄可恢復狀態，不能宣稱 rollback 可以救回已 purge 的檔案。
- 封存區排除於 Git、code index、snapshot、任務驗收、模型搜索與一般 artifact 發布，避免封存內容被當成新的任務修改或再次傳送。
- 設定檔案數、大小、掃描時間、併發、封存容量與重試上限。封存無到期；容量不足時告知使用者選擇刪除 session／保留程式／管理容量，不能擅自依年齡淘汰。
- 此流程是應用管理機制；已獲准執行任意 host 程式的 admin 與外部檔案工具仍可能直接修改資料。不能宣稱 registry 能限制所有 OS 副作用；偵測不一致後轉 blocked／unmanaged。

## 7. 狀態、交易與恢復

目錄狀態：`creating -> active -> archiving -> archived -> restoring -> active`；刪除 session 另外走 `active/archived -> quarantining -> purge_pending -> purging -> purged`。各操作可進入 blocked／failed；未知的執行結果進入 recovery_required。

session 狀態：`active -> delete_requested -> deleting -> deleted`；取消只允許尚未 consume 批准且未產生刪除副作用時，或預覽階段。開始永久刪除後，不能用「取消」宣稱恢復已刪內容。

SQLite 與檔案系統沒有共同 ACID 交易。執行順序必須是：持久化意圖與 lease/fence -> 執行單項原子移動 -> 核對實體位置與 file identity -> 持久化結果 -> purge 逐項完成 -> session 完成。先清除資料庫紀錄再刪目錄會失去可追蹤目標，禁止使用。

恢復程序啟動時與有界背景批次執行，檢查已存在的意圖與實體位置，不重新推算要刪什麼。不確定來源／封存雙存、兩側皆缺、內容被改、檔案占用或 lease 被取代時停止，不盲目重播。相同 idempotency key 與相同清單只對應同一 operation；參數不同不得覆用 key。

session tombstone 與 outbox 必須可原子寫入同一服務資料庫；若現有 session/event store 是不同 DB，使用 outbox 配合冪等消費完成最終一致，不假裝跨 DB 原子提交。刪除過程保留必要 lease/fence 與最小 tombstone，防止遲到 worker 再建立目錄。

## 8. 權限、協定與資訊顯示

新增服務層的 `session_owner_cleanup` 能力：經過真實使用者確認後，只能清理自身 session 下已登記且符合預覽的 temporary 項目；不能任意操作 workspace，也不改變 viewer／operator 一般工具權限。跨使用者、未知目錄接管、正式專案刪除仍要求部署端 admin 並走獨立明確操作。

批准綁定操作者 identity、發起 session、目標 canonical session、root_id、directory_id 清單、generation、manifest digest、動作與有效期；consume 為一次性。后台恢復使用已持久化授權與 frozen 清單，只重試未完成項，不延長／擴大批准。

建議服務接口（名稱仍為設計，不代表已存在）：

- `workspace.list`／`workspace.preview`／`workspace.archive.request`／`workspace.restore.request`。
- `session.delete.preview`／`session.delete.request`／`session.delete.status`。
- `cleanup.progress`／`cleanup.completed`／`cleanup.blocked` 可靠事件，沿用 seq／ack 與 owner 隔離。
- CLI／Telegram 等人類入口可使用 `/workspace list|preview|archive|restore`、`/session-delete preview|confirm|status`，與 MyAI 呼叫同一服務函式，不實作第二套刪除邏輯。具體 transport syntax 在實作前凍結。

後端 hello 宣告新的 capability；舊服務未宣告時停用「刪除對話及清理程式」，不能退化為只刪本機畫面。離線可查看已快取預覽，不能用舊預覽批准執行；恢復連線後重新確認版本與清單。

## 9. 既有目錄接管與上線

1. 第一次上線只做 inventory，不刪除或改名。現有 `workspace/go_hello_world`、`workspace/hello_world` 等均先列為 unmanaged；本計畫不把它們加入本專案 Git commit。
2. 顯示候選目錄與可能相關 task/tool 紀錄，這些紀錄只是人工判斷線索，不是自動清理的權威證據。
3. 使用者／admin 明確確認 owner、session、用途及是否保留，才接管；有既存 Git、重疊、連結或共用疑慮先拒絕並說明。
4. 先上 registry＋預覽，再上可還原封存／還原，最後才開啟刪除 session 後永久清理。第一輪永久清理必須以獨立 fixture session／目錄實測，不使用正式使用者目錄。
5. 模組可透過 feature flag 關閉新增破壞操作；停用時仍可查看進度與恢復既有未完成意圖。程式 rollback 不會還原已永久刪除的檔案，因此資料 migration 要能舊版讀取或有明確停機回復方案。

## 10. 驗收與交付門檻

- 必須有使用者/session/目錄登記、人工 exact-list 批准與持久化刪除意圖，才能永久清理。
- session 存在期間沒有任何時間到期 purge；封存與還原都能通過實際檔案內容雜湊驗證。
- session 刪除只永久清理自身獨占 temporary 目錄；retained、共用、未知、其他使用者與 workspace 根全部保留。
- 原生 Windows junction／reparse／占用／路徑置換測試與 SQLite 崩潰恢復、並發引用、重複確認、離線／重播／遲到 worker 測試通過。
- 後端與 MyAI 真實本機 WS fixture 驗證完整流程；各測試只用可丟棄暫存根，不能碰正式 workspace 或用户 session。
- 明確區分本機 fixtures、原生檔案系統、Docker、真實 MyAI 人工點擊與正式通道驗證。只有真的執行的結果才標記完成。
- 程式異動需新增／更新相關測試、完整必要驗證、`git diff --check`，每個實作里程碑建立對應 commit；若同時改 PythonAI 與 MyAI，兩個 repo 各自建立 commit。

本次交付僅為本設計與依序待辦；沒有建立刪除器、變更角色、發送清理命令或刪除任何檔案。
