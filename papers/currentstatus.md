# PythonAI 專案現況與 Claude 能力差距盤點

盤點日期：2026-10-29
盤點基準：本機 `main`／`6b82e12`、專案原始碼與 roadmap、GitHub Actions，以及 Claude／Claude Code 官方公開能力。

## 1. 結論

PythonAI 已是後端能力相當完整的 Agent 平台原型，但尚未達到 Claude 等級的完整產品。

若比較 Claude Code，PythonAI 已涵蓋多數主要代理構件：Skills、Hooks、MCP、子代理、worktree、程式碼索引、批准、持久任務、測試與 PR 流程；在 Temporal、崩潰恢復、預算、取消與不確定副作用稽核方面，甚至比一般互動式 coding agent 更深入。

目前主要差距不在於再增加一套 workflow，而是：

1. 模型能力與任務成功率尚未經正式 benchmark 證明接近 Claude。
2. 桌面產品、Computer Use、Artifacts、Research、Projects 與 Connectors 等整合體驗仍不完整。
3. CI、安裝、正式部署、跨平台與多機運行尚未達到可發布水準。

現階段較準確的產品定位是：

> 可靠性設計先進的 Windows 本機 Agent 平台，尚不是 Claude 等級的完整跨平台 AI 產品。

## 2. 本次盤點範圍

本次進行整體架構層級盤點，涵蓋：

- 約 538 個專案檔案與 117 個 Python 測試模組。
- CLI、ASGI、WebSocket、Telegram 等主要入口。
- Agent runtime、provider、budget、policy、approval、task、checkpoint 與 evaluator。
- memory、communication、media、browser、code index、IDE、Skills、Hooks、MCP 與 extensions。
- coordinator、worktree、patch／PR／CI、Temporal backend 與 operations。
- `README.md`、`papers/` roadmap、GitHub Actions 現況及 Claude 官方公開能力。

這是功能與工程成熟度盤點，不是逐行 security audit，也不是模型品質實測。

## 3. 已具備且不應重做的能力

下列能力已有明確實作，後續應沿用既有核心，不應為了模仿 Claude 另建一套平行架構：

- 多輪模型／工具 Agent loop、持久 task／checkpoint、依賴步驟與驗收。
- request、token、成本、deadline、iteration、child count 與 retry 預算。
- Temporal Workflow、Continue-As-New、排程、取消、故障恢復與 History replay。
- 多代理 DAG、planner／coder／reviewer、獨立 worktree、follow-up、取消、合併與 archive。
- RBAC、一次性批准、工具政策、路徑與 symlink 防護、Docker sandbox。
- Skills、Hooks、MCP、動態工具版本、人工核准啟用、撤回與 rollback。
- Python／Go／JavaScript／TypeScript 程式碼索引、符號、引用、Jedi／LSP 與 AST 修改。
- episodic、semantic、procedural memory，以及背景 review／embedding queue。
- WebSocket 串流、可靠事件、附件、Telegram／LINE／Discord adapters。
- Patch、PR／MR、CI 門檻、Reviewer、Critic 與結構化完成驗收。
- Prometheus 匯出、trace、readiness、backup／restore 與本機 operations runbook。

