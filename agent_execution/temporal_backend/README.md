# Temporal 可選後端：逐步流程、操作紀錄與審批寫入

依 `papers/temportal.md` 的 Python SDK 路線實作。**預設保留 M0 相容橋接；TP11 新增 opt-in 逐步唯讀流程，接續 TP09 共用步驟與 TP10 操作紀錄；TP17 另提供顯式審批的同機檔案寫入；TP19 提供新排程 CRUD 與唯讀 occurrence；TP20 提供逐筆舊唯讀排程遷移／切換／回滾；TP21 提供獨立 opt-in 的 coordinator DAG 父／子 Workflow 與 shared budget。** 不自動安裝 CLI、不自動啟動背景服務，不遷移既有任務。

## 已實作邊界

- 預設仍為 sqlite；optional SDK lazy import。新任務固定 backend，舊任務不因環境設定而改引擎。
- 同一 TaskStore SQLite transaction 建立 task、submission 與來源 fingerprint，outbox 可由 Worker 啟動後 reconcile。
- Workflow History 僅放版本、opaque submission ID、有限控制欄位與 artifact reference，輸入／結果仍在同機可信 SQLite；沒有明文對話／絕對路徑進入 History。
- 固定同 DB host queue；只接受 loopback Temporal 服務。Worker 重建角色、workspace、profile 與根預算，預設限制 READ_TOOLS（human V2／approval V3 另允許 ask_user；TP17 顯式寫入任務再增加三個檔案工具）。M0 整段 Activity 最多執行一次；逐步模式的 model／tool／evaluation Activity 各最多一次。
- 直接 legacy claim 不可取得已綁 Temporal 任務；已開始 Activity 不可再次取得，取消後不能發布晚到結果。
- StartWorkflow 回覆未知保留 pending，絕不回退本機執行；固定 ID 重送查同一 Workflow。
- `/tasks` 可看 TaskStore 投影，已開始的任務仍保存原 checkpoint；逐步模式只恢復已確認邊界或可證明尚未開始 I/O 的狀態，未知效果需人工核對。M0 不開放自動／手動跨引擎恢復。

限制：目前不支援 Temporal skill、V1／V2 approval resume、舊副作用排程／既有 coordinator DAG 遷移、跨主機事件串流、remote TLS/Cloud、多機資料共用或刪除外部 archival／備份。human V2 模式已開放 `ask_user` 文字回答續行；M0 bridge／steps V1 不支援。原生 sqlite 功能繼續使用原路徑。預設只對新的唯讀測試任務開啟 Temporal；TP17 的三個檔案工具需另行顯式受理與逐次人類審批，詳細邊界見下節。

## TP19：新排程 CRUD 與唯讀 occurrence

`schedule_cli` 是本機 operator 管理入口，沿用原 execution_scope 的 Python `Schedules` bridge
也可供既有入口整合。它不是新的 Web/API 服務，也未註冊成模型工具。所有 get/list/change 以
原 owner 隔離；管理需要 admin。新排程預設 paused，明確 resume 才開啟。
前端與 Worker 必須共用 `AGENT_TASK_DB`、`SCHEDULER_DB_PATH`、workspace、roles 與 namespace；
排程 DB 絕對路徑與 task DB 的 host queue 已綁定，變更配置會拒絕執行／清理。

```powershell
.\venv\Scripts\python.exe -m agent_execution.temporal_backend.schedule_cli create --name "唯讀觀察" --cron "*/5 * * * *" --kind watch --prompt "檢查指定專案狀態，依 watch JSON 契約回報" --max-runs 12
# 將輸出的 id 代入以下命令。
.\venv\Scripts\python.exe -m agent_execution.temporal_backend.schedule_cli list
.\venv\Scripts\python.exe -m agent_execution.temporal_backend.schedule_cli resume --id <id>
.\venv\Scripts\python.exe -m agent_execution.temporal_backend.schedule_cli update --id <id> --timezone UTC --cron "0 9 * * *"
.\venv\Scripts\python.exe -m agent_execution.temporal_backend.schedule_cli pause --id <id>
.\venv\Scripts\python.exe -m agent_execution.temporal_backend.schedule_cli cancel --id <id>
.\venv\Scripts\python.exe -m agent_execution.temporal_backend.schedule_cli notifications
```

- create 回傳持久 id；sync 成功 exit 0，未知／離線回傳 `sync=pending`、exit 2。
  不要重複 create；以同 id 的 `sync --id <id>` 或原 Worker reconcile 完成。list 的
  `revision/applied` 是本機 desired/applied 狀態；直接在 Temporal UI 的外部變更不會被當成本機授權。
- 只受理 `agent/watch`、`{"prompt":"..."}`，沿用名稱／payload／五欄 cron／IANA 時區／
  timeout 1..300 秒／max_runs 1..1000 驗證。新建時區預設 Asia/Taipei；既有 UTC 維持 UTC。
  agent/watch 的工具為 READ_TOOLS 與原 scope 工具清單的交集，忽略全域 file-write opt-in；
  skill、script、command、發布、寫檔及 coordinator delegation 尚未開放。
- watch 固定 SKIP；agent 支援 `--overlap BUFFER_ONE`，最多保留第一個待跑需求，後續重疊需求略過。
  不受理 ALLOW_ALL／historical backfill。真實 Server 將 catchup 最小值正規化為 **10 秒**，
  因此五欄 cron 最多接受一個近期分鐘觸發，不能宣稱絕對零補跑。
- 每次原生觸發是 reference-only occurrence Workflow，再啟動固定 owner 的獨立 task Workflow。
  prompt／結果只留本機；History 不含原文。metadata Activity 最多三次，固定 occurrence/task ID
  與一次 run_count；執行模型／工具仍用原共用 runner、Activity 最多一次，沒有另一套模型引擎。
- 接受 occurrence 時才計一次，即使之後取消／失敗仍計數；Activity retry 不增加，
  不能調整 max_runs 低於既有 run_count。抵達上限需先明確提高 max_runs 才可 resume；
  更新上限不重置計數。pause 停止新受理並保留已受理任務；cancel 不可 revive，
  fencing 已受理任務，Worker 投遞取消。排程 timeout 於預留時刻固定且不延長根 deadline。
- 原 notification queue、跨平台路由與去重保持；本機/API/WebSocket 可查詢，
  communication 只向原對話與已配置 adapter 投遞，仍重查 role／sender allowlist。
  watch 等待直到次數上限轉 needs_input；無法判定／失敗的 occurrence 停止排程並要求檢查。
- legacy SchedulerStore claim 明確排除 temporal rows，無 binding 的中斷預留也不會轉回 SQLite。
  舊 jobs 不自動遷移；TP20 顯式 dry-run、cutover、rollback 與 DST／停機驗收見下節。
- session 刪除沿用原 frozen preview／確認，包含 paused native schedules。先 tombstone owner、
  刪除 native Schedule、關閉／刪除 wrapper History 並核對 visibility，再清理 task／本機內容；
  保存最小雜湊 receipt。offline、活躍 metadata／Activity、transfer task 或 index lag
  都保持 pending 並保留檔案。須恢復相同 Service **及 Worker**；archival／備份仍需外部政策處置。
  排程尚未消耗的 task 結果不可先 retention；本輪沒有排程層自動 GC。

TP19 驗收（2026-10-23）：新增 **35 項契約測試、5 項隔離真實 Service 測試**；完整 venv pytest **1326 passed、12 skipped、95 subtests passed**，1299.93 秒，只有既有 Starlette／AnyIO deprecation warning。`pip check`、compileall、diff check 通過。最後紀錄 `temp/temporal-tp19-full.log`（不入 Git），35 項契約獨立驗證為 `temp/temporal-tp19-unit-final.log`，真實 Service 獨立驗證為 `temp/temporal-tp19-service-final.log`。真實 provider 付費驗收未重跑；受控 runner 不代表正式模型／平台通知
或生產部署已驗收。測試服務與資料均隔離，未修改正式 .env 或使用者排程。

## TP20：舊排程 dry-run／切換／回滾

`schedule_migration_cli` 是可信本機 operator 入口；Python `Migration` API 可沿用原
execution_scope 管理 API／WebSocket／communication owner。CLI 沿用本機 owner，
不能冒用另一個 session。需要當前及原 owner admin、相同 workspace／TaskStore／排程 DB；
新切換仍受 trial allowlist 限制。回滾不因 trial 關閉而被阻擋，但仍重查角色及 session tombstone。

