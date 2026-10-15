# 自動使用者 Profile 計畫與待辦

2026-10-16。使用者已確認：依登入身分隔離、同身分跨 session／專案共用；一般穩定事實自動記錄、健康／財務等敏感資訊須明確要求、憑證永不保存；更正／忘記立即生效，每輪載入原則，當次指示可覆寫偏好，不能提升權限。

## 依序實作與驗收

- [x] P01（無依賴）建立獨立 user_profile 模組、版本化 SQLite 儲存及身分雜湊路徑。分類 fact/preference/instruction/note，保存最小來源摘錄、時間與修訂版本；上限限制及交易防止並行覆寫。驗收：不同身分隔離，同身分跨 session/project 及重啟可讀。
- [x] P02（P01）獨立萃取契約：只分析當輪使用者純文字，模型輸出必須有原文證據；新增、修改、刪除以既有 ID 和版本核對。忽略引用、附件、暫時任務、第三人資料；敏感資訊須明確記住，密碼／token／key／憑證禁止送萃取或儲存。驗收：格式錯誤／無原文證據不寫入，低信心／憑證排除，profile 不提升權限；萃取失敗不阻塞正常回答並向回答流程傳遞未儲存狀態。自然語言語意與非結構化引用仍依賴模型判斷，無法以原文子字串檢查保證所有推論正確。
- [x] P03（P02）接入共用 Agent turn（CLI、Telegram polling、Web/API/WS 現有共用流程），寫入後當輪生效，每輪與恢復執行時重新載入；profile 原則獨立於 top-k 記憶搜尋。驗收：新 session 首輪、既有 session 更新、恢復執行均注入最新原則；不授予額外工具權限。ask_user 人工回覆（含附帶附件）可萃取，工具／附件內容不萃取。
- [x] P04（P03）移除啟動時共用 profile 提示詞，僅本機身分載入舊 workspace/profile.md；提供 /profile、/profile forget <id>、/profile clear 確定性入口。驗收：遠端無本機資料、查看刪除不呼叫模型、舊檔不覆寫；清 session 不等於刪使用者 profile。
- [x] P05（P01–P04）模組內測試及既有整合回歸、完整 pytest、diff check；更新實際證據與限制，僅 stage 本次檔案並 commit。驗收：本次與既有測試全過；區分離線假模型測試與真 provider／裝置驗證。

## 邊界與運作

Profile 是使用者偏好及規則資料，不能覆寫系統／AGENTS／權限。單次明確指示優先於過去偏好。不同平台身分不自動合併。沒有主動推播或連線問候；連線後第一輪模型執行前載入，因此沒有額外登入延遲。

SQLite 位於 tenant storage 的 identity 專用 profile 位置，不隨 project 或 session 改變，不寫 Git。刪除項目不保留舊值歷史；來源只保存經過檢查的最小證據，非整段聊天。/profile clear 不清除既有其他記憶系統或本機手動 profile.md；一般聊天中的舊記憶並非本功能的儲存來源。既有 profile.md 為本機手動維護相容資料。

萃取使用現有 provider 與受治理的 client，每個一般使用者 turn 至多一次額外請求（10 秒 timeout、零自動重試），失敗繼續聊天並提示狀態。沒有透過語義搜尋回填其他人／舊對話資料。自然語言判斷受模型能力限制，查看與刪除命令提供確定性管理。

## 驗收紀錄（2026-10-16）

- 定稿後完整執行：`venv\Scripts\python.exe -m pytest -q --tb=short`，**757 passed、8 skipped、95 subtests passed**，513.49 秒；一則既有 Starlette/AnyIO deprecation warning。輸出保存在本機 `temp/user-profile-final-pytest.log`，不納入 Git。
- 新增 profile 測試 **26 passed**；共用 CLI／session／任務流程專項 **82 passed、4 subtests passed**（其後新增的拒絕保存案例也包含在最終完整測試）；預算專項 **26 passed**。compileall 及 diff check 通過，profile.db 與 WAL 已確認被 Git 忽略。
- 真實設定 provider：以合成英文及繁體中文訊息，分別萃取姓名 fact 與回覆 instruction 並通過原文證據驗證；各 2 筆，未寫入正式使用者 profile。發現 provider 會回傳 fenced JSON，已支援並加入測試。
- 初次全套的三個失敗為 Windows 暫存 DB 占用、測試期間本人編輯檔案導致 benchmark 來源異動檢查，以及 Go 範本驗證。三項獨立重跑全過；停止編輯後再跑全套得到上述無失敗結果，未為此改動無關功能。
- 真實 Telegram／WebSocket 裝置斷線重連及服務部署未實測；本次是來源碼、原生 fixture、通訊整合回歸及真 provider 萃取驗證。重新啟動既有服務後使用新功能。
- 使用者原有 workspace lifecycle／排程／傳輸等未提交變更保留；本次不提交它們。
