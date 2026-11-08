# R2 驗證紀錄

變更前 HEAD：`732006f`；開始時工作目錄乾淨。所有 Python 測試／驗證均使用本專案 `venv/Scripts/python.exe`。變更限原生派工程式庫及其必要的 TaskStore／ExecutionContext／server 注入點，未遷移使用者 DB、啟動正式 worker、呼叫付費 provider 或推送遠端。

## 實作與測試範圍

schema v4 及 v3 意圖資料保留；可信 admission 精確綁定與 generic claim 拒絕；SQLite 多 worker 競爭、owner／全域容量、FIFO、跨員工並行；自動路由原因與冪等；acceptance validation／路徑／執行測試限制；核心與 outbox 容量 rollback；pause／disable／reassign／主管不變條件；lease 過期、timeout、start hang／shutdown failure；TaskStore 取消傳播、執行中取消、晚到 generation／session／turn／產物描述 fencing；terminal payload 不覆寫 core 驗證；v4 清理與未知 runtime 阻擋。

測試包含專案 venv 子程序在 claim commit 後 `os._exit(23)`，確認重新啟動的 dispatcher 不 replay 同一個 task。原生整合測試執行實際 server loop／model step 與預算 wrapper，使用 mock provider／evaluator；另外確認 model 及歷史隔離、共享 model／evaluator 預算、重啟不提高 request 額度、不誤用 caller 的其他 task budget，及 Temporal rollout 不改變已 admission 的原生 backend。

## 驗證命令與結果

最終完整回歸 **1853 passed、89 skipped、118 subtests passed**，無失敗，耗時 **2141.17 秒（35:41）**。報告為 `temp/office-r2-verify.log` 與 `temp/office-r2-verify.xml`（Git 忽略的本機驗證產物）。首項失敗即停止設定未觸發，已跑完全部案例。

新增 **58 項派工測試全部通過**，包含提交者 ceiling、跨 worker 與重新指派不可擴權、舊 intent 採空工具的保護；整個 Office 測試為 **280 passed**。`pip check` 通過，420 個 Python 檔案 AST 語法檢查通過，`git diff --check` 通過。

89 項跳過分別是 Temporal 真實服務 75、Chromium 5、Docker 3、真實 provider opt-in 5、缺少選用 harness2 1；不代表這些外部環境已驗證。本輪有 3 項 warning：Starlette／AnyIO 棄用 1 項、Tkinter 物件回收的主執行緒警告 2 項，均未造成測試失敗。

初輪完整回歸為 1846 passed、89 skipped、118 subtests passed，另有兩項測試支援問題：companion 暫存清理遇到 Windows SQLite 分享鎖，coordinator 的 coder 已驗收成功但 reviewer 流程超出 40 秒等待。後續將清理限制為僅分享鎖／其衍生錯誤可重試最多 2 秒（永久鎖與其他錯誤仍失敗，新增 3 項測試），真實子程序整合等待改為 90 秒、低於既有 worker 120 秒期限；保留原驗收與 fencing 斷言。相關重跑 **30 passed、4 subtests passed**（301.07 秒），再執行最終完整回歸。

下一輪原兩項案例通過，僅 Go scaffold 實際編譯案例失敗（1850 passed、89 skipped、118 subtests passed）；工具預設 30 秒，該案例耗時 33.61 秒，單獨重跑通過。測試支援將真實 Go 編譯整合等待獨立設為 120 秒，正式工具仍為 30 秒並新增直接驗證；Python 測試等待不擴大，也保留實際 Go 命令與通過／失敗證據斷言。相關 **21 passed**（27.20 秒），再跑首項失敗即停止的完整回歸；全數成功時仍會跑完所有案例。

後續早停定位到取消整合測試另有 30 秒啟動等待，已統一為 90 秒，仍驗證取消後實際 PID 消失與不可重新派工。fixture 明確以 `sys.executable` 啟動子程序，經原有 trusted asset／policy 路徑只授權該執行檔；新增實際 interpreter marker 比對專案 venv，避免 Windows bare-python 搜尋到系統 Python。該整合案例 **1 passed**（24.53 秒）。

```powershell
.\venv\Scripts\python.exe -m pytest agent_execution/office/tests agent_execution/tests/test_tasks.py agent_execution/tests/test_budgets.py -q
.\venv\Scripts\python.exe -u -m pytest -q -x -ra --tb=short --durations=10 --junitxml=temp/office-r2-verify.xml
.\venv\Scripts\python.exe -m pip check
git diff --check
```

R2 不驗證真實模型／外部 Codex／Claude、正式長時間多程序服務或強制殺死 SDK thread。原生寫入／完整逐次審批留 R3；HTTP／WS 及自動 worker 留 R4。未宣告 durable resume 或跨任務員工記憶已完成。