只遷移 TP19 支援的唯讀 agent/watch、精確 `{"prompt":"..."}` payload。
skill／script／command／寫入／coordinator 留在 SQLite。預設不變更任何既有 job；
必須逐筆 preview、stage、cutover，沒有批次自動遷移或歷史 backfill。

```powershell
.\venv\Scripts\python.exe -m agent_execution.temporal_backend.schedule_migration_cli preview --id <legacy-id>
# 審閱 owner hash、payload digest、原 next_run、run_count、timezone、next_10，代入 source_digest。
.\venv\Scripts\python.exe -m agent_execution.temporal_backend.schedule_migration_cli stage --id <legacy-id> --digest <source_digest>
.\venv\Scripts\python.exe -m agent_execution.temporal_backend.schedule_migration_cli cutover --id <legacy-id>
# 離線／未知 RPC exit 2：保持相同 id 與 intent，恢復 Service 及 Worker 後續作。
.\venv\Scripts\python.exe -m agent_execution.temporal_backend.schedule_migration_cli reconcile --id <legacy-id>
.\venv\Scripts\python.exe -m agent_execution.temporal_backend.schedule_migration_cli rollback --id <legacy-id>
```

- preview 只以 SQLite read-only mode 讀取，不建立 schema、不載入 SDK、不連 Service，
  不輸出 prompt 原文。`--at <aware ISO time>` 只用於預覽，不是補跑命令。
  實際 stage 保存完整原設定、owner、原 UTC next_run、計數、payload digest 與 generation；
  來源任一變動需重新 preview/stage。活躍 lease、running／uncertain legacy run 不可遷移。
- stage 建立 paused Native Schedule，legacy 繼續持有；用真實
  ListScheduleMatchingTimes 比對未來 10 個 UTC 時刻，含 DST gap／fold。
  不存在的當地時間略過，重複當地時間保留兩個不同 UTC 時刻；不符則保持 paused。
- cutover 原子設為 transition／停用 legacy，之後才啟用 native。activating 重入時刷新
  未來至少一分鐘的 start_at／本機 barrier，拒絕切換前與停機期間的舊觸發。
  SQLite 與 native 共用 (job_id, UTC slot) 唯一 fence，metadata retry 不另計數。
  已受理的未知結果不會自動換引擎重跑；max_runs、既有歷史與 immutable root budget 不歸零。
- 原 `scheduled_jobs.is_enabled` 對 native 永遠為 0，可阻擋舊版 query；
  native desired enable 存在 binding，請由原生管理入口查詢。
  進入 transition 後拒絕一般 native CRUD，使用同一 migration intent 續作。
- rollback 先停止新受理、pause native，等待本機 occurrence、task、live reference、
  operation ledger 及遠端 Workflow 關閉；未知／uncertain 保留 pending。
  刪除 Native Schedule 並核對 NOT_FOUND／applied revision，才恢復 SQLite。
  取現在與最後已受理 UTC slot 的較晚者，選嚴格在其後的下一次 cron，不補歷史。
  人工停用、native pause、watch 完成／needs_input 與次數上限不會因回滾復活。
  已完成 task／預算／通知仍沿用原資料，observation digest 保持 legacy 格式，
  相同觀察跨引擎不重發；retired binding 阻擋舊 Worker 重建 native。
- Worker 只續作明確 cutting_over／activating／rolling_back intents，staged 不自動切換。
  Session 刪除沿用 TP18 frozen plan；遠端及原任務清理驗證後，移除遷移原文 snapshot
  與 UTC fences，只留原雜湊收據。不是排程層自動 GC，fence 上限 100000 筆時拒絕新受理。

TP20 詳細驗收見 `papers/temportal.md` 12.11。新增 **35 項契約、6 項真實 Service 測試**全部通過；完整 venv 首輪 **1366 passed、12 skipped、1 failed、95 subtests passed**（1658.42 秒）。未改動的 Go scaffold 首輪未取得 passing case，整個 results 檔重驗 **19 passed**（33.22 秒），未重現，未確認根因，首輪不記為全綠。另有 Starlette／AnyIO 及 Tkinter 解構 warnings（3）。pip check、compileall、diff check 通過。紀錄 `temp/temporal-tp20-full.log`、`temp/temporal-tp20-results-recheck.log`、`temp/temporal-tp20-unit-final.log` 不入 Git。未呼叫付費 provider、未修改正式設定／使用者排程；受控 runner／scheduled timestamp 不代表正式模型、平台通知或已等待真實分鐘 cron 驗收。

## TP21：DAG 父／子 Workflow、共用預算與獨立 Reviewer

新增的協作 root 以 `AGENT_COORDINATOR_BACKEND=temporal` 明確選用；預設仍為 sqlite。
這是既有 root task 的協作圖調度，root task 的主 backend 保持原綁定。
原 M0／V1–V3 的工具清單保持既有能力；一般使用方式是由有權委派的 sqlite root task
呼叫原 `invoke_subagent`／`check_subagent`／`manage_subagent`。
Planner、Coder、Reviewer 各自有固定 Child Workflow、TaskStore task 及隔離 worktree；
Coder 仍沿用原 coordinator 授權的工具及 runner，並取原父工具清單與 persona 工具的交集。

```powershell
# 僅影響之後建立的協作 root；不遷移已存在的 DAG。
$env:AGENT_COORDINATOR_BACKEND='temporal'
# 以原 managed sqlite task 入口委派，invoke_subagent evidence 會帶 graph_id。
# 原明確啟動的 Temporal Worker 同時服務 DAG control / I/O queues。
.\venv\Scripts\python.exe -m agent_execution.temporal_backend.worker

.\venv\Scripts\python.exe -m agent_execution.temporal_backend.dag_cli status --id <graph_id>
.\venv\Scripts\python.exe -m agent_execution.temporal_backend.dag_cli seal --id <graph_id>
.\venv\Scripts\python.exe -m agent_execution.temporal_backend.dag_cli cancel --id <graph_id>
.\venv\Scripts\python.exe -m agent_execution.temporal_backend.dag_cli reconcile --id <graph_id>
```

- backend 在第一個 node 受理時固定；未綁定的舊 root 仍保留 sqlite。legacy claim／recover
  不執行 native nodes；worker 子程序還需核對持久 Activity 許可與活躍程序身分。
  子 task 固定 Temporal claim fence，在授權 Activity scope 內呼叫原 durable runner，
  不再包一層 M0 bridge，也不新增另一套模型執行器。
- 原 owner／session／admin／trial、workspace／managed worktree／DB host queue、namespace
  都需重新核對。CLI 是可信本機管理入口，Python `Graphs` bridge 可沿用原 execution_scope。
  角色或設定失效停止受理，session tombstone 阻擋重新執行。
- 父 Workflow 決定 DAG 依賴與併發，並沿用跨 root／跨 legacy-native 的全域 coordinator 名額；
  名額不足時只作 metadata 等待，不另建 child、不增加預算或重試 I/O。Coder 的 `review_pending` 會等獨立 Reviewer。
  approve 必須為真正 boolean、findings 必須為空，任何格式錯誤／拒絕會阻擋依賴。
  Child Workflow 結束只代表執行結束，業務成功仍核對原 SQL 狀態。
  `reviewed` 不等於已 merge；原 admin／immediate parent／base-content／Reviewer 驗證保持，
  merge 仍需原明確操作，已停止 DAG 不能再發起 merge。
- 沿用同一 TaskBudget 的 requests／tokens／cost／children／depth／deadline，不另配子預算。
  首次受理保存 immutable contract，新增 Reviewer 也計 child，metadata retry 不重算。
  root 剩餘期限限一小時內，每 node timeout 沿用 1..600 秒、root 最多 128 nodes（含 Reviewer），
  最多 1000 個 DAG，contract 限 64 KiB；不延長原根期限。
- node I/O Activity 最多一次，metadata 最多三次；不確定 process／Activity completion
  轉 needs_resolution 並保留原 checkpoint／worktree，不能自動由另一引擎重跑。
  遺失 Start 回覆以相同 parent／child ID 重送，不另建 root 或 child。
- control 與 I/O queue 分離，I/O 併發由 Worker 設定限制，避免長執行阻塞取消／投影。
  Child Cancellation 明確 WAIT_CANCELLATION_COMPLETED，Parent Close Policy 為 REQUEST_CANCEL；
  取消先寫本機 fence，再等實際子程序樹結束；晚到結果不能發布。
  外部請求已送出時，仍不能保證撤回。
