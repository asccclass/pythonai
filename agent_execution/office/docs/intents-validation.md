# R1.4 驗證紀錄

變更前 HEAD：`c000bfd`；開始時工作目錄乾淨。所有 Python 工作均使用本專案 `venv/Scripts/python.exe`。本次只在測試的隔離資料庫操作，未遷移既有服務 DB 或啟動實際 AI runner。

## 範圍

Office schema v3、`ledger.py`／`persistence.py`／`targets.py`／`reconcile.py`，並擴充保留 task ID guard、directory guard、EventStore transaction append 與會話刪除。原本各 DB 維持獨立，沒有建立平行模型／工具 runner。

測試涵蓋 owner 四維隔離、相同／不同 request 的並行冪等、v2 資料保留與升級失敗 rollback、row／payload／queue 容量、nested arguments fingerprint、原 expiry、ACK 後去重、遺失 ACK、DB path／instance 異動、目標收據破損、輪詢公平性、取消與批准消耗競爭、晚到 sender fencing，以及真實三 DB session-delete／其他 owner 保留。

另外用專案 venv 子程序 `os._exit(23)` 分別在 target transaction commit 前與 commit 後結束程序；前者沒有 grant／receipt 並保留 uncertain，後者可由原 receipt 接回，沒有建立第二筆 grant。測試不把模擬 prose 成功視為已執行工具。

## 相關測試

初輪既有 office：169 passed。新增跨 DB 與既有投遞／任務／清理的擴充回歸曾為 **270 passed、4 subtests passed**（118.73 秒）；其後加入公平性及更完整的升級／容量／取消測試，最終結果將補入下節。日誌均在本機 `temp/office-r14-*.log`，不入 Git。

擴充回歸最終為 **276 passed、4 subtests passed**（142.80 秒），見 `temp/office-r14-integration-final.log`／XML。最後再補 source origin 反向綁定測試，R1.4 核心 **53 passed**（48.09 秒），見 `temp/office-r14-core-final.log`／XML；全部將納入完整回歸。

## 完整回歸

所有程式與設定完成後固定來源再執行：

```powershell
.\venv\Scripts\python.exe -m pytest -q --tb=short --junitxml=temp/office-r14-full.xml
.\venv\Scripts\python.exe -m agent_execution.operations syntax
.\venv\Scripts\python.exe -m pip check
git diff --cached --check
```

2026-11-08 最終結果：**1790 passed、89 skipped、118 subtests passed**，exit 0，1514.96 秒（25 分 14 秒），沒有 failure／error。Office JUnit 包含全部 **222 項**，無略過；相較前階段新增 **53 項**。日誌 `temp/office-r14-full.log`、JUnit `temp/office-r14-full.xml` 為本機產物，不入 Git。

89 項略過：75 項真實 Temporal CLI／Service、5 項 Chromium opt-in、3 項 Docker opt-in、5 項真實 provider opt-in、1 項缺少選用 `harness2`。這些不算本次通過；1 項既有 Starlette／AnyIO deprecation warning 不構成失敗。

語法檢查 **415 個 tracked Python 檔案**通過、`pip check` 無破損相依；8 份文件的 37 個本機連結、code fence 及最終 diff check 通過。測試期間沒有更動 tracked files。R1.4 完成；R2 runner／budget admission、R3 工具批准續行與 R4 客戶端介面仍需後續實作。
