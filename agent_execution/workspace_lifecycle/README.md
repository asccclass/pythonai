# Workspace lifecycle

目錄歸屬保存於 TaskStore 的 `wl_*` version 1 tables。所有權使用 source／identity／project；conversation 非空時跨裝置保持同一 session，原有空 conversation 保留 transport session 範圍。沒有以 token 字串作所有權。

新程式目錄以 `project_scaffold` 或 `write_file` 建立時登記；來源任務明確包含暫存、臨時、試作、temporary 或 scratch 時為 temporary，其餘 retained。既有目錄維持 unmanaged。跨 session 寫入已登記目錄前必須由人類入口 attach；未知目錄的接管需 admin。

已封存目錄也可改為 retained；原 session 刪除後保留其 registry／封存內容。同一使用者可在新對話 `/workspace attach <directory-id>`，再預覽／確認 restore，還原後仍為 retained。保留或共用內容不允許被暫存封存／刪除流程清理。

清理從預覽開始，綁定 directory ID、generation、檔案身份與內容摘要。共用、retained、未知和正在執行任務的目錄不能直接清理。封存沒有時間到期；僅使用者刪除最後相關 session 才永久清理 temporary 內容。

Windows 操作使用原生 pinned handle，移動透過 `NtSetInformationFile` 的已鎖定目的目錄 handle，不依字串路徑推測資料位置。子檔案鎖在原子 rename 前關閉，來源物件／父鏈仍保持鎖定，移動後重新鎖定全部內容並驗證同一 manifest；不一致停在 blocked。永久刪除只操作已 pin 的物件；partial purge 只接受原批准 manifest 的剩餘子集，新增檔案保留。連結、reparse、hard link、檔案占用、跨 volume 及不支援平台拒絕破壞操作。

`/workspace preview <directory-id> archive|restore` 產生預覽，`/workspace request <plan-id>` 請求批准，真實使用者以 `/workspace confirm <plan-id>` 確認；一般 `/approve`＋`/workspace execute <plan-id> <approval-id>` 也可用。blocked 操作以 `/workspace resume <plan-id>` 使用原 frozen 清單續跑。模型不能呼叫人類 dispatcher。`AGENT_WORKSPACE_CLEANUP=0` 停用新增破壞操作，但仍可處理已有意圖。

這是應用管理流程，無法限制已獲准執行任意 host 程式的 admin 或外部編輯器的所有副作用。首次接管未知目錄需明確人工操作，不能依名稱或時間猜測。

設計與里程碑：`papers/workspace_cleanup_plan.md`、`papers/todo.md`。

## 刪除對話與操作手冊

MyAI 連上支援 `workspace_lifecycle.version=1` 的後端後，可使用「刪除對話」。預覽列出永久刪除目錄、檔案數／容量、保留原因、未納管目錄數及要停用的排程；勾選「保留程式」會變更登記並重新預覽。只有使用者確認目前清單才提交刪除。離線、舊後端或停用 capability 時不提供批准；斷線後查詢原 plan，不自動重送確認。

CLI 使用 `/session-delete preview`，檢閱後 `/session-delete confirm <plan-id>`；`/session-delete status <plan-id>` 查詢並完成資料清除，`/session-delete resume <plan-id>` 續跑原清單。預覽 300 秒過期只使批准失效，不會刪除資料。CLI 刪除 session 後，另開新 session 請以新的 `AGENT_SESSION_ID` 啟動；不要重用已刪除 ID。Telegram／其他 communication transport 尚無完整資料清除 consumer，現在拒絕 session-delete，避免只刪程式卻留下傳輸資料而誤報完成。

執行中的 task／coordinator、live schedule lease／memory worker 或 MyAI chat／audio 會阻擋；先明確中斷任務並等待實際結束，再重新預覽。清理不強殺外部程序。確認後先持久化 deleting tombstone，停止新 task、引用與工具；停用凍結清單內的排程，再隔離與 purge。所有目錄完成後，outbox 清除 checkpoint、工具內容、原始 episodic 對話、批准參數、排程內容與結果、owner 檢索快取及可靠事件。共用衍生記憶依政策保留。保留最小狀態／ID／摘要／結果供防重播及恢復；這是邏輯刪除，不承諾覆寫 SQLite 空閒頁、磁碟或備份。

若 blocked，先保留現況，以原 plan 查詢原因；修復檔案占用等問題後續跑。新增／修改檔案、改變引用或檔案身份時拒絕擴大批准。不要手動刪 registry 或刪隔離區來「修復」。purge 完成部分無法還原，cancel 也不會回復它。跨磁碟、網路磁碟、Git／worktree metadata、reparse／hard link 及服務資料目錄都拒絕暫存清理；Git worktree 繼續使用既有 worktree 流程。

上線順序：停服務並備份 TaskStore／ApprovalStore／事件 DB、MyAI history DB 及 workspace，部署後先 inventory，確認既有目錄仍 unmanaged，再以可丟棄目錄完成 archive／restore／session-delete。不得用正式 `go_hello_world`、`hello_world` 作測試。`AGENT_WORKSPACE_CLEANUP=0` 阻擋新批准執行，已持久化操作仍可狀態查詢／續跑。資料表為 additive migration；回退程式前先停服務、處理未完成操作，再還原同一備份組，不自行 DROP `wl_*`。程式 rollback 無法復原已 purge 檔案。目錄容量／manifest 限制達上限時拒絕新增，不自動淘汰。