- `seal` 關閉新 node 受理，但保留已受理 Coder 所需 Reviewer。root task 完成或 seal 後，
  全部 nodes 解析完才關閉圖；否則在原根期限內繼續接受動態委派。
  `cancel` 保存停止意圖，`reconcile` 未知 RPC 回傳 pending／exit 2，原 Worker 可有界續作。
  已關閉／取消的圖不再追加 node，後續工作使用新的 durable root。
- History 只有 graph/node opaque references 及有限取消控制欄位，prompt／結果／路徑只留本機。
  session frozen plan 包含精確 DAG/child targets；先排空 local Activity／lease，
  核對 parent 與 child History／visibility NOT_FOUND，才清理檔案與原文，留下雜湊收據。
  offline／transfer task／index lag 保留原 frozen files。archival／備份仍需外部政策處置。
  普通 retention 會保留有 DAG 關聯的 root，避免共用預算或 guarded merge reference 提前失效；
  Temporal merge／孤兒治理接續見 TP22；自動圖 GC 與容量治理留 TP23。

TP21 DAG 最終驗證 56 passed（43 單元／契約、13 隔離真實 Service）；完整專案回歸 1443 passed、12 skipped、95 subtests passed，僅 1 項既有 deprecation warning。測試服務補強另有 3 項單元驗證。
TP21 驗收數字與紀錄見 `papers/temportal.md` 12.12；本輪使用隔離 Service、受控模型 runner，
不修改正式設定、不搬移使用者 DAG、不進行付費 provider 或正式部署驗收。

## TP22：DAG worktree／merge／孤兒治理

沿用 TP21 的同機 DB／角色／workspace 與原 guarded merge。預設後端不變，Worker 不會自動
建立 merge 或 archive 意圖。只能由同 owner 的非委派 admin 透過現有人類入口明確提交：

```text
/agents <root_task> merge <node_id>
/agents <root_task> archive <node_id>
/agents <root_task> operations <node_id>
```

亦可使用可信 CLI（其執行身分／session 必須與 graph owner 相符）：

```powershell
.\venv\Scripts\python.exe -m agent_execution.temporal_backend.dag_cli merge --id <graph_id> --node <node_id>
.\venv\Scripts\python.exe -m agent_execution.temporal_backend.dag_cli archive --id <graph_id> --node <node_id>
.\venv\Scripts\python.exe -m agent_execution.temporal_backend.dag_cli operation-status --id <operation_id>
```

- 操作先持久化，Worker reconcile 以固定 Workflow ID 派送到同 host 的 `-dag-v1-lifecycle-v1`
  queue；History 只含 operation ID。I/O Activity 最多一次，不因回覆遺失、Worker 重啟或租約過期重做。
  查詢以本機操作 state 為準；Workflow 回傳 reference 不代表未知效果已確認完成。
- merge 仍要求 immediate-root Coder、獨立 Reviewer 的嚴格 approve／空 findings，以及原 root
  deadline／角色／目錄權限。意圖固定 Reviewer、結果、worktree HEAD／status／內容摘要；
  原 parent bytes／base 與 128 files／32 MiB 限制仍由 guarded merge 核對。逐檔及發布前重查取消
  fence，衝突保留人工處理，取消時只回復仍等於本次寫入的檔案，不改父 repository HEAD。
  既有直接 guarded-merge API 保留原管理員／immediate-parent 權限；已有 lifecycle 意圖的節點
  不可再跨路徑重複 merge。取消 fence 不會被晚到 worker failure 降級，兩種到達順序都保留待核對狀態。
- archive 只接受已終止／merged／completed 節點；未合併的 reviewed Coder 須先明確取消。
  整個 DAG 必須結束，所有 Activity／子程序及租約都已排空。核對已授權內容後，由既有
  WorktreeStore 保存 bundle／zip／metadata，含 ignored、untracked 檔案；移除前再次核對內容。
  可用原 worktree restore 還原。完成或未知操作的相同 ID 不會再次移除還原後的目錄。
- 子程序在開始 worktree I/O 前保存實際 PID／process creation identity。Worker 不在且子程序仍
  存活時只發出 cancellation fence 並保留資源；直到兩者都確認退出才投影 needs_resolution，
  不會重新執行、不重算 attempts／child count／預算，也不自動刪 worktree。無法核實存活時保留。
- 取消 merge Activity 先設 fence，再等待 I/O thread 排空後回覆；已開始的 archive 可能完成其
  保存動作，取消仍須等待排空，不宣稱能撤回已發生效果。程序在效果後退出會留下 uncertain，
  需人工核對；禁止自動 replay。新生命周期意圖限 10000 筆；worktree 摘要／封存限 10000 項、
  單檔 64 MiB／總計 256 MiB，拒絕連結或嵌入 repository。
- lifecycle Worker 最大 heartbeat 節流為 2 秒、Activity heartbeat timeout 為 15 秒。
  reconcile 也核對 lifecycle Workflow 的 cancel／terminate／failure，避免遠端先閉合而本機 merge
  尚未排空時錯誤放行清理；遠端已閉合不等於本機 I/O 已停止。
- Session frozen erasure 計畫包含確切 operation IDs；存活執行仍擋清理，對應 lifecycle Histories／
  visibility 查無紀錄後才清除本機操作／程序 reference。舊無 operation 的 frozen 計畫不改格式。

TP22 新增 31 項（25 單元／契約、6 真實 Service）全數通過；最終完整回歸 1488 passed、
13 skipped、95 subtests passed，1 項既有警告。驗收與首輪競態修正見 `papers/temportal.md` 12.14；
紀錄 `temp/temporal-tp22-full.log` 不入 Git。驗證使用 Windows 同機隔離 Git repository、
真實 Service／Worker 與受控 Reviewer 結果；非 Windows 的破壞性清理、正式 provider、
Docker／多機或正式部署未實機驗收。

## TP23：DAG History Continue-As-New（隔離 Service 驗收完成）

`CoordinatorDAG` checks the SDK's `is_continue_as_new_suggested()` hint after reconciling
its persisted graph snapshot. It continues with the same opaque `graph_id` only when every
child handle is settled. The next run re-reads SQLite as authority for graph state, root
budget, idempotency records, and cancellation; active children are never orphaned by parent
Continue-As-New. The SDK control exception is explicitly re-raised instead of entering the
DAG stop/fence path. Session erasure now enumerates every visible run for each DAG Workflow ID,
so prior runs in a Continue-As-New chain are verified and deleted too.

`dag_retention_cli` adds bounded, owner-scoped History retention for completed DAGs. It defaults
to dry-run and only selects sealed graphs past the requested age with completed roots/nodes,
drained local processes and leases, and no unresolved lifecycle operations. `--apply` persists
a target digest before deleting histories and fences new lifecycle operations; RPC uncertainty
leaves a resumable pending intent. Local node results, worktrees and SQLite artifacts are kept.
Their local cleanup remains under the explicit TP18 retention/session-erasure flow.

```powershell
.\venv\Scripts\python.exe -m agent_execution.temporal_backend.dag_retention_cli --owner-hash <sha256> --days 30 --limit 100
.\venv\Scripts\python.exe -m agent_execution.temporal_backend.dag_retention_cli --owner-hash <sha256> --days 30 --limit 100 --apply
```

Per-artifact (2 MiB), total ledger/artifact storage (256 MiB), and operation-count
(100,000) limits remain enforced. An accumulated-capacity regression fills the ledger with prior
results, then confirms that a later result is fenced as uncertain without deleting earlier artifacts.
The isolated Service test lowers the History suggestion threshold, observes at least three runs
under the same Workflow ID, and verifies one child execution plus stable SQLite budget state across
rotation. A separate real-Service retention test confirms History and visibility deletion after
pending retries while preserving local node results. This operator-driven History cleanup does not
remove local results, worktrees, backups, or archived histories. Production load trends and canary
soak remain TP27 operational acceptance; TP23 controlled acceptance is complete.

Verification on 2026-10-28: with the local Temporal CLI 1.9.1 (embedded Server 1.32.0), the
Temporal backend suite passed **565 tests** with **4 skipped** and one existing Starlette/AnyIO
deprecation warning. The DAG Service/Worker module passed **15 tests**; the strengthened three-run
chain and accumulated-pressure test passed in the subsequent focused rerun. The remaining skipped
cases require separate provider or write-live opt-in. The final full venv run passed **1467 tests**,
with **83 skipped** and **95 subtests passed**; its only warning was the same existing deprecation.
`pip check`, `compileall`, and `git diff --check` passed.

## TP24: Workflow compatibility and rollback guard (isolated process drill complete)

