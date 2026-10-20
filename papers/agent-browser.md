# 以 `vercel-labs/agent-browser` 取代 Playwright 的遷移計畫

## 目標與決策

將專案中以 Python Playwright API 執行的瀏覽器自動化，改為由
[`agent-browser`](https://github.com/vercel-labs/agent-browser) CLI 驅動的受控子程序。保留目前的公開工具介面、使用者／工作任務間的登入狀態隔離、網路白名單、下載大小上限、文字／截圖產物與失敗回報語意。

`agent-browser` 是原生 CLI，不是 Python 的 Playwright 相容函式庫；因此本次是建立一個 Python adapter，而非逐行改寫 API 呼叫。它可用 `--json` 回傳機器可讀結果、`batch --json` 減少多次啟動成本，並支援 selector、snapshot、screenshot、下載、狀態檔及持久 profile。[官方 README](https://github.com/vercel-labs/agent-browser#agent-mode) 也特別說明：`networkidle` 不適合持續連線的頁面，應優先等待明確的 selector、文字、URL 或 JavaScript 條件。

### 不變條件（驗收底線）

- `browse_webpage`、`browser_session`、`browser_vision` 的工具名稱、輸入 schema 與 `ToolResult` 成功／失敗語意不變。
- 每個 `task_owner + session_id` 都有獨立的瀏覽器狀態；不能讓另一使用者讀到 cookie、localStorage 或下載檔。
- 非 HTTP(S)、含 URL 帳密、私有 IP／內網目的地仍拒絕；允許網域以現有 `AGENT_BROWSER_ALLOWED_HOSTS` 為來源。
- 下載仍只可落在私有 artifacts 目錄，單檔上限 10 MiB；回傳前仍要驗證真實路徑、型別與大小。
- 不把 cookie、state JSON、profile、trace、下載檔或加密金鑰納入版本控制、測試輸出或模型可讀 workspace。

## 現況盤點（2026-10-20）

| 類別 | 目前位置 | Playwright 職責 | 遷移方向 |
| --- | --- | --- | --- |
| 公開網頁讀取 | `base.py:779` | 新開 Chromium、導航後讀取 `document.body.innerText` | 共用 adapter 的無狀態 read 模式，維持 15 秒期限與 15,000 字截斷 |
| 私有登入／動作 | `agent_execution/media/browser.py`、`browser_worker.py` | persistent context、cookie state、路由封鎖、WebSocket 封鎖、動作、下載、截圖 | 以 CLI session／持久 profile 加 adapter 控管；以每一導航前驗證及 CLI allowlist 維持防護 |
| 環境診斷與熔斷 | `operations/doctor.py`、`turn_control/__init__.py` | 偵測 Python module、`playwright install` 錯誤 | 偵測 `agent-browser` 指令和 Chrome runtime，改辨識 `agent-browser install` 失敗 |
| Python 端整合測試 | `tests/test_base.py`、`agent_execution/tests/*` | mock import 或直接啟動 Chromium | 改 mock adapter/subprocess；瀏覽器 opt-in 測試改跑本機 test server |
| Node E2E／CI | `e2e/`、`playwright.config.ts`、`.github/workflows/playwright.yml` | Playwright test runner、多瀏覽器 smoke test | 改為 agent-browser CLI 的可重現 smoke／驗收腳本；移除 Playwright runner 與報表設定 |
| 依賴 | `requirements.txt`、`requirements-ci.txt`、`package.json`、lockfile | Python 與 Node Playwright | 移除 Playwright，將 `agent-browser` 鎖定為 Node 專案開發／CI 依賴 |

注意：工作樹已有尚未追蹤的 `.github/workflows/playwright.yml`、`e2e/`、`package.json`、`package-lock.json`、`playwright.config.ts`。實作時必須先向提交者確認這些檔案是否為本次應一併取代的 E2E 基線；不要直接覆寫或刪除未追蹤內容。

## 目標架構

```text
工具呼叫／Python runtime
        │
        ▼
agent_execution.media.agent_browser_adapter
  - 驗證 URL、host、action 與輸出大小
  - 以 subprocess 呼叫 `agent-browser --json`
  - 將 JSON／退出碼轉成既有結果格式
        │
        ├── 無狀態 browse_webpage：fresh session + allowlist
        └── 私有 browser_session：owner/session 雜湊的 profile、artifacts 與鎖
                │
                ▼
         agent-browser CLI + Chrome for Testing
```

## 工作項目

### 0. 先做可行性驗證與介面凍結

- [ ] 在乾淨 Windows 與 Ubuntu CI 環境安裝固定版本：`npm install --save-dev --save-exact agent-browser`，再執行 `npx agent-browser install`（Linux 使用 `--with-deps`）。不要採用全域安裝，讓 lockfile 可重現。官方安裝流程見 [README](https://github.com/vercel-labs/agent-browser#installation)。
- [ ] 用本機 HTTP 測試站驗證所有必要能力：open/navigate、selector click/fill、wait、body text、screenshot、download、cookie／localStorage 恢復、request route、WebSocket、非同步輸出與 `--json` 格式；把實際 CLI 版本、輸出 schema、Windows 引號行為記錄到 adapter contract test。
- [ ] 確認 `network route` 能否在「首次導航前」攔下所有不在白名單的 subresource、fetch、WebSocket 與 redirect；若不能證明，禁止切換 production runtime。官方 `--allowed-domains` 能封鎖子資源、WebSocket/EventSource、beacon 及 WebRTC，但它和 profile、restore/state replay 不能共用，見 [官方安全說明](https://github.com/vercel-labs/agent-browser#security)。
- [ ] 決定私有登入 state 的策略。建議將現行每 owner/session 的持久 profile 放在 `AGENT_BROWSER_ROOT/<hash>`，但不得同時啟用 CLI `--allowed-domains`；採取 adapter 的 DNS/IP gate + pre-navigation route 防護，並在風險評審通過前保留 Playwright fallback。若安全需求要求 CLI allowlist，改採無 profile 的 fresh context，並把加密 state 交由受保護的 vault／state 檔載入，且重新設計登入續跑。
- [ ] 明確列出不支援的 Playwright 功能與決策：多瀏覽器（Firefox/WebKit）、`expect` 的完整 matcher、`route_web_socket`、service-worker 封鎖與下載事件。不可用 `eval` 或輸出文字解析悄悄模擬安全保證。

**完成條件：** 有一份通過 Windows/Linux 的 capability matrix；對「profile persistence 與 network containment 不能同時使用官方 allowlist」的取捨有核准紀錄。

### 1. 建立受限的 Python adapter（不改工具 API）

- [ ] 新增 `agent_execution/media/agent_browser.py`，集中處理 executable 尋找、`run_process`、JSON decode、timeout、stdout/stderr 截斷與錯誤正規化；不得讓 `base.py` 或 worker 自行拼 shell 字串。
- [ ] 以 argument list 呼叫命令，固定帶 `--json`，完全禁止 `shell=True`；每次讀取 CLI JSON 都限制 1 MiB，保留原始錯誤的短摘要但不洩漏 state／cookie。
- [ ] 將 action contract 對照為 CLI batch：`click`→`click`、`type`→`fill`、`wait_for`→`wait <selector>`、`wait`→`wait <ms>`、`assert_text`→`get text` 後由 Python 精確比較、`assert_url`→`get url` 後比較、`download`→需驗證實際 CLI 行為與下載路徑；未知 action 繼續拒絕。
- [ ] 動作後以 `read` 或 `get text body` 取得文字，仍在 Python 端限制為 65,536 字；截圖與下載必須指定 adapter 建立的 artifacts 絕對路徑。
- [ ] 保留 `FileLock`、profile／artifact 的 `resolve().is_relative_to()` 檢查，以及所有既有容量上限；新增 CLI state/profile 預期檔案的 symlink 拒絕與總容量檢查。
- [ ] 建立明確例外類別（未安裝 CLI、未安裝 Chrome、逾時、策略拒絕、頁面動作失敗），讓上層能安全顯示可操作的診斷。

**完成條件：** adapter 單元測試可 mock subprocess，驗證 argv、timeout、JSON 錯誤、路徑逃逸、輸出上限、download 產物驗證與敏感值遮蔽。

### 2. 以 adapter 替換 runtime

- [ ] 將 `base.py:browse_webpage` 改呼叫 adapter，保留 HTTP(S) 前置檢查、15 秒 timeout、15,000 字截斷及現有 evidence 結構；移除 `playwright.sync_api` import。
- [ ] 將 `browser_worker.py` 改為薄的 request/response worker（或在測試證明無隔離退化後併入 adapter）；將導航前的 `allowed_url()`、每個 action 的限制、artifacts 驗證保留在可信 Python 端。
- [ ] 將 Playwright persistent context cookie JSON 機制遷至選定方案。若用持久 profile，驗證其保存 cookies、localStorage、IndexedDB、service worker 與登入 session 的行為；官方明列這些 profile 內容，見 [Persistent Profiles](https://github.com/vercel-labs/agent-browser#persistent-profiles)。
- [ ] 如果使用 `--session --restore`，為每個 task owner/session 建立不碰撞的 stable session key，設定 state TTL／清除工作流程，並設定 `AGENT_BROWSER_ENCRYPTION_KEY`；官方 state 預設可能落在使用者家目錄且 state 可能含明文 token，不能接受預設位置。
- [ ] 將 `turn_control` 的環境錯誤群組改為辨識 `agent-browser` 不存在、Chrome 未安裝、`agent-browser install` 要求、CLI protocol error；保持「兩次相同環境失敗後不重試」行為。
- [ ] 將 `dependency_doctor()` 的 Python `playwright` 檢查改為 `agent-browser` executable、版本和 `agent-browser session info --json`／明確健康檢查，避免啟動外站連線。

**完成條件：** 現有工具端到端呼叫可在本機 test server 完成登入 cookie 恢復、selector action、失敗 assertion、截圖與私有下載，且不同 identity 互不可見。

### 3. 測試、CI 與依賴切換

- [ ] 更新 `tests/test_base.py`：mock 新 adapter／可執行檔失敗，仍驗證 `browse_webpage` 回傳失敗的 `ToolResult`，不要 mock Python module import。
- [ ] 更新 `agent_execution/tests/test_media.py` 的 opt-in 瀏覽器案例，保留既有登入續跑、隔離、assertion、下載驗收，另加 redirect 到內網、非白名單子資源、WebSocket 與超過 10 MiB 下載拒絕測試。
- [ ] 更新 `turn_control` 測試，將舊的 Playwright 訊息改為新 CLI/Chrome 安裝錯誤；另測 JSON 壞損與 CLI nonzero exit。
- [ ] 將 UI／workspace HTTP 測試改為 adapter 驅動的 CLI smoke，或若它們只驗證 HTTP 回應，改用直接 HTTP assertions，避免瀏覽器測試無故依賴。
- [ ] 將 Node `e2e` 範例換成 agent-browser CLI 腳本（本機 fixture server、snapshot/selector/title assert、screenshot artifact）；不再要求 Firefox/WebKit，因為目標工具只以 Chrome/CDP 為基礎。
- [ ] 將 `.github/workflows/playwright.yml` 換為明確命名的 `agent-browser` workflow；在 Ubuntu 使用 `npx agent-browser install --with-deps`，保存 screenshot、trace/HAR（如有）及結構化測試結果。
- [ ] 將 `verification.yml` 的 Playwright browser install 改為 agent-browser 安裝；CI 不讀取真實 profile/state，也不設定真實 encryption key。
- [ ] 最後才從 `requirements.txt`、`requirements-ci.txt`、`package.json`、lockfile 移除 Playwright，刪除 `playwright.config.ts`；只處理已確認屬本次範圍的未追蹤 E2E 檔案。

**完成條件：** `./venv/Scripts/python.exe -m pytest`、語法／benchmark 驗證及新的 Node/CLI E2E 全數通過；CI 不再安裝或參照 Playwright。

### 4. 分階段上線、觀測與 rollback

- [ ] 先以設定旗標（例如 `AGENT_BROWSER_BACKEND=playwright|agent-browser`）提供雙軌，預設保持既有 backend；同一組本機 fixture contract tests 同時跑兩種 backend，對比成功結果與安全拒絕結果。
- [ ] 加入不含 URL query、cookie、selector 內容的結構化指標：backend、CLI version、動作類型、耗時、exit category、policy deny、download size bucket；禁止記錄 page text／認證資料。
- [ ] 先在 CI 與內部環境連續觀察，再將預設切到 `agent-browser`；保留 Playwright 相依與 fallback 至少一個已驗證的發布週期。
- [ ] 設定明確 rollback：旗標切回 Playwright、保留舊 profile 只讀備份、停止 agent-browser daemon/session、清除新 state/artifact；回復前後各跑 contract suite。
- [ ] 觀察期結束後才移除 fallback、Playwright 套件與設定；更新 README 中「Playwright owner profile/cookies」描述為 agent-browser adapter 及其安全限制。

**完成條件：** 有已演練的 rollback、已記錄的版本／相容性矩陣，以及無 Playwright reference 的最終 `rg -i playwright` 檢查（歷史／遷移文件除外）。

## 建議提交切分

1. `docs: plan agent-browser migration`（本文件）。
2. `test: add agent-browser adapter contract coverage`（先紅測與本機 fixture）。
3. `feat: add agent-browser adapter behind backend flag`。
4. `feat: migrate browser runtime to agent-browser`。
5. `ci: run agent-browser browser verification`。
6. `chore: remove Playwright after observation window`。

每一個程式碼提交都必須隨附相關測試更新並以本專案 `venv` 執行 Python 測試；文件-only 提交只需做 Markdown／diff 驗證。

## 主要風險與解除條件

| 風險 | 為何重要 | 必須解除的條件 |
| --- | --- | --- |
| network allowlist 與 restore/profile 不相容 | 目前 browser session 要同時保留登入狀態與封鎖越界網路 | 實測證明替代 containment 完整，或重設登入 state 架構；否則不可上線 |
| CLI 非 Python API | 退出碼、JSON schema、並發 daemon 與 Windows 引號可能造成語意差異 | adapter 將所有 CLI 邊界集中，並有 Windows/Linux contract test |
| service worker／WebSocket 行為差異 | 原 worker 明確封鎖 WebSocket、service worker | 為每項做負向安全測試；不能支援就 fail closed |
| browser/test runner 能力收斂 | Playwright 設定涵蓋 Chromium、Firefox、WebKit | 產品接受 Chrome-only，或另保留跨瀏覽器測試工具；不可把兩者混為同一替代品 |
| state 洩漏 | state/profile 有 token；官方文件警告 state file 可含明文 token | 每 owner/session 路徑隔離、權限／容量／清理、加密與 `.gitignore` 都通過審查 |
