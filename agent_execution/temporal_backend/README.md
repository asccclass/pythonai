# Temporal 可選後端：唯讀橋接、逐步流程與操作紀錄

依 `papers/temportal.md` 的 Python SDK 路線實作。**預設保留 M0 相容橋接；TP11 新增 opt-in 逐步唯讀流程，接續 TP09 共用步驟與 TP10 操作紀錄。** 不自動安裝 CLI、不自動啟動背景服務，不遷移既有任務。

## 已實作邊界

- 預設仍為 sqlite；optional SDK lazy import。新任務固定 backend，舊任務不因環境設定而改引擎。
- 同一 TaskStore SQLite transaction 建立 task、submission 與來源 fingerprint，outbox 可由 Worker 啟動後 reconcile。
- Workflow History 僅放版本、opaque submission ID、有限控制欄位與 artifact reference，輸入／結果仍在同機可信 SQLite；沒有明文對話／絕對路徑進入 History。
- 固定同 DB host queue；只接受 loopback Temporal 服務。Worker 重建角色、workspace、profile 與根預算，限制 READ_TOOLS。M0 整段 Activity 最多執行一次；逐步模式的 model／tool／evaluation Activity 各最多一次。
- 直接 legacy claim 不可取得已綁 Temporal 任務；已開始 Activity 不可再次取得，取消後不能發布晚到結果。
- StartWorkflow 回覆未知保留 pending，絕不回退本機執行；固定 ID 重送查同一 Workflow。
- `/tasks` 可看 TaskStore 投影，已開始的任務仍保存原 checkpoint；逐步模式只恢復已確認邊界或可證明尚未開始 I/O 的狀態，未知效果需人工核對。M0 不開放自動／手動跨引擎恢復。

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
- Worker 在 Activity 開始後中斷：M0 等待 Temporal 終止狀態並核對原 TaskStore checkpoint；逐步模式按下節的確認邊界恢復，不能自動重放未知操作。
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

## TP12：根預算、retry ownership 與取消矩陣

新受理的 Temporal submission 會把 root budget 的不透明 reference、絕對 deadline 與
immutable limits 保存到本機 `temporal_budget_contracts`。Workflow History 不包含 owner、
limits 或用量；實際用量仍由 `task_budgets`／`budget_calls` 管理。Activity 進入可信 scope
前會用既有 budget row 重建並核對 contract，設定重載、Worker 重啟或 continuation 都不能
提高已受理任務的額度。TP12 前已存在且沒有 contract 的 M0／TP11 submission 維持相容讀取。

Checkpoint 同時固定 iteration、provider retry 與 evaluation retry 上限；恢復只採用保存值，
格式錯誤或超出 `0..256` 的 retry 上限直接拒絕。Retry ownership 固定如下：

| 重試層 | 擁有者／上限 | 預算規則 |
| --- | --- | --- |
| OpenAI SDK hidden retry | 關閉，`max_retries=0` | 不允許 SDK 隱藏額外請求 |
| provider retry | `server.run_agent` 保存的上限 | 每次實際 request 各自先保守預留 |
| model／tool／evaluation Activity | `maximum_attempts=1` | 不與 provider retry 相乘 |
| bootstrap／recover／finalize Activity | 最多 3 次 | 僅處理本機 metadata／投影 |
| Workflow verified recovery | 最多 3 次 | 先核對 cursor、artifact 與 ledger |

目前唯讀範圍的 deadline／取消結果：

| 邊界 | 行為與最終狀態 |
| --- | --- |
| dispatch 前逾期，且尚無 Activity 效果 | 不啟動新 Workflow；fence operations，`failed` |
| Workflow 尚在 queue 時取消 | Worker reconciler 投影 `cancelled`；不呼叫模型 |
| host slot、provider 前、retry backoff、tool 前 | cooperative check 停止；不開始下一個 I/O |
| provider request 已送出 | 保留該次 request 預留；late result 受 fence 阻擋 |
| tool 已標記 `tool_started`，完成效果無法證明 | `uncertain`，不自動重做 |
| 結果已提交但 Activity 回覆遺失 | 取回已提交 cursor／artifact，不再扣 request 或重跑工具 |
| finalize／projection 後收到舊回報 | submission fence、task 狀態與 owner lifecycle 拒絕發布 |
| Temporal `CANCELED` | 投影 `cancelled` |
| Temporal `FAILED`／`TIMED_OUT`／`TERMINATED` | 投影 `uncertain`，等待證據核對 |