V1/V2/V3 keep separate Workflow types and queues. Existing isolated Service tests generate and replay
Histories for each version; the TP14 legacy patch branch also replays under the current code. Dispatch
now has a regression proving that an unsupported persisted protocol version is rejected before
`StartWorkflow`, so a downgraded or mixed-version Worker cannot silently reinterpret it.

Checked-in, sanitized replay fixtures now cover V1, pre-TP14-patch V1, V2, and V3. An isolated
Service rollback drill starts a V3 approval Workflow while a V1 Worker polls only the old steps
queue; the V3 execution remains on its pinned approval queue, with no Activity claimed by V1.
Fixture replay passed **4 cases**, and the V3-on-old-Worker drill passed.

The process-level drill starts a V1 run with an old Worker subprocess, terminates that process after
its bootstrap Activity starts, then starts a replacement Worker subprocess on the same V1 queue. The
replacement consumes the Activity retry, completes the original submission, and the resulting History
replays with the current V1 definition. The run plus four fixed fixtures passed **5 focused cases**.
This is an isolated Service simulation using a test harness and controlled provider; it does not
install or downgrade a real deployment package, drain a production Worker fleet, or exercise production
traffic. Those deployment and canary gates remain in TP26–TP27.

### TP25 observability progress

Temporal Activities now create a redacted `workflow_activity` span after rebuilding the trusted task
scope. Existing task and model-attempt spans become descendants of that span, so the local trace store correlates
task owner/task ID, opaque submission/workflow IDs, queue, activity type, model attempt, latency and
error type. Trace metadata does not include prompts, provider responses, tool arguments, or credentials.
An isolated process-upgrade test checks the parent/trace relationship and verifies that private provider
evidence is absent from stored spans.

Use the existing operator diagnostics from the configured application environment:

```powershell
.\venv\Scripts\python.exe -m agent_execution.operations readiness
.\venv\Scripts\python.exe -m agent_execution.operations traces
```

`readiness` checks local dependencies, workspace writability, and configured SQLite accessibility; it
does not probe the model provider or prove Temporal Worker pollers are healthy. The authenticated
admin-only `/api/ops/readiness` and `/api/ops/traces` routes expose the same diagnostics. If readiness
fails, stop admitting new Temporal work, preserve configured DBs and Worker logs, verify the same
TaskStore paths/namespace, and restart only after storage/access checks pass. For a stuck run,
correlate task/submission state, Temporal UI History, queue, `workflow_activity` span, child model spans,
and operation ledger; classify unknown side effects as `uncertain` and do not replay them manually.

This completes trace correlation and local redaction evidence only. Exportable metrics, provider-level
correlation, configured alert delivery, and a live poller-aware readiness endpoint are still open TP25
work; generic local readiness must not be used as a production deployment gate.

### TP26 isolated backup/restore progress

`operations.backup` uses SQLite's backup API, verifies each source and restored database, hashes files,
and refuses to overwrite a restore target. The isolated drill stops the Worker and development Service,
backs up the TaskStore database and Temporal Service database, restores them into new paths, starts a
new Service from the restored database, then retrieves and replays the completed Workflow. Run it with:

```powershell
$env:PYTHONAI_TEMPORAL_TEST_CLI = 'C:\path\to\verified\temporal.exe'
.\venv\Scripts\python.exe -m pytest `
  agent_execution\temporal_backend\tests\test_integration.py::test_temporal_service_and_taskstore_backup_restore_drill -q
```

This passed against the isolated CLI development Service. The snapshot is per-database; workers and
Service were quiesced to avoid cross-database drift. A production inventory must also include approvals,
schedules, communication/EventStore, tenant/profile/memory data, artifacts, worktrees, configuration,
and secret references as applicable. Cloud/self-hosted selection, production Service-native backup,
restore permissions, RPO/RTO, and a real recovery environment remain unverified TP26 requirements.

Regenerate fixtures only from the controlled local Service tests:

```powershell
$env:PYTHONAI_TEMPORAL_TEST_CLI = 'C:\path\to\verified\temporal.exe'
$env:PYTHONAI_WRITE_TEMPORAL_REPLAY_FIXTURES = '1'
.\venv\Scripts\python.exe -m pytest `
  'agent_execution\temporal_backend\tests\test_step_integration.py::test_step_workflow_real_io_cached_projection_and_replay[False]' `
  'agent_execution\temporal_backend\tests\test_cancellation_integration.py::test_real_cancellation_reaps_descendants_and_fences_late_provider[remote_legacy]' `
  'agent_execution\temporal_backend\tests\test_human_integration.py::test_answer_survives_worker_restart_and_duplicate_signal[False]' `
  'agent_execution\temporal_backend\tests\test_approval_integration.py::test_restart_duplicate_notification_and_human_decision[approve]'
