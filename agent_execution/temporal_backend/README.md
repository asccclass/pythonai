# Temporal 可選後端：M0 唯讀橋接

依 `papers/temportal.md` 的 Python SDK 路線實作。**目前僅為 M0 相容橋接，不是 TP09–TP30 的正式工具級恢復／排程／多 Agent 遷移。** 不自動安裝 CLI、不自動啟動背景服務，不遷移既有任務。

## 已實作邊界

- 預設仍為 sqlite；optional SDK lazy import。新任務固定 backend，舊任務不因環境設定而改引擎。
- 同一 TaskStore SQLite transaction 建立 task、submission 與來源 fingerprint，outbox 可由 Worker 啟動後 reconcile。
- Workflow History 僅放版本與 opaque submission ID，輸入／結果仍在同機可信 SQLite；沒有明文對話／絕對路徑進入 History。
- 固定同 DB host queue；只接受 loopback Temporal 服務。Worker 重建角色、workspace、profile 與根預算，限制 READ_TOOLS，整段 Activity 最多執行一次。
- 直接 legacy claim 不可取得已綁 Temporal 任務；已開始 Activity 不可再次取得，取消後不能發布晚到結果。
- StartWorkflow 回覆未知保留 pending，絕不回退本機執行；固定 ID 重送查同一 Workflow。
- `/tasks` 可看 TaskStore 投影，已開始的任務仍保存原 checkpoint；未知 Activity 結果需人工核對，M0 不開放自動／手動跨引擎恢復。

限制：目前不支援 Temporal skill、人工回答／approval resume、排程／coordinator 轉交、逐 token 遠端串流、remote TLS/Cloud、多機資料共用或刪除遠端 History。原生 sqlite 功能繼續使用原路徑。只對新的唯讀測試任務開啟 Temporal；不能用設定旗標假裝寫入能力已完成。

## 版本與安裝

- Python 驗證環境：3.12.0 / Windows amd64。
- SDK `temporalio==1.34.0`、`nexus-rpc==1.4.0`、`types-protobuf==7.35.1.20260906`。
- CLI `1.9.1`（內含 development Server `1.32.0`、UI `2.54.1`）。
- CLI Windows amd64 ZIP SHA-256：`babb65844835045c91fb98930fe10b81de28db80c8c609180473d2c0b18c7589`，與該 release 官方 checksums.txt 比對。
- CLI 來源：<https://github.com/temporalio/cli/releases/tag/v1.9.1>；SDK：<https://pypi.org/project/temporalio/1.34.0/>。
- 不將 CLI binary、測試資料庫或 `.env` 放入 Git；依官方架構對應下載。Linux／macOS／ARM 尚未實機驗證。

```powershell
.\venv\Scripts\python.exe -m pip install -r agent_execution\temporal_backend\requirements.txt
.\venv\Scripts\python.exe -m pip check
```

## 開發服務與 Worker

在受控、非 Git 追蹤的目錄放服務資料。開發 server 不適合正式環境。

```powershell
$temporalData = Join-Path $env:LOCALAPPDATA 'PythonAI\temporal-dev'
New-Item -ItemType Directory -Force -Path $temporalData
temporal server start-dev --ip 127.0.0.1 --namespace pythonai-dev --db-filename (Join-Path $temporalData 'temporal.db')
```

另一個終端機設定同一 `AGENT_TASK_DB`、workspace／project mapping、roles、provider 設定後啟動 Worker：

```powershell
$env:AGENT_TEMPORAL_ADDRESS = '127.0.0.1:7233'
$env:AGENT_TEMPORAL_NAMESPACE = 'pythonai-dev'
.\venv\Scripts\python.exe -m agent_execution.temporal_backend.worker
```

Worker main 讀取 repo `.env`（不覆蓋明確環境變數），並以顯式設定覆寫預設。與前端使用不同 DB 或不同 project workspace 時會拒絕／無法派工；不能把複製 SQLite 到別台機器視為安全的遠端 Worker 設定。queue 是同機路由，不是認證。

在原啟動模式的程序設定：

```powershell
$env:AGENT_EXECUTION_BACKEND = 'temporal'
```

再用現有入口提交新的唯讀任務。`task_id=None` 的臨時 runner 不可轉 Temporal，須透過既有受管理入口產生 task ID。還原新任務預設只需改回 `sqlite`，已綁 Temporal 的任務不會改派。

## 故障處理

- 連線或 StartWorkflow 結果未知：保留 outbox 與 task ID，先啟動相同 DB／namespace 的 Worker reconcile。不得換 task ID 重新執行來「修復」。
- Worker 在 Activity 開始後中斷：Activity max-attempts=1，等待 Temporal 終止狀態並核對原 TaskStore checkpoint，不能自動重放整段。
- 取消：既有 TaskStore cancellation 會被 Worker 的執行檢查攔下。client wait 結束／取消也會要求遠端取消；已發出的 provider 請求無法因此保證未收費。
- M0 不會重置 request/token/cost/deadline；現有 600 秒 wall-clock 包含排隊與等待。
- 工作目錄／角色變更：Worker 重新核對，拒絕搬移或提升權限。不同 source 不可冒用 local 身分。
- UI 顯示 Workflow Running 不保證有進展；用 Temporal UI、Worker poller、task phase、submission state 與 checkpoint 共同診斷。M0 尚無新的 HTTP readiness endpoint。

## 測試

```powershell
$env:PYTHONAI_TEMPORAL_TEST_CLI = 'C:\path\to\verified\temporal.exe'
.\venv\Scripts\python.exe -m pytest agent_execution\temporal_backend\tests -q
.\venv\Scripts\python.exe -m pytest -q
```