完整功能表可參考 [`README.md`](../README.md#現有功能表)。

## 4. 目前最急迫的工程問題

### 4.1 `main` 的 Layered verification 目前失敗

最新盤點時，GitHub Actions 的 Playwright workflow 通過，但 `Layered verification` 在 `6b82e12` 失敗，而且近期多次執行均未恢復綠燈。

已確認的失敗為：

1. Windows `unit-native` 在收集 Temporal DAG 測試時，因 CI requirements 未安裝 `temporalio` 而出現 `ModuleNotFoundError`。
2. Linux `docker-browser` 的真實 sandbox 測試失敗，runner 回報 `Command path is outside sandbox workspace`。
3. 同一輪的 IDE job 通過，獨立 Playwright workflow 亦通過。

證據：[`Layered verification` run 40723720879](https://github.com/asccclass/pythonai/actions/runs/40723720879)。

因此，本機大量測試通過紀錄不能直接等同於目前版本可發布。應先修復 CI 依賴分層與 Docker workspace 路徑契約，並將 `main` 全綠設為後續功能開發的 P0 gate。

### 4.2 本機驗收不等於正式環境完成

目前 Temporal 與 operations 的主要證據來自 Windows venv、loopback／隔離 Service 及本機 Worker。以下仍未完成：

- frozen executable。
- 正式告警接收端。
- 正式主機與部署方式。
- Linux／macOS 實機。
- 遠端 Worker 與多機 failover。
- 正式資料遷移與 production legacy 移除。

詳細邊界見 [`papers/temportal.md`](temportal.md#2026-10-29-後續交付與正式環境缺口) 及 `agent_execution/temporal_backend/DEPLOYMENT_HANDOFF.md`。

## 5. 與 Claude 等級相比的功能缺口

| 優先級 | 缺口 | PythonAI 現況 | 需要補足的能力 |
| --- | --- | --- | --- |
| P0 | 模型品質與 Agent benchmark | 有 evaluator、固定案例與本機失敗矩陣，但沒有可證明接近 Claude 的跨任務成功率 | 建立 coding、research、browser、document、long-running、中文任務基準；量測成功率、成本、重試與人工介入率 |
| P0 | Provider 架構 | 主要使用 OpenAI-compatible Chat Completions；沒有多 provider failover | 正式 provider adapter、能力協商、健康路由、reasoning 控制、prompt caching、結構化輸出、安全 failover |
| P0 | 可發布基線 | CI 紅燈，尚無 root `pyproject.toml`，主要依賴 source checkout | 全綠 CI、可重現 lock、wheel／installer、SBOM、升版、復原與 rollback |
| P0 | 正式部署與 HA | Windows 同機 SQLite／Temporal 證據完整，但不是生產環境 | TLS、正式 secrets、遠端 Worker、多機儲存、備份演練、故障轉移、告警接收端與容量測試 |
| P1 | Desktop Computer Use | 有受限 browser、DOM、截圖與下載；未提供桌面操作 | 原生應用畫面理解、滑鼠鍵盤控制、每 app 授權、prompt-injection 防護與可撤銷操作 |
| P1 | 整合式桌面開發體驗 | 有 VS Code extension、Dashboard、WebSocket，但功能分散 | 整合 chat、terminal、diff、editor、preview、PR／CI、side chat 與中途 steer 的工作區 |
| P1 | Research 模式 | 有搜尋、頁面擷取與 evidence，但不是完整研究產品 | 多輪研究計畫、來源去重、段落級引用、矛盾來源處理、研究報告與可重播 evidence graph |
| P1 | Artifacts | 有檔案產物與 artifact reference，但沒有互動工作台 | 即時預覽、版本切換、局部更新、互動 React／HTML、分享、fork、發布與權限模型 |
| P1 | Projects／知識庫 | 有 owner／project／session 隔離、記憶與文件解析 | 可管理的 Project knowledge、跨聊天共用指令、來源管理、大型 RAG 與權限繼承 |
| P1 | Connectors 生態 | MCP／OAuth 基礎已實作；正式 issuer／帳號尚未完整驗收 | 一鍵安裝目錄、consent UI、scope 管理、health、撤權、工具開關與可信發布流程 |
| P1 | 雲端自動化 | 有本機 cron／watch／Temporal schedules | GitHub event、API trigger、受管理執行環境、遠端 routine、執行歷史與通知 |
| P2 | 大型記憶檢索 | SQLite、embedding、Hash fallback 已實作；memory 尚無大型 ANN | 大資料量索引、增量更新、ACL-aware retrieval、品質資料集與長期漂移監控 |
| P2 | 企業治理 | 有 RBAC、批准與稽核，但偏單機部署 | SSO／SCIM、組織政策、集中 audit export、DLP、資料駐留與 connector 管理政策 |
| P2 | 真實整合驗收 | 多數外部平台以 fixture／本機 contract 驗證 | 真實帳號 round trip、撤權、rate limit、網路中斷、重送與平台版本變動測試 |
| P2 | 行動與語音產品 | 有 WebSocket、STT／TTS protocol 與 companion 能力 | 手機客戶端、實際裝置播放、語音品質、背景通知與跨裝置 session continuity |

## 6. 模型能力與工程功能必須分開評估

「達到 Claude 水準」不能只靠增加功能數量判定。

即使 PythonAI 具備相同工具與 workflow，最終表現仍高度取決於：

- 基礎模型的推理、編碼、視覺、長上下文及 instruction-following 能力。
- system prompt 與 tool schema 品質。
- context selection、compaction 與 retrieval 精度。
- 模型判斷何時搜尋、何時使用工具、何時停止的可靠度。
- evaluator 與人工驗收是否真的能偵測未完成或錯誤結果。

因此應建立版本化 benchmark，至少包含：

1. repository diagnosis 與跨檔修改。
2. 測試失敗定位與最小修正。
3. Web research、來源引用及矛盾處理。
4. Browser／GUI 任務與 prompt injection。
5. PDF／Office／圖片理解。
6. 長任務中斷、重啟、審批與取消。
7. 多代理分工、Reviewer 及 merge。
8. 繁體中文需求理解與產物品質。

每個案例應記錄：完成率、verified completion、請求數、token／成本、wall time、人工批准次數、重試次數、錯誤分類與是否產生不安全副作用。

## 7. 建議實作順序

### 階段 A：恢復可信基線

1. 修復 `unit-native` 的 Temporal optional dependency／測試收集契約。
2. 修復 Docker sandbox 在 GitHub runner 的 workspace path 問題。
3. 確認 Layered verification、Playwright、IDE 與 benchmark 全綠。
4. 將 CI 綠燈與必要 artifact 設為 release gate。

### 階段 B：先量化模型與 Agent 品質

1. 建立 Claude 對照 benchmark 與固定資料集。
2. 在相同任務、相同工具與預算下比較不同 provider／model。
3. 釐清失敗來自模型、prompt、tool contract、retrieval、evaluator 或 runtime。
4. 以 benchmark 結果決定 provider adapter 與 agent loop 改造優先級。

### 階段 C：完善 provider 與研究能力

1. 抽象正式 provider adapter 與 capability negotiation。
2. 加入 reasoning、prompt caching、structured output、引用與安全 failover。
3. 建立 Research workflow、來源證據與段落級 citation。
4. 將 Projects knowledge 與大型 RAG 納入同一權限模型。

### 階段 D：補齊產品體驗

1. 完成 Windows 可安裝版本、onboarding、credential store、更新與復原。
2. 建立統一 Desktop workspace 與 app preview。
3. 加入受控 Computer Use。
4. 建立 Artifacts 預覽、版本、分享與 fork。
5. 將 MCP／Skills／Connectors 做成可安裝、可撤權的生態系。

### 階段 E：正式服務與跨平台

1. 遠端／多機 Temporal、正式儲存、TLS 與 secrets。
2. Linux／macOS package 與乾淨機驗收。
3. HA、backup／restore、failover、容量及災難演練。
4. 企業身分、組織政策、audit export 與 DLP。

## 8. 與既有 roadmap 的關係

不建議另建重複的技術核心。後續工作應整合既有 roadmap：

- 跨平台安裝、打包、launcher、credentials、installer 與更新：[`papers/todo_跨平台原生系統.md`](todo_跨平台原生系統.md)。
- Temporal 正式部署、多機、放量與 legacy 移除：[`papers/temportal.md`](temportal.md)。
- Browser backend 與安全遷移：[`papers/agent-browser.md`](agent-browser.md)。
- Memory 型別、RAG、治理與 request budget：[`papers/memory_plan.md`](memory_plan.md)。
- Skills 架構與執行邊界：[`papers/skill_plan.md`](skill_plan.md)。
- 通訊平台與正式送達：[`papers/communication_plan.md`](communication_plan.md)。

其中跨平台發行規劃的 P01–P30 幾乎尚未開始；Temporal 文件也明確將正式部署、多機與 production legacy 移除保留在後續階段。這些項目是從本機原型走向 Claude 等級產品的主要工程路徑。

## 9. Claude 官方能力參考

- [Claude Code：extension features overview](https://code.claude.com/docs/en/features-overview)
- [Claude Code Desktop](https://code.claude.com/docs/en/desktop)
- [Claude Code routines](https://code.claude.com/docs/en/routines)
- [Claude Projects](https://support.anthropic.com/en/articles/9517075-what-are-projects)
- [Claude Research](https://support.anthropic.com/en/articles/11088861-using-research-on-claude)
- [Claude Artifacts](https://support.anthropic.com/en/articles/9487310-what-are-artifacts-and-how-do-i-use-them)
- [Claude Desktop／Web Connectors](https://support.anthropic.com/en/articles/11725091-when-to-use-desktop-and-web-connectors)

上述外部能力會隨產品版本改變；開始對應里程碑前應重新查核官方文件，不應只依本文件視為長期固定規格。
