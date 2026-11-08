# ADR 0001：Office 領域與引擎邊界

狀態：接受，2026-11-07。實作階段：R0.2／R1.1；資料表、執行器及服務整合尚待後續階段。

## 背景

PythonAI 已有 owner-scoped TaskStore、角色／workspace 政策、審批、共享預算及 Coordinator。AI Office 功能需增加持久員工與派工協調，但外部 Claude／Codex agent 自帶工具迴圈；把它當普通模型 API 會造成執行與授權責任重疊。

## 決策

1. 同目的程式、設定、範例、測試及 ADR 全放在 `agent_execution/office/`。先新增可測試的領域契約，保持既有服務入口相容，功能預設關閉且不自動啟動外部程序。
2. Owner 完整保留 `(source, identity, project, session)`，JSON key 與 `tasks.task_owner` 相同。`staff_id`／`team_id` 只是 owner 內資源；主管不是 admin，API／WS 身份不自動合併。Owner DTO 不等於身份憑證；授權仍由可信 transport scope 建立。
3. `AgentSessionRuntime` 是新的 provider-neutral Protocol；現有 `agent_runtime.AgentRuntime` 意義不變。原生 adapter 重用既有 Agent 迴圈，外部 adapter 負責自己的迴圈，不再被 PythonAI 迴圈二次驅動。
4. `start` 回傳 provider session ID；`send` 回傳 `TurnReceipt`，只表示受理。完成由帶 generation／turn ID 的事件表示，最終驗收仍由既有任務契約判定。事件 JSON 是不可變 snapshot，UI／模型不能用事件提升權限。
5. 能力預設皆未宣告。`session` resume 不代表程序或工具續行；`durable` resume 才宣告可恢復已確認邊界。Managed writes 要求 tool approval 且 all tools gated；缺任何一項即拒絕 admission，仍須另過 role／workspace／budget／sandbox／人類審批。可選 `ToolApprovalRuntime` 只接受綁定 session／generation／turn／tool-call 的決策；`ToolDecision` 本身不是授權，不得包含批准後改寫的參數。
6. 未來 `office_*` 關聯表放在同一 TaskStore DB，重用 owner、task、backend binding；ApprovalStore／EventStore 暫留原 DB。跨 DB 用 intent／outbox 與穩定 ID 對帳，不宣稱跨 DB transaction 或外部副作用 exactly-once。
7. 任務執行狀態、員工狀態與四欄任務板分開。`verified_completed` 須沿用 TaskStore 的實際驗收證據；無契約／未驗證結果不能以成功文字推定已驗收。failed／cancelled／uncertain 需保留可見例外標記。
8. 同員工最多一個有效 lease，首版團隊依序執行。取消、設定改版或工作目錄改變須 fencing 舊 generation；未知副作用保留 uncertain 並人工核對，不重播。

## 支援矩陣（此階段）

| 能力 | 契約／基礎 | 可執行整合 |
| --- | --- | --- |
| owner key／隔離、設定驗證 | 已實作與測試 | 不新增 transport 權限。 |
| runtime start／send／interrupt／close／events | Protocol 已定義 | 尚未註冊 adapter。 |
| 原生 PythonAI 引擎 | 既有執行器保留 | office adapter 在 R2。 |
| Codex／Claude | 僅 provider 名稱與共同契約 | R0.3 spike／R5，尚未實測。 |
| session／durable resume | 能力分級與 admission 已定義 | 無引擎在此階段宣告支援。 |
| 工具前審批 | 能力門檻已定義 | office 政策與 durable wait 在 R3。 |
| 員工資料與佇列 | 領域歸屬已定義 | R1.2／R1.3 已完成目錄 CRUD；R1.4 已加入持久派工意圖及 outbox，見 ADR 0003；runner 待 R2。 |
| 手機／多機 | 邊界預留於應用層 | R10／R11，未啟用。 |

## 後果與後續

本次抽出的契約可獨立測試，import 不需要 SDK、模型憑證或 DB。旗標目前不接入服務，避免宣告「已啟用」卻缺乏持久化與審批保護。R1.2 開始才實作 schema、CRUD 及授權應用層；R2 應用本契約，R3 加入精確批准／恢復綁定，R4 才公開入口。

範例見 [runtime.json](../examples/runtime.json)。Protocol 並非執行期 sandbox 或強制安全邊界；實作必須通過契約、故障注入及版本固定的外部引擎測試。

後續資料實作決策見 [ADR 0002](0002-office-directory.md)；本 ADR 的 runtime 能力與授權邊界維持有效。

R1.4 的 intent／receipt／取消與清理契約見 [ADR 0003](0003-office-intents.md)。
