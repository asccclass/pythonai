# ADR 0003：持久派工意圖與跨 DB 對帳（R1.4）

狀態：接受。範圍是資料持久化、request ID 冪等與 SQLite 投遞，不包含 R2 runner、R3 工具批准續行或 R4 transport API。

## 資料與任務邊界

Office schema 升級為 v3，沿用明確離線備份與同一 TaskStore transaction 的 migration；v1／v2 SQL 定義保留。新增：

| 資料表 | 契約 |
| --- | --- |
| `office_assignments` | owner／ID、request ID、完整 request fingerprint、保留 task ID、staff／team revision、owner 內 queue sequence、JSON snapshot、state。 |
| `office_operations` | owner／assignment／request ID、精確 tool arguments fingerprint、不可變 intent snapshot、審批觀察狀態。 |
| `office_outbox` | 固定 ID、event／approval、payload fingerprint、pending／sending／delivered／uncertain／cancel_pending／cancelled、sender token／lease、對帳次數及固定錯誤碼。 |
| `office_targets` | 每種目標固定的 DB 路徑與 instance UUID，拒絕默默改投其他資料庫。 |

Assignment 與初始 event outbox 同一交易建立；operation 與 approval outbox 同一交易建立。相同 owner/request ID 重送返回原紀錄，JSON key 排序不影響 fingerprint；不同內容即 conflict。Operation request ID 的作用域還包含 assignment。重播先查既有紀錄再檢查新資料容量，故滿額時仍可安全重送原 request。

R1.4 只保留 `office:<assignment ID>` task ID，不建立可執行的 core TaskStore task、不領模型預算、不啟動工具。`TaskStore.claim` 拒絕保留 ID，避免舊入口繞過員工設定。R2 必須加入可信 office admission 與任務／預算／lease 綁定才能建立 runner task，不能直接移除這個 guard。

Queued／uncertain assignment 以及尚未完成取消補償的 outbox 會阻止變更其 staff／team 設定。已提交的內容不因 profile 修改而重寫。Criteria 目前是有界 JSON snapshot；R2 建立 core task 時仍須執行既有驗收契約驗證，不能把這個 snapshot 當成驗證通過。

## 目標端收據與流程

三個 DB 必須為不同的私有檔案，位於目前與所有 project workspace 外。EventStore／ApprovalStore 在各自 DB 加入 `office_bridge_meta`、`office_bridge_origin`、`office_bridge_owners`、`office_bridge_receipts`。Origin 反向固定 source TaskStore 路徑，不允許兩個分離的 TaskStore 共用 office targets；同一 TaskStore 的多個程序則仍由 sender token／lease 協調。

1. Source 驗證 owner、schema、目標路徑及 instance；目標先保留 owner 的雜湊 fence。
2. Source 保存 sender token／lease 與 sending，再離開 source transaction。
3. 目標 publication 和固定 ID 收據在同一 target transaction 提交。EventStore 新增 trusted `append_transaction` 以重用原有去重、容量及序號邏輯；approval pending row、audit 與 receipt 也同一交易提交。
4. Source 對帳 receipt，再以 sender token 比對確認 ACK；過期或撤銷的 sender 不能發布晚到 ACK。

Event 使用既有 durable event key；bridge receipt 在 ACK／TTL 清除 event body 後仍保留。Approval ID／原始 expiry 固定，不因程序重啟、拒絕、到期或 request 重送建立新 grant。跨 DB 沒有單一 transaction，也不承諾任意外部副作用 exactly-once。

Source sending 中斷後，只探查 receipt：有收據則接回原紀錄；沒有收據即 uncertain，不自動重送。不以「沒有看到結果」推定「沒有執行」。已觀察到 consumed、但沒有本階段不存在的執行收據時，operation／assignment 轉 uncertain，留待人工核對；reconciler 不批准、不消耗、不執行工具。

對帳最多處理 100 筆，預設 20。取消優先，其餘依對帳次數與建立順序輪替，避免舊 pending approval 輪詢持續擋住新意圖。錯誤只存固定碼，不複製 exception 中的路徑／參數。

## Namespace 與 UI

每次操作以當前可信 execution scope 建立完整 owner；提交、準備 operation、取消、對帳需 admin，讀取限本人。Target event owner 為完整 `OwnerScope.key`，approval 為 `(owner.identity, owner.key)`。這與既有 device 三元組／legacy session 是不同 namespace，不自動合併 API／WS 身份，也尚未接到既有 UI。R4 應透過可信 handler 使用這個 namespace，不能讓 client 任意指定 owner。

Operation 只能使用 snapshot 中且目前 caller ceiling 允許的已註冊工具，所有工具意圖先建立 pending approval。Nested `confirm` 等參數保留在 envelope，完整 bytes 納入 fingerprint；這些資料與 ready 狀態本身都不是工具執行授權。R3 必須重新檢查 role、workspace、參數、generation、預算及一次性批准。

## 取消、清理及還原

未嘗試送出的 outbox 可直接 cancelled；曾送出或結果不明的需 target cancellation receipt。取消會撤銷舊 sender token；目標取消收據阻止晚到 publisher 重建 grant。已 consumed 仍轉 uncertain，不能偽裝取消成功。Target cancellation 可冪等重試；普通 missing-receipt publication 不自動重試。

未結束 assignment／delivery 會阻擋 session-delete。清理經既有 outbox finalize，在兩個目標先保留 deleted owner hash fence、刪除該 owner 的 office feed／approval／receipts，再於 source transaction 按 outbox→operation→assignment→member→team／staff 的依賴順序清理。即使舊 publisher 保留 payload，deleted fence 也會拒絕它；其他 owner 與原本 session-delete 的授權 audit 保留。

Receipt 不按時間自動淘汰；目標 hash fence 在 owner 刪除後也保留。Target DB 遺失／instance 改變／owner fence 遺失時拒絕對帳或清理，不自動重建 DB 當作未執行。任意單 DB 回復舊備份不是安全的自動恢復協定：需停機核對三方收據與 unknown effects，再依既有還原流程處理。

## 容量

`PersistenceLimits` 設定有型別／範圍檢查，可用 `AGENT_OFFICE_PERSIST_<FIELD>` 環境變數覆寫；重新建構服務後生效。Queue 使用原有 `AGENT_OFFICE_QUEUE_LIMIT`（預設每 owner 100 筆非終態）。

| field | 預設 |
| --- | --- |
| `ASSIGNMENTS`／`OWNER_ASSIGNMENTS` | 全域 10000／每 owner 1000，含終態歷史。 |
| `OPERATIONS`／`OUTBOX` | 全域 50000／100000，含終態歷史。 |
| `STORAGE_BYTES` | 三張表 payload 合計 128 MiB；不是 SQLite 檔案／index／WAL 的磁碟 quota。 |
| `PAYLOAD_BYTES` | 單一 canonical JSON 64 KiB、最多 32 層；只接受有限 JSON 數字與字串 key。 |
| `TARGET_RECEIPTS` | 每目標最多 100000 收據及 100000 owner fences，刪除 fence 仍計數。 |
| `DELIVERY_LEASE_SECONDS` | sender lease 30 秒。 |

Admission 在保留寫入鎖中先檢查所有 row／byte 上限，再一次插入；超額不留下半張 assignment、operation 或 outbox。Target 容量也在 publication transaction 中檢查，失敗不留下沒有 receipt 的 grant／event。容量不足時保留背壓或待補償狀態，不丟棄冪等資料換取空間。
