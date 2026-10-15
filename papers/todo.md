# Workspace 暫存目錄清理實作待辦

規劃日期：2026-10-14。詳細設計：[workspace_cleanup_plan.md](workspace_cleanup_plan.md)。既有全專案待辦保留於 [1.todo.md](1.todo.md)。

使用者已決定：暫存／封存資料保留至所屬對話 session 被使用者刪除，不使用天數到期清理。2026-10-14 開始實作；完成範圍與待驗收項分別記錄，不以部分功能宣稱整個里程碑完成。

## M1 — 歸屬清冊與可檢視預覽

- [x] **W01 — 凍結生命周期與接口契約**（無依賴）
  - 決定 temporary／retained／shared／unmanaged 分類、canonical session 映射、舊「原有對話」、session-owner 的窄範圍清理能力、封存根與容量限制。
  - 交付：`agent_execution/workspace_lifecycle/README.md`、協定與狀態契約、fixture 規格。
  - 驗收：使用者刪 session 的語意與 preview／confirm／status 一致；刪訊息、`/clear`、斷線及退出不觸發 purge。
- [x] **W02 — 建立版本化 registry 與 session deletion/outbox schema**（W01）
  - 實作 workspace identity、managed directories、引用、plans、operations、tombstones 與 migration；復用 TaskStore 所有權與 lease，跨 DB 使用 outbox。
  - 驗收：身份偽造、空 conversation_id 合併、金鑰輪替失去 owner、跨帳號讀寫、重啟遺失進度測試均拒絕／正確處理。
- [ ] **W03 — 接入建立與持續使用入口**（W02）
  - 先接 project_scaffold、一般新程式目錄建立；再接工具、Skill、子代理、coordinator、受管理排程／附件及 worktree dispatch。
  - 交付：先登記再建檔、部分建立失敗狀態、跨 session 引用入口。
  - 驗收：同目的沿用既有目錄；未知 host 副作用不自動接管；未登記跨 session 寫入被拒絕。
- [x] **W04 — 實作路徑／file identity 與 manifest 保護**（W02）
  - Windows／POSIX 對應安全列舉、連結拒絕、根與重疊檢查、容量／時間上限，鎖定服務管理封存根。
  - 驗收：workspace 根、祖先、其他使用者、junction、symlink、hard link、置換及跨 volume 不發生越界副作用；失敗原目錄保留。
- [x] **W05 — 列出、接管與刪除預覽**（W03、W04）
  - 只讀清冊、清理清單與衝突說明；既有目錄 inventory＋人工接管，不自動刪除。
  - 驗收：`go_hello_world`／`hello_world` 等既有未知目錄預設 unmanaged；預覽不改檔、不顯示憑證內容。

M1 交付門檻：只讀清冊與預覽可用，正式使用者資料沒有破壞操作；建立相應程式／測試 commit。

M1 實作進度：已建立 `workspace_lifecycle/` version 1 registry、canonical conversation／legacy device 身份、建立前登記、跨 session attach／retain、未知目錄 inventory、exact-list snapshot 預覽與人類 `/workspace` 入口。新 scaffold／write/edit/delete 與共用政策接入；其餘入口、原生 handle 保護與後續 lifecycle 仍列於未完成項。首次相關測試 45 passed、6 subtests passed；不觸碰正式 workspace 目錄。

## M2 — 可還原封存

- [x] **W06 — 精確清單批准、lease 與冪等操作**（W05）
  - 復用 ApprovalStore，綁定身份、目標 session、清單摘要、generation 與內容版本；操作使用 fence。
  - 驗收：模型不能自批、參數篡改、過期、重複消費、並發引用與租約取代均不執行移動。
- [x] **W07 — 原子封存／還原與崩潰恢復**（W06）
  - 同 volume rename、服務隔離目錄、manifest；原位置衝突拒絕覆寫，partial/unknown 狀態可檢視。
  - 驗收：移動前／後各斷點可恢復；內容一致；session 存在時不因時間或容量壓力自動刪封存；跨磁碟不使用不完整 copy-delete。
- [x] **W08 — 索引、驗收、Git 與工具可見性整合**（W07）
  - 封存區排除於 `.gitignore`、code index、snapshot、模型搜尋、artifact 發布；封存後重建活躍引用。
  - 驗收：封存副本不造成驗收誤判、Git staging 混入或模型重讀；還原後索引一致。

M2 交付門檻：封存／還原在原生 Windows 與本機 fixture 通過，永久 purge 尚未開放；建立对应 commit。

M2 實作：原生 handle、same-volume rename、ACL 保護、extended/virtualized path、精確清單批准、一次性 consume、操作 lease、移動後重新鎖定驗證、原地衝突拒絕、崩潰後續跑與 partial purge 原 manifest 子集保護。已新增原生 Windows 封存／還原、占用、junction、crash-after-move 與新增檔案不刪除測試；封存區排除於一般工具、Git、snapshot 與 code index。session 刪除的 transport／資料清除與 UI 接入屬 M3，尚未宣告可用。

## M3 — 刪除 session 與永久清理

- [x] **W09 — session 刪除協調器與操作權限**（W06、W08）
  - 人類來源的 preview／confirm／status；同一批准涵蓋對話及精確 temporary 項目。
  - 持久化 tombstone／outbox，阻擋新指令與新引用；retained 轉使用者所有、shared 解除引用；舊或不確定目錄不刪。
  - 驗收：跨 owner、模型偽造人類確認、重播及最後一個引用刪除競態測試通過。
