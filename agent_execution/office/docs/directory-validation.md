# R1.2／R1.3 實作與驗證

變更前 HEAD：`cf7ce78`；開始時工作目錄乾淨。Python 任務均使用本專案 `venv/Scripts/python.exe`。本次只在測試的獨立 DB／workspace 執行 migration 與 CRUD，未自動遷移既有服務資料庫或啟動派工服務。

實作包含 `models.py`、`store.py`、`directory.py`，並在既有 workspace lifecycle erasure finalize 加入同 owner 的 office metadata 清理。資料表及索引全部放在 TaskStore DB，沒有新增平行 task／budget／engine runner。

## 相關回歸

```powershell
.\venv\Scripts\python.exe -m pytest agent_execution/office/tests agent_execution/tests/test_workspace_lifecycle.py agent_execution/tests/test_workspace_communication.py agent_execution/tests/test_companion_outputs.py agent_execution/tests/test_tasks.py agent_execution/tests/test_sessions.py -q --tb=short
```

結果：**233 passed、4 subtests passed**，108.38 秒。日誌：本機 `temp/office-r12-integration.log`（不入 Git）。其後另補跨 project 備份路徑測試；Office 目錄目前共有 169 個測試，其中 98 個為前階段測試，本階段新增 71 個，最終納入整套 pytest。

最終 office 獨立回歸：**169 passed**，26.92 秒；日誌 `temp/office-r12-targeted-final.log`、JUnit `temp/office-r12-office.xml`。語法檢查 409 個 Python 檔案通過、`pip check` 無破損相依，6 份文件的 33 個本機連結通過檢查。

涵蓋真實 SQLite backup／verify／restore、0→2／1→2 migration、升級 no-op、版本及 schema 不相容、備份及 DDL 故障、備份期間 writer fencing、owner 四個維度的讀寫隔離、角色與工具 ceiling、停用／刪除、團隊主管關係、revision 並行競爭、失敗移轉 rollback、容量及分頁、SQL-like 輸入，以及真實 session-delete 兩段處理。

首輪 lifecycle 測試只呼叫原生 confirm，尚未消費既有 erasure outbox，因此讀到保留資料；已補上與 HTTP／command handler 相同的 finalize 呼叫，未放寬抹除斷言。

## 完整回歸

所有原始碼／設定／說明完成後固定檔案，才執行整套 pytest，避免影響營運 benchmark 的儲存庫快照檢查。

```powershell
.\venv\Scripts\python.exe -m pytest -q --tb=short --junitxml=temp/office-r12-full.xml
.\venv\Scripts\python.exe -m agent_execution.operations syntax
.\venv\Scripts\python.exe -m pip check
git diff --cached --check
```

2026-11-08 最終結果：**1737 passed、89 skipped、118 subtests passed**，exit 0，1530.99 秒（25 分 30 秒），沒有 failure／error。Office JUnit 包含全部 **169 項**，沒有略過。日誌 `temp/office-r12-full.log`、JUnit `temp/office-r12-full.xml` 均為本機驗證產物，不入 Git。

89 項略過：75 項需真實 Temporal CLI／Service、5 項 Chromium opt-in、3 項 Docker opt-in、5 項真實 provider opt-in，以及 1 項缺少選用 `harness2` 模組。這些不算本次驗收通過；1 項既有 Starlette／AnyIO deprecation warning 未構成失敗。

語法檢查 409 個 tracked Python 檔案通過、相依檢查通過，最終文件與 diff check 通過。完成 R1.2／R1.3；R1.4 assignment／operation／outbox、R2 runtime／派工及 R4 HTTP／WS 介面仍依規劃續行。
