# 記憶工作租約與恢復

需求：每筆背景記憶工作必須有獨立的隨機租約、程序 PID／建立時間／主機、
到期時間及心跳。預設 `MEMORY_JOB_LEASE_SECONDS=120` 秒，每三分之一租期續租。
完成、延後及失敗更新必須符合目前租約且尚未到期；其他 store／thread 不能代為完成。
工作執行中即使遠端 API 阻塞，心跳仍獨立執行。

租約到期不代表程序停止。原程序仍存在或無法確認身份時，保留工作與檔案，
拒絕接管及清理。PID 建立時間不同代表原程序已停止，避免 PID 重用誤判。
巡檢發現「租約到期且原程序已停止」才進行恢復：以 SQLite backup API
建立完整、一致且通過 integrity_check 的備份，再以同一資料庫寫入交易將工作標記 failed，
並保存工作 ID、舊 token、備份路徑、原因與時間的恢復紀錄。
可能已寫入的記憶資料保留，不自動重跑、回滾或刪除。
備份儲存在該 memory.db 同層 `memory-job-backups/`，包含原始資料，應依原資料庫權限保護。

沒有程序資料的舊 processing 工作不會因重啟自動恢復。
Windows 維護時先停止所有 PythonAI 服務與原生 worker，確認原服務 PID 已停止，
再執行（請替換資料庫路徑及 PID）：

```powershell
.\venv\Scripts\python.exe -m agent_execution.memory_jobs inspect --database C:\path\memory.db
.\venv\Scripts\python.exe -m agent_execution.memory_jobs recover --database C:\path\memory.db --legacy --stopped-pid 1234
```

可重複指定 `--stopped-pid`。維護工具會再次查詢程序，拒絕仍有 PythonAI worker、
無法確認已停止的 PID、缺乏程序資訊、備份失敗等情況。
不要把舊工作直接 SQL 改成 pending，也不要移除刪除檢查。
恢復成功後重啟後端；處理失敗工作不代表原請求已完成，需要時另送新請求。

刪除受阻會回傳 `blockers`（工作 ID、類型、episode、狀態、程序狀態與租約時間）及
`retryable`。仍在執行的工作可等待後重試；舊無擁有者、失效或身份不明的工作提示維護恢復。
WebSocket、HTTP 與刪除預覽共用此資訊；檔案保護條件保留。

驗證：`venv\Scripts\python.exe -m pytest agent_execution/memory_jobs/tests tests/test_memory_worker.py`
涵蓋阻塞 API 心跳、真實子程序中斷、過期／跨擁有者 fencing、PID 重用、未知程序、
舊 schema、停服與備份檢查。只修改程式碼的 rollback 可回退 commit；新增欄位應保留。
舊版 worker 不理解租約，回退前須先停服並核對／處理 processing 工作。