- [x] **W10 — 任務、程序、排程與待送資料停止流程**（W09）
  - 先處理明確取消批准，再等待 task／lease／coordinator／子程序／排程停止，撤銷待批准和待送事件。
  - 驗收：不強殺未登記外部程序；占用／停止不確定時 blocked；遲到結果與離線舊請求不能重新建立已刪 session 資料。
- [x] **W11 — 隔離後永久清理與逐項恢復**（W10）
  - 僅處理已批准 frozen 清單；active 先隔離、archived 經核對後 purge；設容量、併發、掃描時間與重試上限。
  - 驗收：部分成功不誤報全部成功；剩餘檔案可定位與重試；永久刪除不可還原的事實呈現在 UI；無自動時間淘汰。
- [x] **W12 — MyAI 對話刪除入口與可靠協定**（W09–W11）
  - 在既有 desktop_assistant 加「刪除對話」、保留程式選項、preview／確認／刪除中／blocked／完成與狀態查詢。
  - 能力協商未通過時停用新功能；不把本機刪訊息當後端刪 session；確保重連補送不復活對話。
  - 驗收：真實 localhost WS fixture、兩個裝置、重複點擊、斷線／重連與後端版本不支援案例通過；MyAI repo 獨立 commit。
- [ ] **W13 — 其他人類通道與最小稽核**（W12）
  - CLI／Telegram／管理 UI 共用服務接口；完成清理後清除 checkpoint、工具輸入輸出、原始 episodic 對話與檢索索引；共用記憶僅解除已刪原始內容引用，不全域刪除。記錄摘要、操作者、結果、阻擋原因，不留已刪內容全文。
  - 驗收：不同 transport 的相同政策與冪等結果；不因「清理」自動發送未授權訊息；fixture 與正式通道結果分開記錄。

M3 交付門檻：以獨立可丟棄 session／目錄完成首次永久清理，不以正式 workspace 資料測試；PythonAI、MyAI 相應 commit 與驗證報告齊備。

## M4 — 上線與操作維護

- [x] **W14 — Migration dry-run、分段啟用與 rollback 手冊**（W13）
  - 預設既有資料 unmanaged；先清冊預覽，再封存還原，最後 session purge；feature flag 可停用新破壞操作而保留進度查詢／恢復。
  - 驗收：停機／重啟不重複刪除；資料 schema rollback 方案清楚；容量滿不擅自清理；使用者可在刪 session 前保留／匯出程式。
- [ ] **W15 — 完整測試、原生驗證與正式流程驗收**（W14）
  - 測試矩陣：owner／角色、跨 session shared、retained、unmanaged、active lease、排程、占用、重播、內容變動、連結／TOCTOU、原子移動、崩潰、purge 部分成功、Git／索引排除與容量限制。
  - 驗收：相關與完整必要測試、`git diff --check` 全部通過；Windows 原生與本機 WS 分開報告；真正 MyAI 點擊與外部通道只按已實測範圍標示通過。每個完成變更建立 commit。

總體完成標準：使用者刪除 session 的批准可追溯至實際清理項目；只有自身獨占 temporary 資料消失，其他資料保留；失敗可檢視與安全續跑；session 存在時沒有時間到期刪除。

## 2026-10-15 實作與驗收範圍

M3 的 MyAI／WebSocket 與 CLI 主流程已接入：canonical session 跨裝置歸屬、own-session 窄範圍人工確認、exact-list／generation／manifest 批准、原生隔離與永久清理、outbox 資料清除、持久化 tombstone、防止遲到 task／memory 寫入、共用與 retained 保留。MyAI 保存不確定操作，重連查詢原 plan；UI 提供逐項保留、取消、確認與 blocked 續跑。不同 token 分區用後端 instance／canonical session 清除本機歷史。

W03 保留未勾：project_scaffold、write/edit 及共用工具路徑政策、排程／委派 owner 已接入；既有 worktree 使用其原流程，未知 host／Skill 外部副作用不自動納管，部分建立內容失敗的獨立狀態仍待擴充。W13 保留未勾：CLI／WS 有資料清除 consumer；Telegram／communication 尚無傳輸原始資料 consumer，session-delete 明確拒絕，不能誤報全通道完成。W10 的停止流程採先明確中斷並等待 task／lease／worker 結束；清理本身不強殺外部程序。

W14 手冊位於模組 README。W15 本機驗證完成後記錄結果；外部 provider、正式 Telegram、乾淨 Windows／實際發布 exe 及正式使用者手動流程不計入 fixture 通過。現有 `workspace/go_hello_world`／`hello_world` 持續 unmanaged，未刪除、未納入提交。

最終本機驗證（2026-10-15，原始碼固定後）：`python -m pytest tests agent_execution/tests -q`：718 passed、7 skipped、95 subtests passed，613.44 秒；skipped 沿用各測試的外部／環境條件，不計為通過。生命週期專項 24 passed，涵蓋單字元巢狀目錄、保留封存跨新 session 還原、late memory／checkpoint 拒絕、排程變更／live lease、同名重建、跨 owner 根範圍、崩潰與 partial purge。語法與 `git diff --check` 通過。

MyAI：全套 unittest 97 tests OK（86.923 秒）；已重建 `dist/MyAI/MyAI.exe`，另行驗證封裝版 health／WS 認證／正常退出（1 test OK），檢查實際畫面包含刪除對話按鈕。桌面 commit `619629a`。未重啟正式後端、未刪正式 session；W15 的正式／乾淨機驗收仍未勾選。