未設定測試 CLI 時，真實服務測試 skip；其餘去重／角色／配置等測試仍可執行。無 SDK 時純資料與 legacy import 測試可執行，SDK 測試 skip。測試使用臨時 loopback port、隔離 DB 與 workspace，不操作使用者任務。History replay 與真實模型回應分別驗證，不相互替代。

## ADR：第一階段決策

TP09 共用步驟位於 `../turn_control/steps.py`，透過 `server.run_agent` 同時供
legacy runner 與本機 Temporal 唯讀 Activity 使用。模型單次 I/O、工具 dispatcher
及驗收的 post-state／當輪證據切分已共用；外層 hooks、checkpoint、重試與預算政策保留。
SQLite 真實讀檔回合及真實 Temporal Service/Worker 的同類回合均驗證三種步驟被呼叫。
這是 TP11 前的重構，尚未將每種步驟改成獨立 Activity，M0 的恢復限制仍有效。

1. 使用 Python SDK，不改採 Go 調度器；重用既有政策、預算與 checkpoint。
2. `execution_bindings` 是後端權威，Temporal History 是流程協調權威；TaskStore 仍為當前唯讀 bridge 的執行證據與本機租約。
3. M0 只有一個粗粒度 Activity，故明確 max-attempts=1；TP09/10 完成共享 step 與 operation ledger 後才能做工具級恢復。
4. 可信設定／輸入存同機服務帳號的資料庫，wire payload 不能指定任意身分或路徑。遠端連線在正式 TLS、儲存與認證方案完成前拒絕。
5. 既有 task、root budget、審批與 legacy schemas 保留。只作 additive schema v1；未知 schema 拒絕啟動，不向下覆寫。
6. M0 先持久化不可變 backend reservation，再於同一 transaction 建立 task + submission；只有 binding 而沒有 task/submission 表示尚未受理，不可派工或回退 legacy。若建立 submission 失敗，task 也回滾，重送補齊 reservation。這是對原 TP06「全部同一交易」的具體調整，以保留錯誤／中斷時的後端排他性。

## TP09 驗收紀錄（2026-10-17）

- 完整 pytest：**853 passed、8 skipped、95 subtests passed**，590.27 秒；已設定本機驗證過的 Temporal CLI，真實 Service/Worker 整合測試有執行。紀錄 `temp/temporal-tp09-full.log`（不入 Git）。既有 Starlette/AnyIO deprecation warning 一項。
- 新增共用步驟測試 **9 passed**：單次請求不自行重試、取消不送請求、串流 callback、工具權限、失敗後 context 還原、預算跨載入仍計帳、驗收當輪證據／擷取失敗及 legacy 實際讀檔。
- 首輪相關回歸 **118 passed**，44.38 秒，`temp/temporal-tp09-targeted.log`；之後追加兩項純測試，由上述完整套件覆蓋。真實 Temporal Service/Worker 的受控模型回合確認三種共用步驟都被呼叫，實際讀檔、權限工具清單與任務完成投影均保留。
- `pip check`、compileall、diff check 通過。本次未重新呼叫真實 provider；前次逾時狀態不能視為已解決。未新增逐步 Workflow、ledger、artifact references 或服務部署；目前仍是 M0 唯讀橋接。

## M0 驗收紀錄（2026-10-17，前次交付）

- 完整 pytest：**831 passed、8 skipped、95 subtests passed**，558.82 秒，於 10:00 完成；`temp/temporal-full-final.log`（不入 Git）。這次全套是在後續 Whisper commit 與新增一項純測試的 transaction rollback case 之前完成；Temporal 產品程式碼之後沒有再修改。既有 Starlette/AnyIO deprecation warning 不影響結果。
- 最終 Temporal 模組：**30 passed**，31.26 秒，含真實 CLI service、重啟持久化、History replay、Activity 失敗不重試，以及共用入口 → 實際 Worker → 受控模型工具回合 → read_file → 回覆的整合測試。最後增加的 SQLite trigger 故障注入證明 task/outbox 一起回滾，binding-only reservation 保持排他，重送可補齊。
- 等待期間已有另一筆非本次的 Whisper commit `3276657`；保留它並補做最新 HEAD 的 Temporal + Whisper + tasks + budgets 回歸，**80 passed、4 subtests passed**，82.85 秒。另最終 Temporal 30 項覆蓋新增的 rollback case。
- 任務／預算與 Temporal 順序回歸：**66 passed、4 subtests passed**。曾遇一次既有預算測試清理暫存 SQLite 的 Windows WinError32；預算模組 26 項、順序回歸及最終完整套件均已通過，未放寬斷言。
- `pip check`、compileall、diff check 通過；未啟用 Temporal 的路徑有「不建立 optional schema／bindings」回歸，未安裝 SDK 的情境以子程序阻擋 SDK import 驗證。
- 真實 provider 測試**未通過**：經正式 Worker 的唯讀請求發生 APITimeout，獨立 15 秒小請求也逾時。隔離任務與 submission 均保留 failed，4 筆請求預留沒有被重置；測試服務與 Worker 已停止。紀錄 `temp/temporal-live-provider.log`、`temp/temporal-provider-health.log`，不入 Git。這不等於已確認 provider 的服務端根因，也不能當成真實 LLM 驗收成功。
- 使用者 `.env` 與正式資料未遷移；預設維持 sqlite。本次交付 Windows 同機開發橋接，Linux／macOS／ARM、TLS/Cloud、正式部署、逐工具恢復、人工等待／審批、排程、多 Agent 及完整刪除／留存治理仍待後續階段。