人工回答／審批等待、子 Workflow 傳播與寫入工具的子程序樹終止仍分別由 TP13、TP14、
TP17 完成。TP12 不將正在執行的外部請求宣稱為可強制撤回；它只停止後續步驟並拒絕
過期結果發布。

TP12 驗證（2026-10-18）：完整 pytest **1016 passed、9 skipped、95 subtests passed**，
781.06 秒；TP12 單元矩陣 **75 passed**，擴大 Temporal／server／evaluator／budget 專項
**276 passed、6 subtests passed**，真實 Service/Worker 整合 **7 passed**。受控 provider
先回 429 再成功時，四次實際 request 對應四筆 budget calls，但 History 仍只有兩個
model Activities，且兩者 `maximum_attempts=1`。另一個真實服務案例在 Worker 啟動前
取消已排隊 Workflow，reconciler 投影為 `cancelled`，provider 呼叫數為零。新舊 History
replay、回覆遺失取回、budget contract 設定變更／竄改、dispatch 前逾期也均通過。
紀錄 `temp/temporal-tp12-full.log`、`temp/temporal-tp12-unit.log`、
`temp/temporal-tp12-targeted.log`、`temp/temporal-tp12-integration.log`。pip check、
compileall 與 diff check 通過；完整套件只有既有 Starlette／AnyIO deprecation warning。
本輪使用受控 provider 驗證 retry ownership，沒有把 TP11 逾時的真實 LLM 驗收改標為成功。

## TP11：opt-in 逐步唯讀 Workflow

在原入口的程序設定以下旗標，只影響新任務；Worker 仍須另外啟動：

```powershell
$env:AGENT_EXECUTION_BACKEND = 'temporal'
$env:AGENT_TEMPORAL_ENGINE = 'steps'
```

`AGENT_TEMPORAL_ENGINE` 預設為 `bridge`。受理時將 Workflow type／協定版本與 queue
一起持久化；已受理任務依保存的協定派送，不受後續旗標切換影響。
舊 M0 使用原 queue 與 `ReadOnlyBridge`，新協定使用獨立的 `steps-readonly-v1` queue
與 `AgentTaskWorkflowV1`。同一 Worker 程序同時服務兩者，共享 host execution slot 上限；
回滾新任務到 `bridge` 或 `sqlite` 時，須保留此版 Worker 處理已綁 steps 的工作。

已完成的待辦與行為：

