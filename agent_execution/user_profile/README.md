# 自動使用者 Profile 計畫與待辦

2026-10-16。使用者已確認：依登入身分隔離、同身分跨 session／專案共用；一般穩定事實自動記錄、健康／財務等敏感資訊須明確要求、憑證永不保存；更正／忘記立即生效，每輪載入原則，當次指示可覆寫偏好，不能提升權限。

## 依序實作與驗收

- [ ] P01（無依賴）建立獨立 user_profile 模組、版本化 SQLite 儲存及身分雜湊路徑。分類 fact/preference/instruction/note，保存最小來源摘錄、時間與修訂版本；上限限制及交易防止並行覆寫。驗收：不同身分隔離，同身分跨 session/project 及重啟可讀。
- [ ] P02（P01）獨立萃取契約：只分析當輪使用者純文字，模型輸出必須有原文證據；新增、修改、刪除以既有 ID 和版本核對。忽略引用、附件、暫時任務、第三人資料；敏感資訊須明確記住，密碼／token／key／憑證禁止送萃取或儲存。驗收：格式錯誤、低信心、幻覺、憑證、prompt injection 不寫入；萃取失敗不阻塞正常回答且不宣稱記錄成功。
- [ ] P03（P02）接入共用 Agent turn（CLI、Telegram polling、Web/API/WS 現有共用流程），寫入後當輪生效，每輪與恢復執行時重新載入；profile 原則獨立於 top-k 記憶搜尋。驗收：新 session 首輪、既有 session 更新、恢復執行均注入最新原則；不授予額外工具權限。
- [ ] P04（P03）移除啟動時共用 profile 提示詞，僅本機身分載入舊 workspace/profile.md；提供 /profile、/profile forget <id>、/profile clear 確定性入口。驗收：遠端無本機資料、查看刪除不呼叫模型、舊檔不覆寫；清 session 不等於刪使用者 profile。
- [ ] P05（P01–P04）模組內測試及既有整合回歸、完整 pytest、diff check；更新實際證據與限制，僅 stage 本次檔案並 commit。驗收：本次與既有測試全過；區分離線假模型測試與真 provider／裝置驗證。

## 邊界與運作

Profile 是使用者偏好及規則資料，不能覆寫系統／AGENTS／權限。單次明確指示優先於過去偏好。不同平台身分不自動合併。沒有主動推播或連線問候；連線後第一輪模型執行前載入，因此沒有額外登入延遲。

SQLite 位於 tenant storage 的 identity 專用 profile 位置，不隨 project 或 session 改變，不寫 Git。刪除項目不保留舊值歷史；來源只保存經過檢查的最小證據，非整段聊天。/profile clear 不清除既有其他記憶系統或本機手動 profile.md；一般聊天中的舊記憶並非本功能的儲存來源。既有 profile.md 為本機手動維護相容資料。

萃取使用現有 provider 與受治理的 client，每個一般使用者 turn 至多一次額外請求（10 秒 timeout、零自動重試），失敗繼續聊天並提示狀態。沒有透過語義搜尋回填其他人／舊對話資料。自然語言判斷受模型能力限制，查看與刪除命令提供確定性管理。
