# ADR 0002：Office 目錄資料、遷移與 CRUD

狀態：接受。範圍：R1.2／R1.3；不含 R1.4 派工／operation／outbox 表或 R4 服務介面。

## 儲存與遷移

`OfficeStore` 接受既有 TaskStore；import／constructor 不安裝 office schema。核心 TaskStore 若需升級，沿用既有 operations backup migration，再交給 OfficeStore。

Office schema 使用自己的單列 `office_schema`，不修改 `PRAGMA user_version` 或其他模組的 schema metadata。v1 安裝 staff／teams 與名稱唯一索引，v2 加入 members、單主管索引與成員反查索引；全部 SQL 使用個別 execute，在同一 `BEGIN IMMEDIATE` transaction 完成。預先取得保留寫入鎖，避免備份與 DDL 之間其他 writer 改資料；讀取仍可進行。實際服務停機由可信 operator 的 `offline=True` 聲明，這個參數不會代為停止程序。

每次有效升級先使用既有 SQLite backup API、manifest 與 SHA-256 備份，再寫 schema；備份失敗不寫 schema，DDL／驗證失敗全部 rollback，留下可驗證的備份。current version no-op 不產生新備份。未知版本、未登記的 office 物件、遺失／更動的 owned SQL 定義一律拒絕；空 schema 才能視為 v0。

後續增加或變更 owned schema 時必須新增版本與對應 migration／相容檢查；已發布的 SQL 定義不可直接改寫來冒充舊版本。新增 owner 資料表也必須同步更新 session erasure 的依賴順序與測試。

完整備份可能含私人角色與對話；備份目錄必須在目前 workspace 及所有 project mappings 的 workspace 外，不能把整個資料庫交給另一個 project 讀取。沿用現有 backup 的 256 MiB 單 DB 上限及新目錄 restore；不自動降版、不覆蓋既有 DB。

## 資料與授權

- `office_staff`：owner／ID 複合主鍵、顯示名稱／去重 key、provider／model、工具 JSON、logical workspace project、enabled／deleted、revision、updated。
- `office_teams`：owner／ID 複合主鍵、名稱／去重 key、enabled／deleted、revision、updated。
- `office_members`：owner／team／staff 複合主鍵，role 為 member／lead；兩個外鍵都包含 owner，禁止跨 owner 指派。partial unique index 限制同團隊最多一位主管。

`OfficeDirectory` 的操作不接受 owner 參數，每次從可信 execution scope 讀取完整 source／identity／project／session。讀取需有效 viewer／operator／admin role，修改需 admin；團隊 lead 不提升應用角色。底層 store 是可信程式庫，不是 OS sandbox 或公開端點。

Workspace 不接受 client path，只保留 owner project 作為邏輯參照，每次 CRUD 都解析可信 project mapping 並檢查目錄存在。mapping 更新後不綁死舊絕對路徑；未來 runtime 必須將實際 resolved workspace 納入 session generation 與恢復核對。工具集合預設空，最多 64 項，只接受已註冊且在當前 allowed-tools ceiling 內的工具；儲存設定本身不授予執行權限。

Public DTO 只回傳明確 allowlist 欄位，不包含 owner key、檔案路徑、provider 憑證或 SQLite 內部資料。模型只接受有界 identifier，設定不接受 api_key／token／workspace path 欄位。所有資源查詢均綁定 owner；不存在與他人的 ID 同樣回報 OfficeNotFound。

## Revision、成員與刪除

所有修改在保留寫入鎖內重新讀取並比較 revision；更新／刪除無 unconditional 模式。成員增刪、角色調整、主管移轉會遞增 team revision，因而使舊版整隊修改失效。lead 移轉的降任／提升與 revision 更新為單一交易，失敗不留下無主管狀態。

團隊預設停用；啟用需有 enabled 且未刪除的 lead。現任 enabled team lead 不可直接停用、移除或降任，必須先移轉主管或停用團隊。一般停用員工可保留成員身分，未來 dispatcher 必須排除停用目標。

員工刪除需先停用、移除全部 membership；團隊刪除需先停用、清空 membership。採 soft delete 保留 ID／revision，避免未來歷史任務失去參照；有效名稱可重用，舊 ID 不可讀寫。固定每 owner 200 staff／100 teams、每 team 64 members，tombstone 計入容量，直到正式會話資料抹除。

目前沒有 assignment／runtime 可執行，因此此階段只有設定與成員關係 guard。R1.4／R2 必須在新增執行之前擴充 active／uncertain task guard，不可只靠這次的 enabled flag 就啟動引擎。

## 會話生命週期

`OfficeDirectory` 明確建構時確認 schema 並準備同 DB 的 LifecycleStore。有效修改在同一交易呼叫 ensure_session，登記既有會話所有權；任何 deleting／deleted owner 讀寫均拒絕。

既有 session-delete 先完成原生資源處理，再由 transport 消費 erasure outbox；office scrub 加在這個 finalize transaction，先 members 再 teams／staff，與 owner tombstone 一起提交。其他 owner 與外部備份不受此清理影響；未知 schema 阻止 finalize，避免宣稱已清除卻遺留私人目錄資料。此 hook 不建立 office schema，舊模式無 office 表時直接略過。

實測項目包括升級／備份還原、未知／部分 schema、DDL 與備份故障、同 owner CAS 競爭、跨 owner FK、主管移轉回滾，以及實際兩段 session-delete。完整驗證紀錄見 [R1.2／R1.3 紀錄](directory-validation.md)。