- [x] `contracts.py` 定義 v1 cursor／snapshot；cursor 僅含 submission、revision、next_kind 及有 digest／大小的引用。對話、provider attempt、iteration、progress、tool-call/result、evaluation_start 均留在本機 artifact。
- [x] `turn_control/continuation.py` 與 runner 邊界共用原 hooks、policy 與 evaluator；每次 continuation 最多執行一個 model／tool／evaluation 邏輯步驟。model 步驟內原 provider 重試仍由既有 runner 唯一持有。
- [x] 六種 Activity 為 bootstrap、model、tool、evaluation、recover、finalize；Workflow 只協調引用。執行 Activity 每次重建可信 scope、最新 profile／OS、角色與原 root budget，不重設迭代或驗收上限。
- [x] model／tool／evaluation 的 `maximum_attempts=1`；只做 metadata 的 bootstrap／recover／finalize 最多三次，Workflow 額外最多三次經核對的恢復，最多 1024 個執行步驟。設定顯式 RetryPolicy 的依據見 [官方錯誤處理說明](https://docs.temporal.io/develop/python/best-practices/error-handling)。
- [x] `step_store.py` 保存有版本的 cursor／fence 與原子 artifact 投影。完成的 cursor 重送取回原引用；checkpoint 已完成但 Activity 回覆遺失時，投影下一步，不重跑模型或工具。
- [x] 確認邊界為 `step_ready`、`model_received`、`tool_done`、`evaluation_done`；model／evaluation pending 只有 ledger 尚未開始 I/O 時才可重新取得。PRE hooks 開始前先寫入 `step_preparing`，此階段未確認完成也停止為 uncertain。`tool_started` 即使已有 raw result，也因完成 hooks 未確認而停止為 uncertain；running／uncertain ledger 不重送。
- [x] 已知拒絕、預算、取消、缺 artifact、格式／校驗錯誤與未知執行分別映射；SDK failure 不帶原始錯誤內容或憑證。人工等待、審批與寫入仍未開放。

TP11 驗證（2026-10-18）：完整 pytest **949 passed、8 skipped、95 subtests passed**，
670.92 秒；Temporal／continuation 專項 **122 passed**，前置 hook 邊界專項 **35 passed**。
真實 Service/Worker 使用受控模型完成讀檔／驗收、提交後回覆遺失、
Worker 停止再啟動、新舊 queue 同時服務與 History replay；未知工具效果僅執行一次並停止。
紀錄 `temp/temporal-tp11-full-final.log`、`temp/temporal-tp11-final-targeted.log`、
`temp/temporal-tp11-boundary-final.log`。pip check、compileall、diff check 通過；完整套件只有既有
Starlette／AnyIO deprecation warning 一項。

真實 LLM 本輪**未通過**：隔離 step 任務在第一個 model 步驟遭遇 APITimeout，
90 秒期限後 cancelled，三筆請求預留保留、沒有工具執行；獨立 15 秒探測也回報
`APITimeoutError`，使用另一筆獨立請求。紀錄 `temp/temporal-tp11-provider-check/live.log`、
`probe.log`；服務與 Worker 已停止。尚未取得 provider 服務端根因，不能用前輪 TP10
成功或本輪受控模型結果代替本輪真實 LLM／原始長任務驗收。預設設定維持原狀。

## TP10：操作紀錄與本機結果引用

`ledger.py` 使用同一 TaskStore SQLite 的 additive `temporal_operation_schema` v1、
`operation_ledger` 與 `temporal_artifacts`。未啟用 Temporal 的 runner 不建立這些表。
`operations.py` 以 Activity-local ContextVar 接入共用 steps；離開 Activity 後還原，
不將 SDK、ledger 或身分 context 放入 Workflow History。

已完成的待辦與驗收範圍：

- [x] `(owner, task_id, step_id)` 去重，digest 包含操作種類、canonical arguments 及已綁定的輸入／context／角色快照。
- [x] `prepared → running(fence) → completed`；已開始但結果未知為 `uncertain`，確認執行前拒絕為 `rejected_no_effect`。running／uncertain／rejected 不重新取得執行權。
- [x] 模型以 iteration + provider attempt、工具以 iteration + tool-call ID、驗收以 iteration + eval retry 產生穩定 ID。provider 重試仍由原 runner 控制，每次真正請求沿用原預算。
- [x] 結果與 evidence 以 JSON artifact 保存；引用只含 `schema_version/id/sha256/bytes`。兩份 artifact 與 completed 狀態同一交易提交，缺失或校驗失敗會報錯，不能藉此重新執行工具。
- [x] 取消／Workflow 終止時，prepared 標為未執行拒絕，running 標為 uncertain；提交同時核對 operation fence、submission fence、task 狀態、owner lifecycle 及 workspace mapping。
- [x] 沿用現有 TaskStore／lifecycle 的任務 worktree 登記；拒絕任意 workspace override，保留 opt-in worktree 路徑。
- [x] 快取重用仍檢查目前角色與工具 allowlist；角色降權後，不使用舊的模型工具能力清單發送請求。
- [x] 故障注入包含實際子程序在副作用後／提交後直接退出、並行搶占、DB trigger 回滾、參數變更、artifact 修改／缺失、取消／刪除及跨 owner/task 拒絕。

儲存 ADR：TP10 的小型 JSON 結果先使用 SQLite BLOB，讓結果、evidence 與 operation
完成狀態可原子提交，避免檔案 rename 與 DB commit 間留下分離狀態。引用不含絕對路徑。
這細化了規劃原先的本機檔案 artifact store；大型附件／圖片、多機共享、retention 與
完整刪除整合仍由 TP18／TP29 等階段完成。資料仍屬同機可信儲存，應納入原 DB 備份與存取控制。

可選容量設定（不改變 root model budget）：

| 設定 | 預設 | 範圍 |
| --- | --- | --- |
| `AGENT_TEMPORAL_ARTIFACT_MAX_BYTES` | 2097152 | 每份 JSON result／evidence 的 UTF-8 bytes |
| `AGENT_TEMPORAL_LEDGER_STORAGE_BYTES` | 268435456 | 全部 ledger／artifact 內容及保留的欄位 metadata 空間 |
| `AGENT_TEMPORAL_OPERATION_LIMIT` | 100000 | 全資料庫 operation 筆數 |

啟動設定與 preflight 會驗證三個設定必須為正整數；preflight 顯示這些限額但不建立資料庫。

完成過的操作可由同一任務 scope 的 `Ledger.run` 取回原結果，不再次呼叫 executor 或扣模型請求。
未知結果無自動 reset／retry API。現有 M0 仍是單一 Activity，max-attempts=1，
未開放 manual resume；M0 接線留下操作紀錄。TP11 opt-in 模式另提供上述已確認邊界的恢復。
整個 Activity、hooks、事件／記憶 finalize 的重送去重也未因此完成，分別見後續任務。

本輪回歸另定位並修正 budget stream 關閉競態：取消監看執行緒可能仍在讀取 SQLite，
原 `close()` 只通知停止就返回，Windows 清理可出現 WinError32。現在在 lock 外等待監看
執行緒結束，監看執行緒自行關閉時不 join 自己，close 發生例外也會收尾；三項回歸驗證
進行中的資料庫檢查、監看自我取消與 stream close 例外。

## 共用步驟與 M0 決策

TP09 共用步驟位於 `../turn_control/steps.py`，透過 `server.run_agent` 同時供
legacy runner 與本機 Temporal 唯讀 Activity 使用。模型單次 I/O、工具 dispatcher
及驗收的 post-state／當輪證據切分已共用；外層 hooks、checkpoint、重試與預算政策保留。
SQLite 真實讀檔回合及真實 Temporal Service/Worker 的同類回合均驗證三種步驟被呼叫。
這是 TP11 前的重構，尚未將每種步驟改成獨立 Activity，M0 的恢復限制仍有效。

1. 使用 Python SDK，不改採 Go 調度器；重用既有政策、預算與 checkpoint。
2. `execution_bindings` 是後端權威，Temporal History 是流程協調權威；TaskStore 仍為當前唯讀 bridge 的執行證據與本機租約。
3. M0 只有一個粗粒度 Activity，故明確 max-attempts=1；TP09/10 已完成共享 step 與 operation ledger，工具級自動恢復仍須 TP11 逐步 Workflow 接管。
4. 可信設定／輸入存同機服務帳號的資料庫，wire payload 不能指定任意身分或路徑。遠端連線在正式 TLS、儲存與認證方案完成前拒絕。
5. 既有 task、root budget、審批與 legacy schemas 保留。只作 additive schema v1；未知 schema 拒絕啟動，不向下覆寫。
6. M0 先持久化不可變 backend reservation，再於同一 transaction 建立 task + submission；只有 binding 而沒有 task/submission 表示尚未受理，不可派工或回退 legacy。若建立 submission 失敗，task 也回滾，重送補齊 reservation。這是對原 TP06「全部同一交易」的具體調整，以保留錯誤／中斷時的後端排他性。

## TP10 驗收紀錄（2026-10-17）

- 完整 pytest：**902 passed、8 skipped、95 subtests passed**，764.80 秒；真實 CLI Service/Worker 測試已啟用。紀錄 `temp/temporal-tp10-full.log`（不入 Git）。既有 Starlette/AnyIO deprecation warning 一項，沒有新增執行緒例外 warning。
- 故障／預算／共用步驟專項回歸 **81 passed**，32.45 秒，`temp/temporal-tp10-fault-pass.log`。包括程序在副作用後直接退出不重做、提交後退出取回原結果、實際 Git worktree，以及三種 stream monitor 關閉情境；最終完整套件另覆蓋最後的 workspace guard。
- 真實 Service/Worker 模組回歸曾通過 **72 passed**，42.43 秒；後續補上的 terminal-task cache／worktree 測試也由完整套件覆蓋。實際 Worker 中留下 model、tool、model、evaluation 四筆 completed 操作，工具結果可用 artifact reference 校驗取回，History 不含讀檔內容。
- 真實 provider 小型探測成功，使用一筆獨立請求。隨後以隔離 SQLite／tenant／workspace 及 loopback Service 完成 **真實 LLM → read_file → 最終回覆 → 驗收**，使用 **3 筆模型請求**；submission／task 均 completed，四筆操作均 completed。重取工具結果未再執行 executor，請求計數保持 3，History 不含檔案內容。紀錄 `temp/temporal-tp10-provider-check/probe.log`、`live.log` 與同目錄隔離 `live-*` 資料；服務與 Worker 已結束。
- 先前 M0 的 provider APITimeout 沒有因此取得服務端根因；本輪證明當次小型唯讀回合成功，不代表原始長任務、寫入能力或持續可用性已驗收。
- `pip check`、compileall、diff check 通過。budget stream 競態修正已獨立提交 `bf7bcfd`。未修改使用者 `.env`，未啟用正式後端或搬遷正式資料；TP11 逐步 Workflow、TP18 完整刪除／retention 與 TP29 多機仍待實作。

## TP09 驗收紀錄（2026-10-17，前次交付）

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
