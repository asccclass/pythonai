# ADR 0004：原生 admission、FIFO 與執行 fencing

狀態：R2 實作；接續 ADR 0003。Office schema v4 增加 `office_executions`、員工執行唯一索引、容量索引及 `office_staff_controls`。v1–v3 的 SQL 不變，依既有停機、離線備份、增量 migration 流程升級；import 與 constructor 不自動升級。

## 可信邊界

`OfficeLedger.submit()` 只保留 assignment／task ID；`OfficeDispatcher.claim()` 必須在既有可信 admin／owner scope 且 `OfficeSettings.enabled=True` 中呼叫。它在同一 TaskStore write transaction 檢查 lifecycle、員工 revision／provider／啟用、團隊綁定、根 task 容量與 acceptance contract，再建立 pending core task、execution lease 與 active assignment。預建 core task 固定 SQLite backend，既有 Temporal rollout 不會把此任務轉交另一引擎。

內部 admission 是 ContextVar authority，綁定實際 DB、owner 四維、assignment、task、staff、generation、隨機 token。TaskStore claim 每次查驗持久 lease 與期限；單純提供 owner／保留 task ID／RuntimeStartRequest 不能啟動工作。這是應用程式信任邊界，並非對能讀寫資料庫或注入 Python 的管理者提供 sandbox。

`ExecutionContext.task_database` 讓 TaskStore／TaskBudget／lifecycle 在同一可信 DB 上工作，不修改 process environment。adapter 再檢查 workspace project mapping、凍結的 model／tools、request ID／goal。server loop 接受明確 `client_factory`／`model`，不修改全域模型；省略新參數時維持舊行為。

assignment 同時保存提交者的 tool ceiling；worker 的較寬權限不能擴大原限制，重新指派也只能繼續縮限。沒有此欄位的既有 v3 intent 採空工具集合；需要工具時須取消並在當下可信 scope 重新提交，不猜測原提交者權限。operation admission 及 native start 也核對相同上限。

## 排程及背壓

明確指派沿用 ledger.submit；自動指派選擇啟用且未暫停的 PythonAI 員工，依未結束工作數排序，再以 staff ID 破同分，回傳原因。派工依 owner queue_seq 選最早可執行工作；同員工先進先出，有人忙碌時其他員工可前進。paused／stale／disabled 工作保留在佇列，須取消、更新指派或解除暫停；不會略過同員工的舊工作。

SQLite `BEGIN IMMEDIATE` 與 partial unique index 保證同 owner／staff 僅一個 running／draining／uncertain execution。owner／全域容量以共享 TaskStore 中所有未確定結束的 execution 計算，不以 process 內 semaphore 計算；所有 worker 必須共用 DB 及同一組限制。queue、record、payload、core task、outbox 沿用既有容量，event 計數另限每 execution 10,000。滿載拒絕新增或停止 claim，不靜默丟工作。

lease 預設 30 秒、execution timeout 600 秒，可用 dispatcher constructor 的有限正數參數調整（上限 86,400 秒）；event loop 定期續約並檢查 deadline，start／send／shutdown 也有等待上限。TaskBudget 的持久根 deadline／request／token／cost 等限制仍獨立生效。native model、重試及 evaluator 使用同一個根預算，重啟不提高舊額度，失敗／未知 provider call 仍計費；不繼承呼叫者無關 task 的預算。

## 取消與恢復

`cancel()`、ledger active cancellation、core TaskStore.cancel／cancel_session 同步撤銷 core lease 與 Office 狀態；跨 DB outbox 沿用 cancel_pending 補償。pause 取消正在執行的工作並保留 queued 工作；disable 同時增加員工 revision，仍遵守啟用團隊主管的不變條件。重新指派只接受尚未啟動、沒有 frozen operation 的個人工作，必須比對原 staff ID 及新 staff revision，並排入隊尾。團隊工作須取消後用新 request 重建，避免偷偷改變團隊快照。重送原 request 不建立第二個任務。

native SDK 在 thread 中執行，interrupt 是 cooperative cancellation；close 短暫等待而不取消 to_thread wrapper。無法證明 thread 已停止時保留 uncertain 容量，不假稱已強制終止。lease／timeout 過期與 worker crash 由 `reconcile()` 標記 uncertain，撤銷 core lease；不自動再執行或採納舊 provider 回覆。後續需人工核對實際執行／外部程序狀態，完整恢復工具留在 R3。pending、尚未 claim 的 queued 工作可由重啟 worker 繼續派工。

每個 event 都檢查 live admission、session、generation、turn。terminal event 必須已有 terminal core task；公開結果只取 core reply／verification，不採用 provider 聲稱的 task ID／outcome／驗證標記。終態 outbox 與 assignment／execution 同交易提交；容量失敗不發布結果，保留 uncertain。已取消／舊 lease 的文字、產物描述或完成事件均不發布。此階段沒有獨立檔案產物匯出路徑。

未知或尚在 draining 的 execution 阻擋目錄 mutation 與會話清理。已結束且對帳完成後，既有 erasure 先清 execution／controls，再清 assignment、成員與目錄；跨 DB 順序維持 ADR 0003。

## 原生 adapter 的能力範圍

| 能力 | R2 狀態 |
| --- | --- |
| Native loop、TaskStore checkpoint、既有 task workspace／worktree | 支援；worktree 仍由 `AGENT_TASK_WORKTREE` 及既有綁定決定 |
| 模型與 client 注入、持久根用量 | 支援；共用原生 provider 協定與預算 wrapper |
| 工具 | frozen staff tools ∩ caller ceiling ∩ `read_file/list_files/workspace_info`；逐次仍經既有 policy |
| Hooks、Skills、shell、寫入、MCP、子代理 | 本 adapter 不開放；未宣告 tool_approval／all_tools_gated |
| 記憶與歷史 | 不建立共用 MemoryStore／背景記憶 worker；每 assignment 獨立 messages／core checkpoint，不跨員工或 owner 注入歷史 |
| provider session／durable resume | 不宣告支援；每 assignment 的 session ID／generation 持久綁定，generation 1 不自動重用 |
| 驗收 | 沿用 TaskEvaluator／core verification；prose completed 不等於 verified_completed；執行測試的 acceptance check 須待 R3 |
| HTTP／WS、服務 startup、自動 worker | 留 R4，設定 enabled 不會自行啟動 worker |

外部 Codex／Claude adapter 留 R5；R3 完成之前不擴大原生寫入權限。