Remove-Item Env:PYTHONAI_WRITE_TEMPORAL_REPLAY_FIXTURES
.\venv\Scripts\python.exe -m pytest agent_execution\temporal_backend\tests\test_replay_fixtures.py -q
```


## 目前入口的限定接入驗收（TP11.7）

使用現有 `/ws/v1` 路由、真實本機 provider 與獨立 Worker 程序；所有 DB、owner、角色設定及
workspace 放在隔離目錄。只對指定試用 owner 開啟 approval/file-write 模式，不修改 `.env`。
新 Temporal 任務即使自動命中 executable skill，也走原 Agent steps；明確 skill 執行仍不支援。
已固定 SQLite 的任務及 Activity 內原執行器維持既有 skill 路徑。
每次審批等待由 Worker journal 發布 `approval.required`，不依賴原前端回合仍在訂閱；事件可去重及重連重播。

```powershell
$env:PYTHONAI_TEMPORAL_TEST_CLI = 'C:\path\to\verified\temporal.exe'
$env:PYTHONAI_TEMPORAL_ENTRY_LIVE = '1'
$trialRun = Join-Path 'temp' ('temporal-entry-live-' + [guid]::NewGuid().ToString('N'))
.\venv\Scripts\python.exe -m pytest agent_execution\temporal_backend\tests\test_entry_live.py -v -s --basetemp=$trialRun
Remove-Item Env:PYTHONAI_TEMPORAL_ENTRY_LIVE
```

此命令會呼叫現有 provider，root 上限 16 requests／128000 tokens／300 秒。
驗收管理員程式只核准七個指定子目錄、限定長度且含標記的 `llm-wiki/README.md`，以及這些目標的讀取；
其他路徑與命令一律拒絕。這是協定層審批驗收，不代表桌面 UI 的人工點擊已驗證。
第一次審批前重啟 Worker，核對 workflow ID／原預算不變；完成後再重啟 Worker、重連 socket，
驗證相同結果與重複 chat.send 去重、檔案內容／mtime 及 requests 不變，並重播 History。

2026-10-26 真實驗收 **7 passed**（6 審批範圍案例、1 完整 live 案例），125.89 秒。
live 案例完成七個子目錄及繁體中文 README，**4 requests、2 approvals、1 chat.delta、15 重連重播事件**；
等待審批與完成後各重啟一次 Worker，結果取回無新增請求或改寫產物。
紀錄：`temp/temporal-entry-live-20261026-final.log`，完整證據與產物保留在
`temp/temporal-entry-live-20261026-final/` 的 `workspace/.system_generated/{acceptance.json,frames.json}`
及 `workspace/llm-wiki/README.md`，均不入 Git。完整回歸與失敗診斷分列 `papers/temportal.md` 12.13。
此結果完成使用者確認的 TP11.7 產物範圍；未啟用日常服務、移轉舊任務或擴大到其他身分。

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
- 取消：既有 TaskStore cancellation 會被 Worker 的執行檢查攔下。斷線／client wait 結束只解除訂閱，明確取消才要求遠端取消；已發出的 provider 請求無法因此保證未收費。
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

## TP18：同機 erasure、retention 與 artifact GC

沿用人類入口的 `/session-delete preview`／`confirm <plan-id>`／`status|resume <plan-id>`，
以及原 workspace HTTP／communication 刪除流程；沒有新增模型可呼叫的刪除工具。
預覽另固定 Temporal submission ID 與狀態 digest，目標在確認前改變必須重新預覽。
仍需先取消／處理原 active task、lease、transport、schedule、memory worker 與 build blockers。

確認後，在原 lifecycle tombstone 的同一 TaskStore transaction 建立 erasure intent、
雜湊 task fence，將 submission 改為 `erasing`，並封鎖 started／unknown operation。
取消／關閉遠端 Workflow 不能解除此 fence。實際檔案清理前，僅從持久紀錄讀取可信
loopback address、namespace、host queue 與固定 Workflow ID：
session 刪除要求 cancel／terminate 原執行；一般 retention 則等待遠端自然關閉。
只對核對過的閉合 run 發出 DeleteWorkflowExecution，接著確認 History describe 與
visibility query 均查不到。服務離線、RPC 結果未知或索引尚未反映刪除時保留 pending，
原 frozen files／artifacts 仍在；恢復後接續同一 intent，不啟動原模型或工具。

Activity、StartWorkflow dispatch、publication／relay 與完成結果取回皆保存短期持久
reference。清理等待原程序放行；Windows 以 PID 加 process creation time 核對，
程序仍活著時不因時間過期而猜測安全。程序真的退出後可清除殘留 reference。
目前寫入 commit 的 fence 與取消檢查仍有效，晚到結果與 profile preparation cache 不能重新寫回 TaskStore。
所有參與程序應先更新到此版本；混合舊 Worker 的版本治理仍屬 TP24。

原 frozen filesystem 計畫完成後，erasure finalize 清除同 task 的 input/result、
artifacts、operation ledger、continuations、人工回答、審批／decision outbox、
stream events、file-write journal／人工觀測、profile preparation 與預算資料。
ApprovalStore 目標搬移會阻擋清理，跨 DB 中斷可重入。保留最小雜湊 task tombstone、
opaque submission/session ID、處置收據；原 task ID 不能再走 Temporal 或 legacy claim。
這些 fence 不自動到期／逐出；達容量上限會拒絕新增清理，需明確維運處理，
避免因清理 tombstone 讓舊請求復活。它們是最小去重紀錄，並非對身分匿名化的保證。

已經由原計畫完成的舊 session 刪除，可由新 Worker 依 completed operation／outbox
核對授權後補做 Temporal 清理，不復活 session。新 session 的 pending erasure
由 Worker reconcile；原檔案刪除計畫仍使用 `/session-delete resume <plan-id>` 接續。
刪除結果的 `temporal` 欄位及本機 `temporal_erased_tasks`／`temporal_erasures`
保存版本化處置收據：namespace retention 秒數、archival 設定、History／visibility
查無紀錄的時間。**archival 與備份一律標記 external_policy_required**；
此功能不刪外部歸檔／備份，也不宣稱磁碟、SQLite WAL 或儲存快照已經安全抹除。

一般 retention 預設只預覽，不新增背景 TTL 排程、不刪程式檔案／retained／共用目錄，
也不代替 conversation memory、identity profile 與 transport outbox 的各自留存政策。
可信本機 admin operator 可依 TP16 的 owner hash 指定範圍：

```powershell
.\venv\Scripts\python.exe -m agent_execution.temporal_backend.retention_cli --owner-hash <sha256> --days 30 --limit 100
.\venv\Scripts\python.exe -m agent_execution.temporal_backend.retention_cli --owner-hash <sha256> --days 30 --limit 100 --apply
```

`--days` 為 1..3650（預設 30），`--limit` 為 1..100。dry-run 使用 read-only SQLite，
不建表、不連 Temporal。apply 在寫入 transaction 中重選候選並固定 fence；
只接受到期 completed／failed／cancelled、沒有活躍 reference、unknown operation、
未完人工等待／profile extraction／finalization／可靠投遞的任務。
已消耗且 ledger completed 的 approved 操作可清理；未消耗審批仍保留。
CLI 回報 `pending` 表示尚未完成，重跑同一範圍或相同 DB／namespace 的 Worker
可接續；不會重做模型／工具或重新受理舊 task ID。
retention 不掃描刪除 staging orphan；這些檔案只隨原人類批准的 frozen temporary
workspace 刪除計畫處理，保留／共用目錄仍保留。

驗收（2026-10-23）：專案 venv 完整 pytest **1286 passed、12 skipped、95 subtests passed**，
1148.42 秒；新增 TP18 的 **35 項契約／單元測試及 3 項隔離真實 Service 測試**均通過。
Service 案例確認本機 Activity 仍存活時保留 frozen files／artifacts、晚到結果拒絕、
History／visibility 查無紀錄、服務離線後重啟接續，以及 retention CLI 的 pending／重入。
保留 retained 目錄與一般 retention 的程式檔案，舊 task ID 不復活，新 task 仍可受理。
跨 DB 中斷、程序直接退出、跨 owner、容量、舊刪除計畫補清理及錯誤 receipt 均有覆蓋。
pip check、compileall、diff check 通過；只有既有 Starlette／AnyIO deprecation warning。
紀錄為 `temp/temporal-tp18-full.log`、`temp/temporal-tp18-verified.log`、
`temp/temporal-tp18-retention-final.log`，不入 Git。真實 Service 使用隔離 loopback service、
DB 與 workspace；未修改正式設定、未操作使用者任務，未呼叫真實 LLM。
archival／備份／正式部署與非 Windows 破壞性清理仍需獨立外部驗收。

## TP17：審批綁定的同機檔案寫入

預設仍只讀。第一批僅開放 `create_directory`、`write_file`、`edit_file`；
前端仍需 `AGENT_EXECUTION_BACKEND=temporal`；另在前端與 Worker 的程序設定 `AGENT_TEMPORAL_ENGINE=approval`、
`AGENT_TEMPORAL_FILE_WRITES=1`，以及 TP16 顯式 owner allowlist。
只能由目前 admin 受理新任務，能力以整數版本 1 固定於 submission。
旗標關閉不升級／降級既有任務；停止新增可沿用 `AGENT_TEMPORAL_TRIAL_OWNERS=[]`。
沿用 V3 Workflow／queue，不修改正式設定或搬移任務。

每個檔案工具仍需人類逐次核准。審批綁定工具參數、目標／父路徑 identity、
檔案前態 hash／大小／mtime／mode，以及預期後態 digest。執行經原 policy dispatcher、
當前角色／allowlist／lifecycle／root budget 檢查及一次性 consume；
adapter 另外核對精確 consumed grant。變更目標或父目錄、撤權、取消與刪除 fence
均阻止後續寫入。內容使用 UTF-8 與本機換行；檔案必須位於獨立程式目錄，
拒絕絕對／逃逸／私有路徑、符號連結、junction、硬連結、Windows 保留名稱及 ADS。
上限為單檔 2 MiB、目錄批次 100 筆、路徑深度 32／長度 1024 字元、plan 64 KiB；
仍受原輸入、artifact、ledger 與總預算上限限制。

寫檔使用同目錄 staging、fsync、原子 replace，保留原檔案 mode；
沿用程式目錄登記與 build 生命周期。commit 前在 TaskStore 寫入 transaction 中再次
核對 fence／預算／角色及前態，寫入後核對實際結果並保存觀測，再由原 ledger
提交 result／evidence。**檔案系統與 SQLite 並非共同原子交易**；
consume 後、實際副作用後、ledger 提交前的中斷均保守停止為 unknown／uncertain，
不重新核發 grant、不盲重做。這也不是對惡意外部程序同時換路徑的原子隔離保證。
一般退出會刪除 staging；突然終止留下的 orphan 只隨 TP18 的已批准 frozen workspace 刪除計畫清理，一般 retention 不掃檔。

`/steps <task_id>` 對新寫入任務回傳 steps 與 file_writes，列出 operation ID、
工具、路徑、effect_observed 及 human_observation。相同 owner 的目前 admin 可核對
已 fenced 的 uncertain／cancelled／failed 操作，使用：

```text
/resolve <task_id> <operation_id> {"status":"matches_expected","note":"人工核對檔案內容與預期一致"}
/resolve <task_id> <operation_id> {"status":"matches_before","note":"人工核對仍與寫入前一致"}
```

系統重新讀取實際檔案並核對宣告，保存不可改寫的 actor／session／plan digest／觀測。
JSON 上限 16 KiB、note 1..4000 字元。這是當下檔案觀測，不能證明過去從未產生
副作用；不改任務終態、不重置預算、不續行，也不代表產品驗收完成。
`temporal_file_writes`、`temporal_write_resolutions` 留於本機可信 DB，計入 ledger 容量。

既有 TaskEvaluator／程式測試要求／reviewer／approval 仍有效。
shell、測試命令、刪除、Git 與外部服務寫入未開放；
需要執行程式測試的 code task 不能僅憑寫檔成功宣稱驗收通過。
Docker、桌面、跨平台及正式部署仍需另行驗收。

驗收（2026-10-23）：

- 真實 Service／Worker、受控 provider：正常完成、consume 後失聯、檔案 replace 後結果未提交、
  projection 回覆遺失、持久串流審批／finalization；Worker 重啟、History replay、
  實際檔案與請求次數／原 deadline 不變。修正前端審批／人工回答等待事件使用舊 fence 的問題。
- 隔離子 Python 程序於實際檔案寫入後 `os._exit(73)`，原操作保持 running／uncertain、
  grant consumed，拒絕重放；人工觀測保存後仍不恢復。
- 真實 provider 沿用既有設定：隔離 llm-wiki 的 7 個子目錄與 README 確實建立，
  **1 passed，18.22 秒，3 筆模型／驗收請求、2 次精確審批**；完成結果取回、
  History replay 與預算不變通過。未執行使用者正式 wiki 任務；
  TP11.7 原始長任務完整產物補驗見「目前入口的限定接入驗收」。
- 專案 venv 完整 pytest **1248 passed、12 skipped、95 subtests passed**，1446.15 秒；
  pip check、compileall、diff check 通過。只有既有 Starlette／AnyIO deprecation warning。
  真實 provider 測試預設 skip，以上獨立 live 結果分開記錄；最終紀錄為
  `temp/temporal-tp17-full.log` 與 `temp/temporal-tp17-live.log`。live 預設獨立 opt-in：
  `PYTHONAI_TEMPORAL_WRITE_LIVE=1` 配合測試 CLI 執行
  `tests/test_file_writes_live.py`。隔離 log 位於 `temp/temporal-tp17-*.log`，不入 Git。

先設定已驗證的測試 CLI 與本機 provider，再單獨執行 live 驗收：

```powershell
$env:PYTHONAI_TEMPORAL_WRITE_LIVE = '1'
.\venv\Scripts\python.exe -m pytest -v -s agent_execution\temporal_backend\tests\test_file_writes_live.py
Remove-Item Env:PYTHONAI_TEMPORAL_WRITE_LIVE
```

## TP16：限定 owner 的封閉唯讀試用

`AGENT_EXECUTION_BACKEND=temporal` 仍須顯式設定。可再設定
`AGENT_TEMPORAL_TRIAL_OWNERS` 為最多 1024 個唯一、小寫 SHA-256 的 JSON 陣列，
將新任務限制在可信的 source／identity／project／session。未設定保持原有顯式 opt-in
相容；`[]` 停止新增 Temporal 任務。未知格式、空字串、萬用字元或重複 key 均拒絕受理，
不以設定失敗回退執行。這不是角色授權，不能擴充唯讀工具或繞過目前權限。

例如為已認證的 WebSocket 身分／裝置計算範圍（不讀取 DB、不開啟連線）：

```powershell
$trialOwnerKeys = .\venv\Scripts\python.exe -c "import json; from agent_execution.policy import ExecutionContext; from agent_execution.tasks import task_owner; from agent_execution.temporal_backend.admission import owner_key; print(json.dumps([owner_key(task_owner(ExecutionContext(source='websocket',identity='websocket:alice',project='default',session='default:device')))]))"
$env:AGENT_TEMPORAL_TRIAL_OWNERS = $trialOwnerKeys
.\venv\Scripts\python.exe -m agent_execution.temporal_backend.preflight
```

preflight 僅顯示模式與數量，不顯示身分或 allowlist，也不建立 DB。設定要來自受信任的
部署程序，不能從訊息 payload 取得。未納入的新任務維持原 SQLite 功能；這個清單不是
整個產品的寫入封鎖。原生 WebSocket 與 profile guard 共用任務後端判定，排除任務的
斷線取消仍為原 SQLite 行為，且不建立可選 Temporal schema／profile ledger。

已存在的 binding 或 legacy task 優先於新受理政策：關閉試用、清空 allowlist 或修改新任務
預設均不改派已受理任務。暫停新受理不會取消在途工作，需明確使用 TP14 取消入口。
Worker 仍以已存的 scope、當前角色、原 root budget 與 workspace 重建執行權威。
StartWorkflow／RPC 結果未知不回退 SQLite，不換 ID 或補充配額。

隔離 E2E 位於 `tests/test_trial_integration.py`，使用真實 loopback Service／SDK／Worker。
受控模型刻意重複要求讀取，驗證跨 Activity 的 turn-control 狀態、讀取重用、History replay、
新 Worker 取回完成結果，以及停收後 backend／deadline／request 計數不變。
wiki 建立要求在工具 I/O 前被唯讀權限拒絕；沿用既有保守 `uncertain` 停止並拒絕手動
重跑，不宣稱已建立 wiki。受控 HarnessRouter 案例使用明確標示的測試來源，與真實 URL
／provider 驗收分開。

獨立執行真實 provider（沿用本機設定，小預算／隔離 workspace，不改正式使用者產物）：

```powershell
$env:PYTHONAI_TEMPORAL_TEST_CLI = 'C:\path\to\verified\temporal.exe'
$env:PYTHONAI_TEMPORAL_LIVE = '1'
.\venv\Scripts\python.exe -m pytest agent_execution\temporal_backend\tests\test_trial_integration.py -k live -v -s
Remove-Item Env:PYTHONAI_TEMPORAL_LIVE
```

每例固定 8 筆 request／75 秒 root deadline，單次 provider timeout 15 秒、SDK 隱藏 retry
為零；runner 的既有有限 retry／backoff 仍受根 deadline 約束。預設完整測試跳過這兩項
外部驗收，不能把 skip 或有界逾時當成內容完成。

TP16 本機驗收（2026-10-22）：最終專案 venv 完整 pytest **1201 passed、11 skipped、95 subtests passed**，
971.86 秒，紀錄 `temp/temporal-tp16-full-final.log`；包含上述兩個受控 E2E，新增兩個
skip 為獨立真實 provider 案例。index／allowlist／WebSocket 回歸 **60 passed、5 subtests passed**，
31.15 秒；pip check、compileall、diff check 通過。首次完整測試發現既有 LSP 取消測試的
0.5 秒時鐘早於前置 hook，可能在進入 LSP 前就被正確拒絕；改由 LSP 程序呼叫開始計時，
並斷言確實呼叫，最終完整重跑無失敗。正式取消邏輯與允許的例外均未放寬。

2026-10-22 初次外部紀錄（後續補驗見下文）：獨立 15 秒探測為 `APITimeoutError`，1 筆請求，無 HTTP status。
兩個真實案例各 3 筆請求，均由 deadline 取消，0 次工具執行、沒有 wiki 產物。
live 測試 **1 passed（wiki 停止安全性）、1 failed（HarnessRouter 未完成內容）**，
159.17 秒；紀錄 `temp/temporal-tp16-provider-probe.log`、
`temp/temporal-tp16-trial-live.log`。額外唯讀診斷的 TCP 連線為 `TimeoutError`，`/models` 為 `APITimeoutError → ConnectTimeout`，
未取得 HTTP 回應，紀錄 `temp/temporal-tp16-provider-diagnostic.log`；尚不能定位遠端服務
或網路設備的根因。這不證明 provider 可用，也未完成真實內容驗收。
初次連線失敗時曾保留 TP16.5／TP11 外部驗收待辦，以下為連線恢復後的獨立補驗。

2026-10-22 補驗：沿用現有設定，TCP 連線與 `/models` 成功，設定模型在清單中；
紀錄 `temp/temporal-tp16-provider-recheck.log`。專案 venv 執行原 opt-in live 測試
**2 passed、2 deselected**，44.27 秒，紀錄 `temp/temporal-tp16-trial-live-recheck.log`。
這次使用真實 provider 與真實 HarnessRouter URL，沒有替換模型回應或來源內容：

- wiki：1 筆模型請求，寫入要求在工具 I/O 前被唯讀權限拒絕；沿用 `uncertain`
  停止，0 次工具執行、沒有 wiki 產物，禁止手動重跑未知狀態。
- HarnessRouter：任務 completed、回覆與實際工具結果均含 UHP，3 筆含驗收的請求，
  `fetch_url` 實際執行 1 次。新 Worker 在停止新受理後取回相同結果，不增加請求／抓取。
- 兩個 History replay 均通過，History 不含原始 goal；重啟後的 submission ID、
  request 計數與原 deadline 不變，未知狀態仍拒絕恢復，不回退 SQLite。

TP16.5 與 TP16 的封閉唯讀驗收完成；不將 wiki 的權限拒絕宣稱為建立成功。
TP11.7 原始長任務完整產物已由現有入口補驗，見上述限定接入章節；TP17 首批檔案寫入驗收見上節，正式部署留 TP26、放量留 TP27。
本次僅更新文件，程式版本沿用上列完整回歸結果；未重跑全套、未改 provider 設定或正式資料。

## TP15：持久串流與 finalization

新受理且由 AgentRuntime 啟動的 Temporal 任務保存 `publication.version=1` 與可信的
memory／episode／原 delivery references；沿用 bridge／V1／V2／V3，不增加 Workflow
或啟動入口。原 `.env` 預設、純 SQLite、自訂非 MemoryStore runtime，以及沒有這個
binding 的舊任務維持原路徑。所有 callback、DB connections 與事件本文只留在執行端。

Worker callback 先寫同 TaskStore 的 `temporal_events`，固定 submission／step／事件 ID
及單調序號；同一 key 不允許換內容。model.delta 約 2048 字元或 100ms 合併，工具／驗收
事件保留原事件名稱；已提交的 frames 可重播，未提交的記憶體尾端以最終完整結果為準。
Workflow History 不保存 tokens 或角色／路徑／私有輸出。事件追加重查 owner lifecycle、
submission fence 與取消狀態；取消／uncertain 只能發布固定狀態通知，不能發布晚到成功。

WebSocket 仍使用原 seq／ACK；Temporal frames 包含 `task_id`、`temporal_event_id`，
包含 chat.delta 的所有這類 frame 都可靠投遞。EventStore 保存 identity tombstone，ACK／
原事件 TTL 到期後也不重新分配 seq；不同內容重用同 ID 拒絕。Worker outbox 複製成功後
才 ack journal，兩庫之間中斷可重送。原生連線 pump 與即時 callback 共用可靠序列；
握手 replay 完成前不插入 live frame，斷線／client-loop 關閉只解除訂閱，明確 interrupt、
`/cancel-task` 與 deadline 仍走 TP14。重新連線會取回原任務事件，不重開模型。

communication／Telegram 沿用 CommunicationStore.queue_delivery；等待與最終通知使用
同一事件 ID，前端回覆及 Worker reconcile 不會排入兩筆。平台 outbox 的 identity marker
不隨 delivery TTL 消失；idle command polling 也會發送已排入的結果。傳送結果未知仍為
uncertain，不自動重送，不以新模型回合修復投遞。provider callbacks／adapters 不進 History。

最終結果與 `temporal_finalizations(task,version)` intent 同 TaskStore transaction 提交。
Memory DB 以相同 finalization key 在一筆交易完成 assistant event、episode status、唯一
memory_review job；Memory DB 已 commit 但 TaskStore ack 遺失時，只取回原 job ID。
不同 DB 不假裝原子；暫時失敗保留 pending，由 metadata retry／同機 Worker reconcile
續作，不能重新呼叫模型。已註冊的背景記憶 worker 會輪詢 durable pending jobs；若記憶
服務未執行，工作保留至之後載入／維護。finalize 不直接做遠端萃取、不再消耗 root requests。
Memory 目標搬移、episode 已刪除、owner 已刪除均拒絕處理；本節證明 finalization 去重，TP18 erasure／retention 見上節。

profile 萃取仍在原人類輸入階段，沿用身分隔離、secret filter 與版本化 apply；以 task／
輸入 digest 先預留，已完成取回結果，執行結果未確認不自動再抽取。明確 `/profile` 命令
維持原行為。重複取得完成結果不新建 episode、不重複記錄 assistant 或排入 review。

| 限額 | 預設 |
| --- | --- |
| AGENT_TEMPORAL_EVENT_LIMIT | 100000 frames |
| AGENT_TEMPORAL_EVENT_BYTES | 64 MiB（含保留 metadata） |
| 單 frame | 2 MiB |
| COMPANION_EVENT_KEY_LIMIT／COMM_DELIVERY_KEY_LIMIT | 各 100000 identity markers |

既有 EventStore／communication outbox 容量也仍有效。transport 已滿保留 journal 等 ACK／
恢復，不重做模型；journal 已滿則停止追加，不繞過容量或對未知效果重跑。新的 journal／
identity markers 保留在原本機 DB 作最小去重；TP18 接入 session 內容清除與 execution retention，transport 身分 marker 仍沿用各自政策。這仍限同機共享資料權威，
不表示已完成遠端部署、真實 provider／桌面實機或各平台訊息送達驗收。

TP15 驗收（2026-10-22）：專案 venv 完整 pytest **1174 passed、9 skipped、95 subtests passed**，
1029.94 秒，紀錄 `temp/temporal-tp15-full-final.log`；僅一項既有 Starlette／AnyIO 棄用警告。
另串行 TP14／TP15 真實 SDK 整合 **9 passed**，含 History replay；runtime／memory／stream
回歸 **49 passed、1 skipped**，預算／communication 回歸 **34 passed**。原生 WebSocket
reconnect／ACK、兩庫提交後中斷、重複 resume、容量滿載恢復、身分與取消 fence 均通過。
首次完整測試發現既有 admission 測試的 Mock 未提供 idle outbox 輪詢清單，已修正並新增
忙碌時仍輪詢的斷言，最終完整重跑無失敗。pip check、compileall 與 diff check 通過。
上述真實 SDK／Service 測試採受控模型；communication 使用測試 adapters，未據此宣稱
真實 provider／Telegram 送達／桌面／Docker／正式部署通過。

## TP14：取消與執行清理

既有 `/cancel`、session clear、TaskStore.cancel／cancel_session 與 Store.cancel 會在同一
TaskStore transaction 更新任務、submission、operation fence 與 `temporal_cancellations`。
紀錄只含 submission ID 與投遞狀態，一個 submission 最多一筆；不增加取消入口。
舊 cancelled submissions 在 optional Store 升級時補建取消紀錄；純 SQLite 任務取消不
建立 Temporal schema，也不載入 SDK。已完成結果不因晚到取消改標。

Worker 每輪 reconcile 先重送本 host queues／address／namespace 的 pending 取消；
不依賴 active 清單，因此本機已 cancelled 或 Worker 重啟也不遺失意圖。RPC 限時 10 秒，
未知結果保留重試，只有成功或 NOT_FOUND 才 ack；不使用 signal-with-start 或重開任務。
先提交本機 fence，再撤銷待用審批及通知遠端。取消與完成競賽可能使服務端先成為 FAILED，
本機已提交的 cancelled 與 ledger uncertain 不被這個晚到狀態覆寫。

bridge／V1／V2／V3 共用 10 秒 heartbeat monitor，heartbeat timeout 維持 45 秒。
monitor 複製 Activity context，結束會停止、join 並還原 ContextVar。heartbeat API 發生例外
後，合作式 checker 拋出 ActivityLeaseLost／OperationUncertain；這和使用者取消分開，
不能當成「無副作用」或直接重跑。SDK heartbeat 有內部傳送與節流機制；本地呼叫成功不代表
網路服務端已確認，不能宣稱拔網路後 10 秒內一定停止。

同步 Activities 使用 `no_thread_cancel_exception=True`，以 cancellation checker 停止 I/O，
避免 SDK 在 SQLite transaction 或程序 finally 清理途中注入 thread exception。SDK 取消
仍回報 CancelledError；Workflow 用 `tp14-propagate-cancellation-v1` replay patch 避免
將取消誤當可 recover 的 Activity 故障。舊 History 仍保留原 recovery 路徑。
參見 [SDK cancellation 說明](https://github.com/temporalio/sdk-python#cancellation)。

run_process 沿用 Windows suspended launch + Job Object／POSIX process group，取消、
heartbeat 失效、timeout 或 checker 例外都會進 finally 清理程序樹；測試確認真實 descendant
已結束。這不能強制撤回已送出的遠端 HTTP／模型請求；不合作的同步程式仍需等待其自身
timeout，未知效果保留 uncertain，late result 不發布，root budget 不重置。
目前同機唯讀產品流程沒有 Child Workflow；DAG 父子流程及 Parent Close Policy 的實作／
孤兒清理由 TP21–22 交付；TP17 首批檔案工具不啟動命令，Docker 實機子程序樹終止仍未驗收。

TP14 專項驗證（2026-10-21）：取消 outbox 契約 14 項、liveness／程序清理 5 項、
真實 SDK／Service／Worker 整合 5 項通過。包括舊取消 History（保留 recover）的相容 replay、
新取消 History 不排入 recover，以及 Windows 隔離 Python runner 程序直接終止時的 Job Object 清理；後者不是整個正式 Worker 部署的崩潰演練。
Temporal 全模組／process／communication cancel 回歸 **226 passed**，348.92 秒；
另補跑取消 outbox、隔離 runner 退出與舊 History 專項，完整回歸統一覆蓋所有案例。
完整專案 venv pytest **1132 passed、9 skipped、95 subtests passed**，1061.31 秒；
pip check、compileall、diff check 通過。一項既有 Starlette／AnyIO deprecation warning；
跳過案例及受控 provider 結果不作真實 LLM／瀏覽器／非 Windows／Docker 實機通過證據。
完整紀錄 `temp/temporal-tp14-full.log`。
紀錄在 `temp/temporal-tp14-targeted.log`、`temp/temporal-tp14-outbox-final.log`、
`temp/temporal-tp14-process.log`、`temp/temporal-tp14-legacy-replay-final.log`（不入 Git）。

## TP13.5–13.6：操作審批整合

新任務設定 `AGENT_TEMPORAL_ENGINE=approval` 時固定綁定 `AgentTaskWorkflowV3` 與
`-steps-readonly-v3` queue。此模式對模型要求的每個唯讀工具先請求審批，`ask_user`
仍使用 V2 的文字回答流程；預設不開放寫入，TP17 需另行顯式受理。skill、scheduler 與 coordinator 仍未開放。現有 bridge、
steps、human 任務不改派，新 Worker 同時服務四種 queue，並共用 host 併發限制。

審批在工具的 PRE hooks／I/O 之前建立。問題透過原 suspended 回覆顯示工具與參數、
`/approve <id>`／`/deny <id>`；`/approvals` 可檢視完整參數。只有現有已認證的人類
審批入口能決定，admin、identity、session 邊界維持原規則。決定後 Worker 會續行／停止
同一 Workflow；可用 `/resume <task_id>` 取回結果。文字「approved」或模型提供的
approval ID 不能核准操作。原桌面授權 API 仍使用 ApprovalStore.decide，並補送原 `approval.required` 可靠事件顯示卡片；
WebSocket 以原 `approval.decide` 協定決定，CLI／Telegram 沿用命令。TP13 交付當時未包含
TP15；目前的串流／最終通知整合見上方 TP15 節。

TaskStore 的 `temporal_approval_waits` 先保存不可變 intent，再於 approval DB 以唯一
binding 建立 request；決定早到或兩庫間中斷後重試會取回同一 request，不產生新 grant
或延長期限。binding 包含 host queue、submission、owner、task、operation ID；授權
同時固定 project/action 與工具／參數 digest。ApprovalStore 的決定和
`temporal_approval_outbox` 同 transaction 提交；不同 SQLite 檔案不假裝為原子交易。

Signal 只有 submission/event ID。RPC 結果未知保留 outbox，重送以 event ID 去重，
不用 signal-with-start。Activity 查原資料庫的 pending／approved／denied／expired／
cancelled 狀態；等待採 Timer，不占 Activity thread。核准後工具仍走原政策；若 PRE hook
改變工具或參數，原 grant 不可授權修改後的操作。執行前重查當前角色、路徑、allowlist、
lifecycle、budget、ledger 與 exact binding，然後一次性 consume。消耗後中斷保持
uncertain，不重新發放授權，不重跑未知工具；完成結果重取不再 consume。

Approval DB 的路徑也持久化綁定，新 Worker 設定不符會拒絕；前端與 Worker 必須使用
相同 `AGENT_TASK_DB`／`AGENT_APPROVAL_DB`。TTL 仍受原 300 秒／root deadline 約束，
停機也計時。拒絕／過期終止此任務，不要求模型換參數反覆重試。取消先提交 TaskStore
fence，再盡力撤銷 grant；即使 approval DB 暫時不可用，原任務也不能消耗或執行。
V3 排空前須保留支援 V3 的 Worker；回滾新任務旗標不遷移存量工作。

TP13.5–13.6 驗證（2026-10-21）：35 項契約／單元測試、6 項真實 SDK／Service／Worker
測試通過，包含重啟、重複事件、TTL、取消、consume 後故障、人工回答接審批與 V3
History replay。模型回應使用受控 provider；桌面卡片的 runtime callback 已有回歸測試，
尚未新增桌面實機或真實 LLM 成功驗收。專案 venv 完整 pytest **1107 passed、9 skipped、
95 subtests passed**，1069.19 秒；一項既有 Starlette／AnyIO deprecation warning。
pip check、compileall、diff check 通過。紀錄 `temp/temporal-tp13-approval-full.log` 與
`temp/temporal-tp13-approval-notify-final.log`（不入 Git）。未修改正式 `.env` 或遷移使用者資料。

## TP13：人工回答（前輪交付）

設定 `AGENT_TEMPORAL_ENGINE=human` 的新任務綁定 `AgentTaskWorkflowV2`，可使用 `ask_user`。
V2 使用獨立 `-steps-readonly-v2` queue，舊 Worker 不會誤接新流程；新 Worker 同時服務
M0／steps V1／human V2，並共用 host 併發限制。V2 任務排空前不能將 Worker 降回舊版本。
問題仍由原有入口
回覆；直接輸入文字回答會接回原 Workflow，`/resume <task_id>` 在尚無新回答時顯示
原問題。未確認副作用的 uncertain 任務仍拒絕自動恢復。原本 bridge 任務不改派，
已綁 V2 的任務也不因預設 engine 切回 bridge 而變更流程。

`waiting_user` checkpoint 確認完成後，同 transaction 保存 human cursor 與等待紀錄。
`temporal_human_answers` 位於同一 TaskStore DB；回答與 notification outbox 原子提交。
通知只含 submission ID，沒有問題、答案或 owner。Worker 每輪 reconcile 重送尚未
確認送達的通知，結果未知不清除 outbox；不用 signal-with-start 復活已關閉流程。
Workflow 的 Signal 只喚醒 metadata Activity，後者核對 cursor、owner、call ID、
lifecycle、fence 與 TTL，再將回答消耗、checkpoint 與新 cursor 一次提交。

等待不占 Activity thread／host slot／模型槽位，採 Temporal Timer 與 Signal；
metadata Activity 最多三次，model/tool/evaluation 仍最多一次。TTL 為等待建立時的
300 秒與原 root deadline 的較早者，停機也計時，消耗時過期就終止，不延長舊額度。
即使答案已受理，Worker 停機直到期限後才消耗，仍不得執行後續工具。根 Workflow
deadline 本身到期時沿用 TP12 的保守終止映射。

答案最多 1 MiB 且只接受文字；shared ledger storage limit 也計入答案與等待 metadata。
相同答案重送不增加紀錄，變更已決定答案／跨 owner／錯 call ID／取消／刪除中的
session 均拒絕。多工具回合在 `ask_user` 後立即保存等待，剩餘工具在回答後續行。
串流與記憶／profile finalization 去重已由 TP15 接通，範圍見上方 TP15 節。

前輪僅完成人工回答；本輪接續的 ApprovalStore decision outbox、授權通知、撤權重查與
一次性 consume 故障矩陣見上節。寫入工具仍受 TP17 gate 限制。

TP13 人工回答專項驗證（2026-10-20）：Temporal 全模組 **149 passed**，228.63 秒，
已啟用真實 development Service／Worker 測試。新增 18 個單元／契約案例與 3 個真實
服務案例，涵蓋既有文字入口回答、等待中 Worker 重啟、早到／重複通知、回覆遺失、
TTL Timer、單次消耗、跨 owner、舊回答不重用、session 刪除、取消與 V2 queue 隔離。
V1／V2 History replay 通過，History 沒有問題／答案／檔案明文；模型使用受控回應，
TP11 真實 provider 與原始長任務驗收仍未完成。
測試服務／執行資料置於 `.system_generated`，避免 SQLite journals 被驗收器視為
任務產物。紀錄 `temp/temporal-tp13-v2-final.log`；未啟用正式設定或遷移正式資料。

最終完整 pytest **1065 passed、9 skipped、95 subtests passed**，1118.53 秒，
真實 Service／Worker lane 已啟用；僅有既有 Starlette／AnyIO deprecation warning。
pip check、compileall、diff check 通過。完整紀錄 `temp/temporal-tp13-full.log`。

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

人工回答／審批等待已於 TP13 接通，共用取消與現有唯讀程序清理由 TP14 擴充；
子 Workflow 傳播留 TP21–22；TP17 首批檔案寫入故障矩陣見上節，Docker 實機終止仍未驗收。TP12 不將正在執行的外部請求宣稱為可強制撤回；它只停止後續步驟並拒絕
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
同機刪除／execution retention 已由 TP18 接入，詳見上節；TP29 多機仍未交付。資料仍屬同機可信儲存，應納入原 DB 備份與存取控制。

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
