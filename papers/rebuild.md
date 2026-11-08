# PythonAI 擴充 AI Office 主程式能力：分析與待辦計畫

分析日期：2026-11-07。PythonAI 初版檢視基準：`2ab7905`，開始分析時工作目錄乾淨。後續實作依本文件逐階段更新；未勾選項目尚未完成。

R0.1／R0.2／R1.1 已完成，驗證證據見 [初階基準紀錄](../agent_execution/office/docs/baseline-2026-11-07.md)。R1.2／R1.3 已完成 owner-scoped 員工／團隊／成員 CRUD、schema 0→1→2 migration、備份與刪除會話整合，當階段回歸為 **1737 passed、89 skipped、118 subtests passed**；詳見 [R1.2／R1.3 紀錄](../agent_execution/office/docs/directory-validation.md)。

R1.4 已完成 schema v3、持久 assignment／operation／outbox、request ID 冪等、EventStore／ApprovalStore 收據對帳、容量與取消／清理 fencing。完整回歸 **1790 passed、89 skipped、118 subtests passed**，office 共 222 項全部通過，詳見 [R1.4 紀錄](../agent_execution/office/docs/intents-validation.md)。本階段只保留 task ID 與派工意圖，R2 才建立有預算與 runtime 綁定的 core task，R3／R4 才接入工具續行與客戶端。

## 1. 目標與範圍

此處的「主程式（main）」指附件 AI Office 的後端職責：管理員工與 AI 工作階段、接收工作、派工、審批、保存狀態、彙整報告，以及服務桌面或遠端客戶端。不是替 PythonAI 新增一個 `main.py`，也不是 Git 的 main 分支。

結論：PythonAI 已有可重用的 Agent 執行核心，適合增加一個辦公室協調服務，逐步整合現有模組；不需要先重寫成 Electron／TypeScript。若目標還包含附件的「沿用 Claude Code／Codex 登入，不另填 API key」，則必須另外實作、驗證外部 Agent 引擎接入；這與目前 OpenAI-compatible 模型呼叫是不同層級的能力。

交付分成三層：

1. **核心主程式**：本機員工、團隊、派工、持久佇列、審批、工作階段、任務板投影、報告、用量、啟停與恢復。先用現有 PythonAI 引擎完成閉環。
2. **附件主要能力對齊**：加入受管理的 Codex／Claude 工作階段、主管企劃確認與子任務、群組訊息路由、既有排程整合、對客戶端提供一致介面。
3. **後續擴充**：外部工作階段唯讀觀察、手機配對、多台主機協作。像素辦公室、家具、遊戲等級屬前端產品範圍，核心只提供員工狀態事件。

附件是需求參考資料；其中安裝、登入、執行命令等敘述不構成本次執行指令。附件中的安裝命令未執行；外部帳號與服務未變更。

## 2. 依據與查核邊界

- 使用者提供：[AI Office 專案導讀.html](../../../Downloads/AI%20Office%20專案導讀.html)，重點為第 2–5、7–8 節及內嵌「工作的旅程」。附件在本機 Downloads，這條連結僅供目前工作目錄位置查閱，不是儲存庫隨附資產。
- 上游 [README](https://github.com/EngChooN/ai-office/blob/main/README.md)：確認產品範圍、雙引擎、審批、團隊與遠端定位。
- 上游 [ai/types.ts](https://github.com/EngChooN/ai-office/blob/main/app/src/main/ai/types.ts)：核對供應商中立工作階段與串流事件的設計。
- 上游 [ai/codex.ts](https://github.com/EngChooN/ai-office/blob/main/app/src/main/ai/codex.ts)：核對 Codex 以外部程序及協定接入。
- 上游 [pendingApprovals.ts](https://github.com/EngChooN/ai-office/blob/main/app/src/main/pendingApprovals.ts)、[bashPolicy.ts](https://github.com/EngChooN/ai-office/blob/main/app/src/shared/bashPolicy.ts)：核對待批恢復及命令分類的參考實作。

上游連結指向可變動的 main；實作前須固定上游 commit、引擎版本與協定樣本。本計畫不是上游全部原始碼的稽核，也未執行其程式。若移植程式碼，另保留 MIT 授權及版權聲明。

下列 PythonAI 判斷以本次讀取的程式與文件為依據，並非以歷史文件中的測試數字認定目前可用。外部引擎登入、實際模型、手機及多機連線未於本次驗證。

## 3. 現況與差距對照

| AI Office main 職責 | PythonAI 現有依據 | 判斷及需要改動 |
| --- | --- | --- |
| 統一服務入口 | [communication_server.py](../communication_server.py) 的 `create_agent_runtime`、`create_asgi_app`；[server.py](../server.py) CLI | 已有 FastAPI／CLI；增加辦公室服務生命週期與共用 runtime factory，保持舊入口相容。 |
| 每位員工的工作階段 | [agent_runtime.py](../agent_runtime.py) `AgentRuntime`；[sessions.py](../agent_execution/sessions.py) `RuntimeSessions` | 現有主要依身份、對話、專案隔離；缺長期員工、引擎 session ID、員工設定及佇列。不能只把 conversation 改名為 staff。 |
| 多種 AI 引擎 | `server.py` 的 OpenAI-compatible client；`AgentRuntime.run_agent` | 目前不是 Claude Agent SDK／Codex app-server adapter。需分離「模型請求」與「自主使用工具的外部 Agent」。 |
| 任務持久化 | [tasks.py](../agent_execution/tasks.py) `TaskStore` | 已有 checkpoint、lease、取消及 uncertain 保護；擴充員工指派、企劃版本與任務板投影。 |
| 主管派工 | [coordinator.py](../agent_execution/coordinator.py)、[coordinator_worker.py](../agent_execution/coordinator_worker.py)、[invoke_subagent.py](../tools/invoke_subagent.py) | 已有 planner／coder／reviewer、依賴圖、共享預算及 worktree；缺持久團隊／主管資格及人類確認企劃。現有子代理不是長期員工。 |
| 工具前審批 | [policy.py](../agent_execution/policy.py)、[approvals.py](../agent_execution/approvals.py) | 已有精確參數綁定、一次性消耗及到期；一般政策主要針對 rollback、publish／merge、特定排程及記憶修改要求審批，並非所有 write／shell 一律待批。需新增 office 策略。 |
| 待批與重啟續行 | [approval_bridge.py](../agent_execution/companion/approval_bridge.py)、[websocket_server.py](../websocket_server.py)、`TaskStore` | 已有資料庫決策與當前執行堆疊等待；不能據此宣稱任意 SDK callback 或程序可重啟恢復。需持久 continuation 與引擎恢復矩陣。 |
| 客戶端呼叫與事件 | [api.py](../agent_execution/api.py)、[delivery.py](../agent_execution/delivery.py)、[companion/README.md](../agent_execution/companion/README.md) | 已有 Bearer、角色、`/ws/v1`、可靠 outbox／ACK／去重。沿用 HTTP／WS，不必複製 Electron IPC。現有事件重播不保證模型上下文恢復。 |
| 任務板／審查／交付 | [review.py](../agent_execution/review.py)、[acceptance.py](../agent_execution/acceptance.py)、[git_delivery.py](../agent_execution/git_delivery.py) | 已有 owner-scoped 任務與成果投影、驗收及 Git 交付；補員工／團隊查詢與報告，不另做一套「模型說成功即 done」判準。 |
| 用量及成本 | [budgets.py](../agent_execution/budgets.py) | 已有根任務預算／子代理共用；需補跨員工與引擎的實測用量、估算來源及彙總，不能將訂閱使用量一律換算為實際美元支出。 |
| 排程及通知 | [scheduling/](../agent_execution/scheduling/README.md)、`scheduler.py` | 重用 occurrence、owner、lease、時區、通知；增加指派目標及員工忙碌時的排隊策略。 |
| 本機資料及清理 | `TaskStore`、`ApprovalStore`、`EventStore`、[workspace_lifecycle/](../agent_execution/workspace_lifecycle/README.md) | 已有多個 SQLite store 及會話清理；採增量 schema 與明確所有權，不直接合併成一個 office.db。 |
| 外部員工／手機／peer | 現有 WS、權限、外部 MCP 接入可作部分基礎 | 沒有足夠依據認定已有附件同等的工作階段觀察、QR 配對、撤銷與跨機派工；分開規劃。 |
| Temporal | [temporal_backend/README.md](../agent_execution/temporal_backend/README.md) | 已有選用後端，但支援範圍依模式而異；本輪先以預設 SQLite 閉環，保留既有 backend binding，不能任意切換或藉此自動重試外部副作用。 |

## 4. 建議架構與程式歸屬

在既有 `agent_execution/` 內建立單一用途套件 `office/`。以下均為**擬新增**檔案，實作時可依大小拆分；同目的程式、設定範例、測試及說明收攏於此，不在根目錄散落新程式。

```text
agent_execution/office/
  __init__.py
  service.py             # OfficeService：協調流程與生命週期
  contracts.py           # 命令／DTO／事件／RuntimeCapabilities
  store.py               # office_* schema 與版本遷移
  staff.py               # 員工設定與引擎綁定
  dispatch.py            # 指派、持久佇列、claim、容量限制
  plans.py               # 團隊／企劃版本／核准後派工
  approvals.py           # office 策略及恢復綁定，重用 ApprovalStore
  events.py              # office 事件投影與既有 outbox 橋接
  reports.py             # 結果／驗收證據／用量彙總
  lifecycle.py           # 啟停、租約、恢復及 session 清理整合
  api.py                 # HTTP／WS 共用應用層 handler
  runtimes/
    base.py              # AgentSessionRuntime 合約
    pythonai.py          # 既有 AgentRuntime adapter
    codex.py             # 外部引擎 adapter，通過 spike 後實作
    claude.py            # 外部引擎 adapter，通過 spike 後實作
  examples/              # 設定、命令及事件樣本
  tests/                 # 新功能、契約及故障注入測試
  README.md
```

呼叫方向：客戶端 → 既有認證入口 → OfficeService → 持久指派 → 員工 runtime → 工具政策／審批 → 任務驗收 → 報告／事件。UI 不直接啟動程序、寫任務 DB 或核發審批。

需要修改的既有整合點：

- `communication_server.py`：掛接 office handler、startup／shutdown、健康與版本資訊；既有 `/health` 目前只回 `ok`，不作新程式已載入的證明。
- `agent_runtime.py`、runtime 建構流程：保留既有 dataclass 語義，提供可注入的引擎設定、事件及取消。避免沿用 `server.py` 全域 client／環境變數來切換員工模型，造成同時任務互相污染。
- `agent_execution/api.py`、`websocket_server.py`：認證後呼叫共用 handler，加入 office feature negotiation，保持舊客戶端協定。
- `policy.py`、`approvals.py`：以 opt-in office 執行情境擴充策略與綁定；舊模式政策維持原契約。
- `tasks.py`、`coordinator.py`、`budgets.py`：以 adapter／關聯表串接；只在確有必要時擴充核心 API，避免另一套 task、budget 或 DAG runner。
- `workspace_lifecycle/`、`scheduling/`：將新員工執行、待批、排程及產物納入刪除 blocker／取消／清理，遵守既有 owner 邊界。
- `pytest.ini`：新測試放在 `office/tests/` 時，必須加入 testpaths，避免完整回歸漏收集。

## 5. 必須先固定的設計契約

### 5.1 身份、員工與資料

`staff_id` 是工作角色，不是可登入身份。以可信來源建立的 `(source, identity, project, session)` 為 owner，另關聯 office／team／staff；不得允許前端指定別人的 owner，也不能讓「主管」名稱自動取得 admin。

第一版員工及團隊限定在同一 owner scope。若要讓多個對話共用辦公室，須先設計正式 membership 與資源授權；不能移除 session 條件來達成。API 的 `api:*` 與 WS 的身份目前不會自動合併，同人跨通道審批須用可撤銷的授權綁定，不能複製另一身份的審批資料。

建議將 office 關聯及佇列新增至既有 `AGENT_TASK_DB`，以 `office_*` 前綴、schema version、owner 複合唯一鍵管理：

| 建議實體 | 主要欄位及不變條件 |
| --- | --- |
| staff／teams／members | owner、id、設定版本、provider、model、workspace mapping、工具能力、停用狀態；主管權限取決於成員關係。 |
| runtime_sessions | owner、staff、provider session ID、generation、resume capability、目前 task、lease；一位員工同時最多一個有效執行者。 |
| assignments | owner、request ID、task ID、staff、queue sequence、狀態、lease；同一命令重送不重建任務。 |
| plans／plan_items | parent task、revision、完整規格摘要雜湊、依賴、assignee、預算、人工決策；舊版核准不能授權新版企劃。 |
| messages／reports | owner、來源標記、task／staff、產物 reference、驗收結果、摘要；員工回報及附件都是資料，不可提升為系統權限。 |
| runtime_operations／event_outbox | 穩定 operation／event ID、狀態、結果引用、sequence；已開始但結果未知的操作保留 uncertain。 |
| usage | provider／session／turn／task／staff、數值來源、累計或增量、幣別、是否估算；缺資料為 unknown。 |

既有 ApprovalStore／EventStore／scheduler DB 先保持原位。不同 DB 之間不宣稱有單一交易：在 task DB 先寫 intent＋outbox，再以穩定 ID 投遞與對帳；審批消耗後、工具執行前崩潰的縫隙須可識別。副作用未知時停止並要求核對，不承諾外部 shell／網路操作 exactly-once。

### 5.2 引擎合約

使用新名稱 `AgentSessionRuntime`，避免與現有 `AgentRuntime` 混淆。至少定義 `start`、`send`、`interrupt`、`close`、`events`；`resume`、tool approval、usage、MCP、attachments 以能力旗標宣告。`send` 的回傳是接受／拒絕及 turn ID，完成由事件決定。

- 統一事件：`session.started`、`text.delta`、`tool.proposed`、`approval.waiting`、`usage.updated`、`turn.completed`、`turn.failed`、`session.closed`。這些是擬定 office 事件，需映射成既有 WS envelope，不能直接覆蓋現有名稱。
- 不支援 resume 的引擎須顯示「需重新開始」；不得默默開新 session 冒充續行。模型、workspace、工具或權限設定改變時，核對版本並建立新 generation。
- 先包裝原生 PythonAI runner，保留 Skills／記憶／hooks／驗收／預算。外部 Agent 已有自己的模型與工具迴圈，不再被套入 `server.run_agent` 形成兩層自動執行。
- 外部引擎必須在執行工具**之前**提供可阻擋決策，並能覆蓋內建工具、MCP、子代理及背景操作。不能只監看執行後日誌來當作審批。
- Claude 接入先比較可用 Python SDK 與隔離 Node sidecar，驗證登入及 callback；若需 sidecar，其程式、鎖檔與測試放在 `office/runtimes/` 的獨立子目錄。Codex 接入以已驗證版本的 app-server 協定為準。
- 不直接擷取、複製或儲存 CLI 登入 token。登入失效、CLI 缺失、協定不相容時提供明確錯誤；既有 API provider 模式仍可使用。

### 5.3 狀態、審批與恢復

三種狀態分開保存：員工狀態、實際執行狀態、任務板狀態。保留 TaskStore 原狀態，不直接改寫成四欄看板。

| 任務板投影 | 來源或成立條件 |
| --- | --- |
| todo | 尚未派工、排隊、等待企劃確認。 |
| doing | 有有效執行 lease；等待工具審批／使用者答案另顯示 waiting 原因。 |
| review | 已產生結果，等待人工審查或尚缺驗收證據。 |
| done | 符合凍結的驗收契約與該任務完成政策；無驗證契約時必須標明 unverified，不能宣稱 verified_completed。 |
| 例外標記 | failed／cancelled／uncertain／blocked 必須可見，不能被四欄映射成完成。 |

Office 模式建議：明確的內建讀取工具依權限放行；檔案修改與任意 shell 預設送審，必要時直接拒絕。命令分類必須區分 Bash／PowerShell／cmd；首版不以簡單字首或正規表示式將任意 shell 認定唯讀。重導向、命令替換、腳本、解譯器、Git 外部 diff／hook、MCP 任意行為均要保守處理。現有角色、workspace、OS sandbox 與預算仍在審批前後檢查；審批不會增加原本沒有的權限。

待批記錄須綁定 owner、task、staff、runtime generation、provider tool-call ID、完整 canonical arguments digest、必要的檔案前置 hash、期限及一次性決策。審批期限採獨立配置；附件的 7 天是其恢復參考值，不能直接套用，也不把 PythonAI 現有預設 300 秒直接視為產品需求。

恢復順序：重建設定及 owner → 檢查 session tombstone／lease → 對帳 runtime／操作 → 還原待批與佇列 → 核對權限、參數與檔案版本 → 依引擎能力續行。只恢復 pending 資料不等於還原活的 callback；若引擎不支援重接，顯示需核對或重啟。不得將已開始且效果未知的工具自動重播。

### 5.4 派工與主管

同一員工採 FIFO，跨員工有全域與 owner concurrency 上限；等待審批仍占用該員工 session，但不能無限占用模型請求 slot。首版團隊依序執行，後續才加入依賴允許的平行工作，避免同 workspace 寫入競爭。

明確 `@staff` 優先；未指定時按能力、可用性及負載的可解釋規則選人，無適任者即保留待指派。未登入與忙碌不同；離職／停用不刪歷史，未執行任務須重新指派，進行中的任務先取消及對帳。

主管先提出版本化企劃：目標、子任務、依賴、負責員工、工具範圍、產物及驗收、共享預算；人工核准後才建立可執行的子節點。修改企劃需重新確認。子任務失敗／待批／取消均向父任務傳遞，主管只彙整有來源的成果；子任務全完成不自動等於父任務驗收通過。

## 6. 分階段待辦與驗收

以下每個階段可拆為多個小 commit；每次程式變更均同步測試。依賴表示開始實作前應先完成的階段，不表示本次已執行。

### R0：固定基準及可行性（P0，無前置）

- [x] R0.1 保存當時 HEAD、工作目錄、venv 套件、測試與服務配置基準；檢查現有 pytest 失敗，區分既有問題與新增回歸，不沿用舊測試數字。已保存環境快照，記錄首次受檔案變動影響而中止的測試，並完成固定來源後的全套回歸；詳見基準紀錄。
- [x] R0.2 將第 5 節契約寫成 ADR／schema 樣本，固定 office owner 範圍、完成定義及支援矩陣。已加入 ADR 0001 與 runtime JSON envelope 範例；資料表 migration schema 屬 R1.3。
- [ ] R0.3 分別為 Codex／Claude 做隔離 spike：版本、登入檢查、開啟 session、串流、工具前批准／拒絕、取消、重啟、用量、Windows 子程序清理；實際請求與費用需可追溯。
- [ ] R0.4 固定上游 commit、引擎版本、依賴授權與升級策略。無法攔截工具的引擎不得列為可寫入的 hired staff。

驗收：可重現的基準報告與兩份 capability matrix；未支援項目有明確降級行為。外部 spike 尚未通過時，R1–R4 可用原生 PythonAI 推進，R5 不開放。

### R1：office 領域及資料遷移（P0，依賴 R0.1–R0.2）

- [x] R1.1 建立 `office/` 套件、README、contracts、設定範例及測試收集設定；功能預設關閉。旗標目前不接入服務 startup，也不建立 DB／啟動 worker。
- [x] R1.2 建立 owner-scoped 員工／團隊／成員 CRUD，驗證名稱、workspace mapping、provider、模型及最小工具能力；敏感資料不入回應。已完成應用層 OfficeDirectory，HTTP／WS 仍依 R4 接入。
- [x] R1.3 實作版本化增量 migration、索引、設定 revision、停用／刪除限制、備份及 schema 相容檢查。已完成離線備份的 schema 0→1→2、revision CAS、soft delete／主管關係 guard 與會話資料抹除；執行中的 assignment／runtime guard 須於 R1.4／R2 引入時擴充。
- [x] R1.4 實作 assignment／operation／outbox 表與冪等 request ID，建立跨 DB 對帳與容量上限；新增資料表時同步更新 schema version、相容檢查及 session erasure 順序。已完成 v3、固定 target receipt／origin 綁定、sender fencing、取消補償與三 DB 清理；unknown 不自動重送，實際 runner 留至 R2。

驗收：全新及既有 DB 升級均通過；不同 owner 的列表、ID 直取、修改與事件互不可見；失敗 migration 不留下半套狀態；關閉功能可啟動舊流程。

### R2：原生 runtime 與持久派工（P0，依賴 R1）

- [ ] R2.1 建立 `AgentSessionRuntime` 與 PythonAI adapter；將每員工模型／工具配置注入 factory，不改全域環境切換模型。
- [ ] R2.2 實作明確指派及可解釋自動路由、FIFO、同員工單一 lease、owner／全域限流、timeout 與背壓。
- [ ] R2.3 沿用 TaskStore、工作目錄／worktree、根預算及取消；保存 task／staff／runtime generation 關聯。
- [ ] R2.4 加入暫停、停用員工、佇列取消與重新指派；取消後拒絕晚到事件及產物。

驗收：同一請求重送只建一任務；兩個 worker 競爭同員工只一個執行；同員工任務依序，異員工可受限並行；重啟後佇列可對帳，歷史及記憶不串人。

### R3：完整工具審批與恢復（P0，依賴 R2；R5 前必要）

- [ ] R3.1 在既有 policy 增加 office 模式，覆蓋 write／edit／delete／shell／Skills／MCP／子代理的巢狀執行；不重複要求同一工具授權。
- [ ] R3.2 補齊第 5.3 節 binding、檔案變更預覽、拒絕理由、到期與一次性原子消耗；人工核准介面與模型工具分離。
- [ ] R3.3 實作 durable wait／continuation 與恢復分類，解決目前僅存活堆疊可等待的限制；重啟後不可假造 provider callback。
- [ ] R3.4 加入跨 DB approval intent 對帳；工具開始前後 crash、核准後改檔、角色撤銷、session 刪除、重複點擊、逾期及參數替換均保守停止。

驗收：寫檔前內容不變，拒絕後內容不變，批准後僅執行已審內容；不同 task／staff／owner 不能借用批准；未知效果標 uncertain，沒有默默重跑；一般非 office 舊測試仍通過。

### R4：應用介面、事件與最小可用閉環（P0，依賴 R3）

- [ ] R4.1 在既有認證 HTTP／WS 加入 staff、team、dispatch、queue、approval、report 查詢／命令；明確回應 401／403／404／409、分頁與容量限制。
- [ ] R4.2 共用 contracts 與 application handler，將 office events 接入可靠 outbox；定義 sequence、ACK、replay floor、snapshot 及斷線恢復。
- [ ] R4.3 增加讀取 owner-scoped snapshot 的任務板／員工狀態 DTO，保留 raw state、waiting reason 與 verification。
- [ ] R4.4 提供最小操作介面或既有客戶端整合，完成「建員工 → 派任務 → 待批 → 批准／拒絕 → 報告」；MyAI 完整 UI 另列跨儲存庫工作。

驗收：真實本機檔案任務完成一次完整往返；斷線重接不重複執行；舊 `/ws/v1` 客戶端可用；API token 不會自動得到另一 WS 身份權限。

**MVP 門檻：R0.1–R0.2 與 R1–R4 通過。此時可稱具備主程式核心，仍不可宣稱已支援 Claude／Codex 員工。**

### R5：Codex／Claude 受管理引擎（P1，依賴 R0.3–R0.4、R4）

- [ ] R5.1 先完成能力最明確的一個 adapter，再完成另一個；處理 handshake、message framing、request correlation、取消、超時、stdout／stderr 與程序樹回收。
- [ ] R5.2 將供應商工具批准請求轉成 R3；不支援阻擋的工具、背景自主執行或網路能力預設停用，不以觀察事件代替授權。
- [ ] R5.3 保存 provider session ID、版本、generation 及能力；測試同 session 續問、真正重啟恢復、登入過期、版本不符與模型不可用。
- [ ] R5.4 接入獨立成果驗收與根預算；外部引擎無精確請求／token 限制時，宣告限制並以可用的 timeout／turn／成本上界管制，不能宣稱與原生硬預算等價。
- [ ] R5.5 以固定版本錄製協定 fixture；另跑可選真實登入整合，報告實際平台與版本，避免 fixture 通過被誤報為正式引擎驗收。

驗收：兩種引擎各自完成啟動、串流、審批拒絕／批准、取消及能力宣告的恢復案例；互不混用登入、workspace、session 與用量。不支援的能力在 API 與 UI 都可辨識。

### R6：主管企劃與團隊（P1，依賴 R4；雙引擎團隊另依賴 R5）

- [ ] R6.1 加入主管及成員權限，提供受 owner 限制的團隊查詢／派工工具；普通員工不能提升角色或任意建立團隊。
- [ ] R6.2 實作版本化企劃、人工核准／拒絕／修改、預算保留、依賴驗證及循環偵測。
- [ ] R6.3 核准後將子任務接到現有 Coordinator；對長期員工與暫時 agent node 建立映射，首版依序執行。
- [ ] R6.4 子任務結果帶 artifact／evidence reference 回主管，彙總後由父任務驗收；傳遞取消、失敗、預算不足與待人處理。

驗收：未核准企劃零子任務執行；企劃改版撤銷舊批准；一員失敗不偽造整體成功；共享預算不因重新指派／續問而重置；不得重複建立 DAG 節點。

### R7：群組訊息、報告、用量與排程（P1，依賴 R4；團隊報告依賴 R6）

- [ ] R7.1 保存群組訊息與 @ 路由，區分人類指令、工具結果、主管／組員報告；以範圍查詢與上下文上限避免將全部聊天塞給每員工。
- [ ] R7.2 報告包含需求、處理摘要、產物／diff、驗收證據、限制及下一步；沿用 acceptance／git delivery，發布或 merge 仍需既有獨立授權。
- [ ] R7.3 正規化 cumulative／delta 用量，依穩定 event ID 去重，以 staff／task／provider／日期彙總；估算及 unknown 顯式標示。
- [ ] R7.4 排程新增 staff／team target，維持 Asia/Taipei 新排程預設、owner、run limit、通知去重與忙碌策略；模型新增排程仍經既有審批。

驗收：訊息及產物不跨 owner；重播不重複計帳；無用量不顯示為零成本；排程 worker 重啟不重複派工，停用員工時有明確結果。

### R8：服務營運、清理及發布（P0 發布門檻，依賴欲發布的功能階段）

- [ ] R8.1 統一服務啟停：停止新 claim → 處理活躍工作／待批 → 保存狀態 → 關引擎程序與 worker；同 data dir 以租約防雙重 dispatcher，開發配置使用獨立資料路徑。
- [ ] R8.2 readiness 加入 build SHA、schema、worker heartbeat、引擎可用性及降級原因；log／trace 串接 owner-safe task／staff／operation ID，遮罩憑證與敏感內容。
- [ ] R8.3 整合 session 刪除 preview／blocker／tombstone，涵蓋新 runtime、排程、待批、事件與產物；活躍／未知效果未解決前不清除必要證據。
- [ ] R8.4 完成 Windows 原生程序清理驗證，及宣告支援平台的煙霧測試；記錄部署後需重啟後端，健康檢查核對 build SHA。
- [ ] R8.5 提供 feature flags、獨立資料測試、備份、分批啟用與 rollback runbook；關閉 office 先停止派工及核對外部程序，再切回舊入口。

驗收：完整回歸及支援平台整合全過；停機／重啟／DB lock／磁碟滿／離線測試可控；可用備份還原。schema 不相容時只從停機備份回復，不以刪表當 rollback。

### R9：外部員工唯讀觀察（P2，依賴 R5、R8）

- [ ] R9.1 依固定版本建立 Claude session／Codex 日誌 watcher，限制路徑、大小、解析錯誤、輪替、更新頻率及敏感資料。
- [ ] R9.2 清楚區分 managed／observed，外部員工只能顯示觀察狀態與來源；下指令及審批返回原工具。

驗收：觀察器不寫入外部 session、不接管批准、不把舊日誌誤判活躍；版本變更時能停用並顯示未知。

### R10：手機安全配對（P2，依賴 R8）

- [ ] R10.1 另定手機協定及客戶端範圍，重用 office application handler；加入配對期限、裝置憑證、撤銷、TLS／pinning、監聽限制與網路模式。
- [ ] R10.2 手機批准需正式 owner membership／device binding；測試遺失裝置撤銷、重送、重配對及過期批准，不直接轉送任意 WS 命令。

驗收：預設關閉；未配對連線無查詢／審批權，撤銷立即有效；真實手機斷線與重接測試通過。只提供後端 API 不算 Android App 完成。

### R11：多機協作（P2，依賴 R6、R8）

- [ ] R11.1 設計主機身份、mTLS 或等價互信、peer allowlist、能力通告及 owner 授權；每機保存自己的 SQLite，不共用網路磁碟 DB。
- [ ] R11.2 定義跨機 assignment ID、接單回執、取消／過期 fencing、artifact hash 傳輸及本機接收審查；不把本機絕對路徑直接交遠端執行。
- [ ] R11.3 測試網路分割、重複接單、延遲回報、雙邊重啟及遠端副作用未知；不以 timeout 當成「遠端沒做」而自動換機重跑。

驗收：兩台實機以最小權限完成一次派工／成果驗證，故障案例不重複產生副作用；Temporal 模式若要支援，需另做 backend capability 及遷移設計。

## 7. 測試、提交與驗收方法

文件初版只異動文件，當時僅做文件檢查。後續程式實作均須遵守以下驗證要求；各次實際結果另外記錄，不以規劃中的命令或歷史通過數字代替當次驗證。

| 測試層 | 必覆蓋事項 |
| --- | --- |
| 單元／契約 | 路由、權限交集、狀態投影、版本化企劃、schema、用量去重、各 adapter 事件與能力。 |
| SQLite／程序整合 | 真實 DB migration／rollback、並發 claim、lease 過期、outbox 重播、程序樹取消、檔案前置 hash。 |
| 授權／故障注入 | 跨 owner／project／session／staff、冒用主管、approval 參數替換、TOCTOU、核准後撤權、執行前後 crash、未知效果及 tombstone。 |
| 端到端 | 原生引擎完整派工閉環；各外部引擎有獨立可選 live 測試；舊 CLI／API／WS／Telegram／排程及 workspace 清理回歸。 |

Windows 所有 Python 測試或驗證都使用本專案 venv：

```powershell
# office/tests 建立並納入 pytest.ini 後執行。
.\venv\Scripts\python.exe -m pytest agent_execution/office/tests -q
.\venv\Scripts\python.exe -m pytest agent_execution/tests/test_tasks.py agent_execution/tests/test_sessions.py agent_execution/tests/test_policy.py agent_execution/tests/test_approvals.py agent_execution/tests/test_coordinator.py agent_execution/tests/test_budgets.py agent_execution/tests/test_delivery.py agent_execution/tests/test_auth_transport.py agent_execution/tests/test_review_ui.py tests/test_communication_server.py tests/test_websocket_server.py -q
.\venv\Scripts\python.exe -m pytest -q
.\venv\Scripts\python.exe -m pip check
git diff --check
```

涉及新 sidecar／客戶端時，另跑其鎖定套件環境的測試及建置。live 引擎測試以明確 opt-in、隔離 workspace／DB／帳號環境執行；skip 須列原因與剩餘驗收，不可算成通過。若基準已有失敗，先記錄並處理阻塞，不能宣告「所有驗證通過」。

每次程式修改都新增或更新相關測試，對應變更完成後建立 commit；小階段 commit 保留可追蹤性。提交前核對 diff／index，僅提交該次檔案，不混入既有使用者修改。建議訊息如 `feat(office): persist owner-scoped staff and queue`；測試與功能一起提交。

## 8. 執行順序與完成判準

建議順序為 R0 → R1 → R2 → R3 → R4，先交付可操作的原生核心；外部 spike 可獨立驗證，但 R5 在審批閉環完成後才開放。接著 R5、R6、R7，並對每次發布套用 R8 門檻。R9–R11 獨立安排，不阻塞核心。

首個可實作工作包是 **R0.1／R0.2＋R1.1**：固定基準、定義契約、建立 package 與測試收集。下一包才加入員工表與 CRUD。此順序能先固定最難回頭的 owner／schema／引擎責任邊界，避免先做 UI 或外部程序後補審批。

上述初始工作包與 R1.2–R1.4 現已完成。接續 R2.1–R2.4：沿用意圖／收據資料，建立可信 office task admission、runtime adapter、FIFO／lease／共享預算與取消；保留 generic TaskStore 對 office reserved ID 的保護。

「具備附件主程式主要能力」需 R1–R7 及 R8 通過，且 R0 外部引擎能力已實測；「含遠端與多機的完整對齊」還需 R9–R11 及對應客戶端。桌面地圖另屬前端工作，不以後端完成推定其已完成。

主要風險依序為：外部引擎無法完整攔截工具／恢復、身份跨通道誤合併、審批與外部副作用之間 crash、長期 session 與暫時子代理衝突、供應商用量不透明，以及跨機重試造成重複效果。上述風險均有對應待辦與驗收；在 R0 取得實測前，不給出有保證的工期或「直接相容」承諾。
