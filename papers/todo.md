# PythonAI 跨平台本機發行任務規劃

規劃日期：2026-10-12。目標是讓使用者在 Windows、macOS、Linux 安裝並啟動 PythonAI，不必自行安裝 Python 或整理依賴。本檔是本機發行的新任務清單；工作目錄中的 `papers/1.todo.md` 保留原 T01–T21 功能與正式驗收紀錄，本次不修改該備份。以下全部為待執行工作，不代表已完成實作或跨平台驗收。

## 產品範圍與交付原則

- 首版採「內附 Python runtime＋既有 FastAPI／Web UI＋本機 launcher」，使用者啟動後自動開啟瀏覽器。保留 CLI、Telegram polling、`communication_server.py` 整合 Web／API／WebSocket 三種模式，共用核心設定與執行政策。
- 核心程式共用；runtime、原生依賴、安裝器按 OS／CPU 架構分別製作。優先 Windows x64，再 macOS arm64、Linux x64；macOS x64、Windows arm64、Linux arm64 為擴充目標，通過驗收才列為支援。
- 「本機安裝」與「完全離線」分開驗收：首版可以連遠端模型；離線版須另外完成本機推論、embedding、模型資源與禁網路驗證。Telegram／Discord／LINE、遠端 MCP 等仍須網路。
- 模型、Laya、文件轉換、Playwright、語音、外部 Skill／MCP、Docker 沙箱分成明確功能組。未安裝的功能須在 UI／doctor 顯示原因，不能將精簡版宣稱為全功能版，也不能默默移除保護政策。
- 程式、唯讀資源、可寫使用者設定、owner 隔離的狀態、使用者 workspace 分開。發行包不含真實 `.env`、Token、個人記憶、瀏覽器登入資料或本機工作檔。
- Docker Compose 與桌面外殼列為後續選配；各自完成才標註其發行通過。Docker 沙箱功能仍依原本政策要求 Docker，不能因桌面打包而降級成 host 執行。

## 原始碼分析與需先處理的落差

| 現況與來源 | 對打包的影響 | 對應任務 |
| --- | --- | --- |
| 根目錄尚無 `pyproject.toml` 或正式發行建置設定 | 缺安裝入口、套件資源清單、可重現發行流程 | P03、P04、P16 |
| `requirements.txt` 混合模型、轉換、開發依賴；CI 清單也含測試與替代版本 | 不能直接把 CI 環境或現有 venv 當成正式產品 | P02、P04 |
| `server.py` import 時讀必要環境變數；API Key 驗證固定要求 LiteLLM `sk-` | 首次啟動缺設定即失敗，本機模型須另外處理 provider 契約 | P06、P13、P14 |
| 多處使用 cwd、`__file__`、`LOCALAPPDATA`／home fallback | 安裝唯讀目錄、不同啟動目錄、macOS／Linux 目錄不一致 | P05、P08 |
| coordinator、browser／audio／LSP worker 使用 `sys.executable` 與腳本路徑 | 凍結執行檔後不能直接當成 Python interpreter 使用 | P09、P16 |
| Skills／動態工具／manifest 與 Web 靜態檔有檔案路徑依賴 | wheel 或安裝包可能漏檔；內建與使用者擴充需分開 | P08、P10 |
| Windows Job Object、POSIX process group、平台命令與外部 executable | 純 Python 測試成功不等於各 OS 的程序取消與工具相容 | P09、P10、P15 |
| 現有 CI 以 Windows unit/native、Ubuntu 部分 Docker/browser 整合為主 | 缺 macOS、安裝包、乾淨主機、簽章與更新驗收 | P15、P17–P23 |
| Telegram 已有本機 Bot 鎖，但先前正式 API 實測仍有 consumer 409 | 安裝包須避免自己重複啟動，不能宣稱能排除其他主機 consumer | P11、P15、P23 |

## 執行順序與里程碑

以下編號為建議交付順序；同階段工作可在滿足依賴後並行，但不得跳過驗收門檻。

| 階段 | 任務 | 交付門檻 |
| --- | --- | --- |
| M0：發行規格 | P01–P03 | 支援矩陣、功能分級、授權清單與目錄／入口設計確定 |
| M1：核心可安裝 | P04–P10 | 依賴可重現、設定／資料隔離、資源與 worker 在安裝後可運作 |
| M2：本機使用體驗 | P11–P15 | 單實例 launcher、首次設定、本機 UI 與核心測試通過 |
| M3：可交付建置 | P16–P17 | 各平台 portable artifact 自動建置與發行驗證流程具備 |
| M4：首版跨平台發行 | P18–P23 | Windows → macOS → Linux 的安裝、更新、移除與乾淨主機驗收完成 |
| M5：後續選配 | P24–P30 | Docker、桌面外殼、額外架構、完整離線版分別驗收，不回填成首版已完成 |

M4 不依賴 M5 的選配功能；P23 通過才可宣稱首版跨平台可用。若 P01 決定首版必須完全離線或具桌面外殼，須將相應 P25–P27／P30 納入 P23 發行門檻。

## M0：發行規格

- [ ] **P01 — 明確定義支援平台、功能組與首版驗收範圍**
  - 工作：列出 OS 最低版本、CPU 架構、Python 基線、Linux glibc／桌面條件、磁碟與 RAM 需求。以現有 Python 3.12 CI 為候選，依原生 wheels 與功能測試決定，不能僅選最新版本。定義核心版／選配功能、遠端模型版／離線版、portable／installer 的支援承諾。
  - 交付：版本化支援矩陣、功能能力表、首版 acceptance cases、已知不支援範圍；Desktop／Docker／其他架構列明是否延後。
  - 驗收：每個「支援」組合均有對應驗收案例；未配置的外部服務與硬體驗收列為待辦，不以 fixture 代替。
  - 依賴：無。

- [ ] **P02 — 盤點依賴、外部程式、模型與再散布授權**
  - 工作：區分直接／間接依賴、純 Python／原生 wheels、必需／選配；盤點 Git、Node、Docker、Chromium、OCR／音訊工具、Skill executable、MCP server 與 Laya／其他模型。確認來源、版本、平台、下載大小、授權、NOTICE、模型再散布權與更新方式，記錄已知漏洞／相容問題的處置。
  - 交付：可機器讀取的依賴／資源 inventory、授權與第三方公告清單；決定哪些內附、首次下載或要求外部安裝。
  - 驗收：發行所用套件與資源均有來源／授權記錄；不能因開發機已有某 executable 就忽略其安裝與驗證。禁止將帶個人資料的模型 cache 或整個現有 venv 直接打包。
  - 依賴：P01。

- [ ] **P03 — 規劃套件、發行專用目錄與相容入口**
  - 工作：在 `distribution/` 集中 launcher、平台建置／安裝設定、需求文件、資源清單與測試；共用執行設定優先放既有 `agent_execution/`。根目錄新增 `pyproject.toml` 作專案 metadata。規劃套件納入既有模組的方式，避免無必要的大量改名；既有三個 Python 啟動入口保留相容轉接。
  - 交付：目錄設計、套件邊界、CLI／worker 入口契約、資源／使用者資料邊界；建置輸出使用忽略於 Git 的 `build/`／`dist/`。
  - 驗收：新增獨立程式資源集中於專用目錄；既有工具與 import 契約有遷移方案，不新增第四個重複 Agent 核心。
  - 依賴：P01、P02。

## M1：核心可安裝

- [ ] **P04 — 建立 pyproject、功能分組與可重現依賴**
  - 工作：宣告 build backend、Python 範圍、console scripts、套件資料；分出 core、local-model／guard、documents、browser、audio、development 等依賴組。使用 lock／constraints 記錄平台條件、版本與校驗；CI 清單與正式 runtime 分工，排除正式包中不必要的 pytest／lint 工具。
  - 交付：wheel／sdist、各功能組安裝方式、各平台依賴鎖定與相容性記錄。
  - 驗收：乾淨環境可 build、install、import 核心；`pip check` 通過。功能組缺依賴時能列明原因；使用特定開發工具的 Agent 功能仍可在受控工具環境安裝該工具。
  - 依賴：P03。

- [ ] **P05 — 統一跨平台設定、狀態、cache 與 workspace 路徑**
  - 工作：以 `platformdirs` 或同等跨平台機制建立集中 path resolver；區分唯讀 app resources、config、SQLite 狀態、logs、cache、models、browser profile、user extensions 與使用者選擇的 workspace。統一目前分散的 LOCALAPPDATA／home fallback，保留 owner／project／tenant 隔離；提供明確覆寫方式。
  - 交付：路徑 API、各平台目錄表、舊資料位置探測與遷移映射；CLI／服務／子代理使用相同 resolver。
  - 驗收：中文／空白／非 ASCII 路徑、任意 cwd、唯讀安裝位置均可運作；狀態不寫入程式目錄；使用者擴充的可寫區不自動成為受信任 executable 區。
  - 依賴：P04。

- [ ] **P06 — 設定載入與 provider 契約重整**
  - 工作：移除 import 階段必需的環境變數讀取，先建立設定物件再初始化 Agent；定義預設值、檔案、環境變數、CLI 的優先序。舊 `.env` 相容匯入但不覆蓋明確的新設定；ROLES_CONFIG／COMM_ADMIN_SENDERS 的優先序維持。遠端 LiteLLM、一般相容 endpoint、本機 endpoint 各自驗證 URL／auth／模型能力，取消所有 provider 共用 `sk-` 假設。
  - 交付：設定 schema／版本、驗證與去密輸出、provider capability／health 契約、錯誤分類。
  - 驗收：全新安裝缺設定仍可進入設定頁；遠端缺必要 credentials 應拒絕連線；允許本機無 auth 不代表放寬遠端驗證。文字／圖片／embedding 能力不相符時明確提示，不靜默替換模型。
  - 依賴：P05。

- [ ] **P07 — 資料遷移、備份還原與升版相容性**
  - 工作：盤點記憶、通訊、任務、批准、排程、trace、OAuth 與擴充等 stores；沿用 operations 備份／migration，補齊整體 manifest、schema 版本、舊路徑搬遷與一致性策略。進行中的工作與不確定外部副作用須標記，不能更新後自動重放。
  - 交付：先備份再遷移的流程、失敗復原、還原驗證、資料與程式版本相容表。
  - 驗收：真實 SQLite fixture 的升版／中途失敗／重啟／還原均通過；不在連線仍開啟時直接搬移 DB，不只複製 SQLite 主檔而漏 WAL。降版不支援時保留資料並提示，不強制開啟新版 schema。
  - 依賴：P05、P06。

- [ ] **P08 — 套件資源與動態載入相容性**
  - 工作：完整納入 Web 靜態檔、Skill metadata／腳本、builtins manifest、schema、prompt／範本與 worker 資源；改用明確 resource loader。內建唯讀 Skills 與使用者可寫／批准後擴充分離。需實體路徑的資源有受控生命週期與校驗。
  - 交付：package-data 清單、資源解析 API、缺檔／版本不符的診斷。
  - 驗收：離開 repository、wheel 安裝後仍可開 UI、列出／匹配 Skills、載入 manifest；資源存取不依賴開發機絕對路徑。生成工具不得寫入安裝目錄或繞過原批准／測試 gate。
  - 依賴：P04、P05。

- [ ] **P09 — 統一 Python runtime 與 worker 啟動**
  - 工作：內附可再散布的各平台 Python runtime；集中解析受信任 interpreter 與 worker argv，涵蓋 coordinator、browser、audio、LSP、operations 與 Python 工具。保留獨立程序、timeout、取消、輸出上限與 Windows／POSIX 清理；區分核心 runtime 與使用者專案 venv，Agent 不可直接更新 app runtime。
  - 交付：runtime manifest、worker dispatch 契約、子程序啟動 helper、工具環境管理。
  - 驗收：未安裝 Python、PATH 無 python、不同 cwd、空白路徑仍能啟動 worker；取消／crash 無殘留程序。若日後使用 PyInstaller，須另驗證 frozen dispatch，不能用凍結後的 `sys.executable` 直接執行 `.py`／`-m`。
  - 依賴：P04、P05、P08。

- [ ] **P10 — 外部工具、Skills、MCP 與平台命令相容性**
  - 工作：清除固定 Windows `.exe`／PowerShell／絕對路徑假設；建立功能能力偵測、平台命令 adapter、受信任 executable resolver。依 P02 決策處理 Git／Node／OCR 等安裝或下載；MCP 使用者設定與 credentials 保存在資料區。
  - 交付：跨平台工具能力表、安裝／補齊指引、缺依賴的統一錯誤；檔案模式、symlink／junction 與 executable 權限相容策略。
  - 驗收：合法工具在支援 OS 成功，缺 executable 不阻塞整個 Agent；命令參數、workspace confinement、角色與批准政策一致。Docker 沙箱缺 Docker 時仍 fail closed。
  - 依賴：P02、P06、P08、P09。

## M2：本機啟動與使用體驗

- [ ] **P11 — Launcher、單實例與背景 worker 生命週期**
  - 工作：建立 GUI／CLI 共用 launcher，管理三種模式、服務 health／readiness、埠衝突、自動開啟 UI、啟停／重啟／退出、日誌位置。明確決定 UI 關閉時停止或留背景；scheduler／memory／polling ownership 不因多開重複執行。
  - 交付：狀態機、可追蹤診斷、crash 復原、跨平台單實例機制；第二次啟動導向既有實例。
  - 驗收：快速多開、長任務退出、Ctrl+C、程序 crash、休眠喚醒、網路斷線均有測試；服務尚未就緒不顯示可用。沿用 polling lease／pending deadline；遠端 consumer 409 仍提示真實限制，不主動搶占或刪除 webhook。
  - 依賴：P06–P10。

- [ ] **P12 — 本機 Web、API、WebSocket 與 credentials 安全**
  - 工作：預設只 bind loopback；維持 API 身份／批准政策，設計本機登入、短期 session 或 launcher credentials。補 Origin／Host／CORS／CSRF／WebSocket 檢查與安全 bootstrap，避免為了開瀏覽器把 token 放 URL query 或日誌。對 LAN／外網監聽提供明確配置與驗證。
  - 交付：本機 UI 認證流程、credentials 儲存／輪替／去密、平台權限策略；若使用系統 keychain，另列其依賴與不可用時的受限 fallback。
  - 驗收：外部網頁不能藉 loopback 操作 Agent；錯誤 Origin／未授權 API／WebSocket 被拒絕；角色與批准不因本機包裝而變全 admin。簽章密鑰與 runtime download trust 不交由模型管理。
  - 依賴：P06、P11。

- [ ] **P13 — 首次設定與能力診斷 UI**
  - 工作：在既有 Web UI 加模型／embedding endpoint、workspace、角色、通訊模式、可選功能組、模型安裝與連線測試。整合 readiness／doctor，列出缺依賴、停用原因、下載進度、錯誤修復與日誌匯出；保留 CLI 設定方式。
  - 交付：全新安裝 onboarding、設定儲存／套用／重啟流程、能力狀態頁。
  - 驗收：缺模型、無網路、錯誤 API Key、埠被占用、無寫入權限皆可從 UI 處理；匯出 diagnostics 去密。測試只驗證連線與 capability，預設不發正式平台訊息。
  - 依賴：P06、P10–P12。

- [ ] **P14 — 模型與大型選配資源管理**
  - 工作：依授權與平台決策建立 Laya、local inference／embedding、Chromium／文件／語音資源的 manifest；區分內附與選配下載、CPU／GPU 能力、磁碟空間、checksum／來源、取消／斷點／原子完成、cache 與移除。外部模型服務可偵測與導引安裝，不強制產品包包含推論引擎。
  - 交付：資源管理與能力檢查、模型／引擎相容表、無 GPU 使用方式。
  - 驗收：下載中斷、損壞、空間不足、平台不相容與無網路有可恢復行為；未就緒的 guard 明確標示政策，不能藉失敗直接宣稱完整保護可用。單純把依賴改為 optional 不算成功提供該功能。
  - 依賴：P02、P06、P10、P13。

- [ ] **P15 — 安裝後核心功能與跨平台程序回歸**
  - 工作：建立適用來源／wheel／發行包的同一套 acceptance：CLI、UI／API／WS、工具／Skill／MCP、記憶、附件、任務恢復、排程、coordinator／media worker、角色／批准／workspace。支援功能使用真實本機執行，不只 mock；外部平台另列 opt-in。
  - 交付：安裝後 smoke runner、固定案例、故障注入與環境證據格式。
  - 驗收：無 repository、無開發機 Python、唯讀 app 路徑的環境可通過核心案例；每平台驗證程序樹終止、路徑逃逸拒絕與 DB 關閉。browser／Docker／provider／平台送達均分列結果；不能沿用既有 Windows source pytest 作為 macOS 發行通過。
  - 依賴：P07–P14。

## M3：建置與發行基礎

- [ ] **P16 — 製作可重現的各平台 portable bundle**
  - 工作：以各 OS／arch 的 runtime、鎖定依賴及套件資源產生可搬移的本機服務包；安裝後在目標機建立／修正必要路徑，不能直接搬開發機帶絕對路徑的 venv。建置全程與秘密設定、個人 DB／workspace 分離；驗證離線安裝所需 wheels／資源。
  - 交付：`distribution/` 建置腳本、portable ZIP／tar 包、版本／來源／依賴／checksum manifest、第三方 notices 與 SBOM。
  - 驗收：不同路徑解壓後可啟動，使用者資料不隨解壓覆蓋；archive 無路徑穿越、secret 或個人檔。可重現須定義 runtime／依賴／來源固定與差異原因，不誇稱每次簽章或 archive 時戳可 byte-identical。
  - 依賴：P02–P15。

- [ ] **P17 — 跨平台 build／test／release CI**
  - 工作：延伸 `.github/workflows/verification.yml`，分 source tests、wheel tests、portable tests、選配整合、release。加入 macOS，OS／arch runner 覆蓋 P01 矩陣；固定 toolchain、依賴與資源校驗，產出 evidence／checksums／SBOM。簽章 credentials 放 CI secret，pull request 不取得 production 發行權限。先完成 portable matrix 與 installer job 接入契約，平台 installer 的具體 jobs 與驗收由 P18–P20 各自補齊，不將尚未製作的安裝器算作本任務已驗收。
  - 交付：建置矩陣、artifact 保存、測試報告、tag／version 與發布 gate。
  - 驗收：每個發布 artifact 都是被驗收的同一版本／digest；缺目標 runner 或實機驗證不標為支援。僅配置 workflow 不等於已跑遠端 CI；外部發布依使用者授權進行。
  - 依賴：P15、P16。

## M4：首版跨平台交付

- [ ] **P18 — Windows x64 安裝包與實機驗收**
  - 工作：選定 per-user 安裝器，內附 runtime／launcher，建立開始選單／移除入口；不依賴 PowerShell 特殊 execution policy。預設非管理員執行；若需開機自啟，提供 opt-in 與清楚生命週期。正式對外發行評估程式／installer 簽章。
  - 交付：Windows portable 與 installer、安裝／設定／移除指引、證據。
  - 驗收：乾淨 Windows VM 無 Python／Git 等預設開發工具可啟動核心版；中文帳號／路徑、Program Files 唯讀、Job Object 取消、安裝中斷與移除皆驗證。用戶資料預設保留，清除須明確選擇；已知外部工具缺失可診斷。
  - 依賴：P16、P17。

- [ ] **P19 — macOS arm64 安裝包與實機驗收**
  - 工作：建立 app bundle／DMG 或符合首版服務包的 launcher，驗證 native wheels／可執行權限、Finder 啟動的 PATH、bundle 唯讀與 user data。準備正式簽章／notarization 的資格與 CI 流程，避免依賴開發者機器的 Gatekeeper 例外。
  - 交付：macOS arm64 發行包、最低 OS／依賴條件、安裝與驗證證據。
  - 驗收：乾淨 Apple Silicon 機器安裝、第一次啟動、模型／worker、睡眠喚醒、取消／退出、更新／移除可用；正式發行包檢查簽章／公證與 quarantine 後啟動。macOS x64 另依 P28 驗收。
  - 依賴：P18 已建立首版交付基線、P16、P17；不依賴 Electron 桌面外殼。

- [ ] **P20 — Linux x64 發行包與實機驗收**
  - 工作：決定最低 glibc／發行版與 desktop／headless 支援；優先 tar portable 或選定 DEB／AppImage。驗證 runtime／native libraries、browser 系統依賴、XDG 目錄、可執行權限與 POSIX 程序清理。自啟／systemd user service 為明確 opt-in。
  - 交付：Linux x64 發行包、支援發行版矩陣、安裝／服務設定／移除說明。
  - 驗收：最低支援發行版及一個代表性較新版環境通過；缺 GUI 或 Chromium 系統依賴可診斷。容器內測試不取代桌面／程序樹／實際安裝驗收，且不要求 root 使用 Agent。
  - 依賴：P19、P16、P17。

- [ ] **P21 — 核心發行包的更新、復原與移除**
  - 工作：首版先提供人工下載更新或受控 updater；下載校驗、版本相容、停止 worker、資料備份／migration、原子切換、啟動 health 確認、失敗復原。程式回退與資料降版分開處理；保留 release notes。移除可選清理 cache，個人狀態／workspace 不預設刪除。
  - 交付：更新／復原 manifest 與操作流程、原生更新入口、資料清理契約。
  - 驗收：從前一版有資料／進行中任務更新到新版；斷網、壞包、空間不足、schema 失敗、新版無法啟動均可安全恢復，不重放已送達訊息或外部命令。驗證各 OS 移除無殘留背景程序。
  - 依賴：P07、P11、P17–P20。

- [ ] **P22 — 發行文件、診斷與使用者支援資料**
  - 工作：更新 README 與平台指南：安裝、首次設定、三種模式、功能組、遠端／離線差異、sandbox、角色、Telegram 單 consumer／webhook 擇一、更新／移除與資料位置。整合去密 diagnostics bundle，列明版本、OS／arch、依賴、worker／模型狀態與可重現命令。
  - 交付：快速開始、功能限制、第三方 notices、疑難排解與回報範本。
  - 驗收：首次使用者按文件可完成設定；不包含開發者磁碟路徑、真實 Key 或已失效指令。診斷匯出前提供內容說明，排除個人對話／附件／credential。
  - 依賴：P13、P18–P21。

- [ ] **P23 — 首版跨平台 release candidate 與正式交付門檻**
  - 工作：對 P01 首版支援矩陣執行乾淨主機完整旅程：下載／校驗／安裝 → 首次設定 → CLI／UI 與核心 Agent → restart／取消／crash → 更新 → 移除／還原。保存每個 artifact 的 version／digest、機器環境、測試結果與限制；確認簽章／授權／無 secret。
  - 交付：三平台驗收報告、RC artifacts、已知問題與發布清單；獲授權後才發布正式下載。
  - 驗收：核心必需案例不得有未解決失敗；缺 provider／帳號／硬體的功能單獨列未驗收，不能用 source fixtures 覆蓋。若沒有 macOS／Linux 實機或簽章資格，先交付已通過的平台並如實標示，不宣稱全平台已完成。
  - 依賴：P01–P22；P01 指定為首版必需的選配任務亦必須完成。

## M5：後續選配產品與擴充

- [ ] **P24 — Docker Compose 本機／背景服務發行**
  - 工作：建立專用 Dockerfile／Compose、非 root、healthcheck、config／data／workspace volumes、固定依賴與架構標籤。使用 Docker secrets 或受限外部設定；預設不掛載 host Docker socket。說明 container Agent 不能直接使用 host executable，Docker 沙箱巢狀使用需獨立設計與風險決策。
  - 交付：Compose 部署包、備份／升版／權限說明與 image SBOM；image 發布另需授權。
  - 驗收：Windows／macOS Docker Desktop、Linux Docker 的目標組合實測，bind mount 的權限／中文路徑、資料持久化、退出／重啟、連 host 模型與網路邊界可用；不把 image 可啟動當完整 Agent 驗收。
  - 依賴：P16、P21、P22；選配，不阻擋首版 native 包。

- [ ] **P25 — 桌面外殼技術選型與最小 prototype**
  - 工作：優先評估 Electron Forge 包裝既有 Web UI；若評估 Tauri／其他 webview，比較 runtime 大小、平台 webview、sidecar、更新／簽章與團隊維護成本。用實際 prototype 比較啟動時間、RAM、worker 生命周期、中文輸入與 artifact 開啟，不僅比較理論包體。
  - 交付：選型 ADR、prototype 證據、支援平台與資源成本；桌面程式置於 `distribution/desktop/` 或選定專用目錄。
  - 驗收：能啟動受控後端、顯示既有 UI、正確退出；不能複製 Agent 核心或另建不相容 API。
  - 依賴：P11–P13、P16；選配。

- [ ] **P26 — 桌面版生命周期、系統整合與 IPC 安全**
  - 工作：實作單實例、system tray、前景／背景狀態、workspace 選擇、附件／artifact 開啟與通知。只載入受信任本機 UI；限制 IPC 方法、參數與外部導航，避免 renderer 取得任意 shell／檔案／credentials 權限。Web auth 與原 Agent RBAC／approval 仍一致。
  - 交付：桌面 UI、受限 bridge、sidecar 狀態監控、三平台互動測試。
  - 驗收：視窗關閉／系統登出／後端 crash／多開不留殘留 worker；外部網站／惡意內容不能呼叫 native bridge。桌面通知不洩漏個人對話，設定背景模式時有明確呈現。
  - 依賴：P12、P21、P25。

- [ ] **P27 — 桌面版 installers、簽章與自動更新**
  - 工作：依選型建立 Windows／macOS／Linux 桌面 installers、各平台簽章／公證、可信更新來源與更新提示；桌面 shell 與 Python 後端用共同相容 manifest。若啟用自動更新，驗證資料 migration、版本回退、channel 與下載失敗；沿用 P21 的資料安全策略。
  - 交付：桌面 release matrix、installer／update artifacts、簽章證據與手動更新 fallback。
  - 驗收：跨版本桌面＋後端配對正確；壞包／未簽章更新不能載入；完整三平台使用旅程通過才宣稱桌面版支援。憑證與發布權限缺失記為門檻，不繞過平台安全檢查。
  - 依賴：P17–P22、P26。
  - 首版前置：沿用核心平台包基線；若 P01 指定桌面版為首版必需，本任務先驗收，再進入 P23，避免循環依賴。

- [ ] **P28 — 擴充架構與平台相容範圍**
  - 工作：逐一評估 macOS x64、Windows arm64、Linux arm64 及更多 Linux 發行版；驗證 Python／native wheels、模型引擎、browser／MCP executable、最低 OS 與硬體。emulation 可另記，但不得等同 native 通過。
  - 交付：增量 runtime／installer／CI matrix 與支援表。
  - 驗收：每個新組合重跑 P15、P21、P23；桌面／Docker 宣稱也須跑相應驗收。沒有相容依賴或硬體時不提供標為正式支援的空包。
  - 依賴：P23；桌面／Docker 擴充另依 P24、P27。

- [ ] **P29 — PyInstaller／其他凍結方案可行性實驗**
  - 工作：在 P09 的 worker 契約上評估 onedir 優先、onefile 後續；驗證 dynamic imports、資源、原生 libraries、工具專案 venv、multiprocessing／worker dispatch。比較內附 interpreter 包的大小、啟動、維護與功能成本；實驗結果可為「不採用」。
  - 交付：各平台 prototype、spec／hooks、量測與採用／拒絕決策。
  - 驗收：不能因簡單 demo 可打包就宣稱整個 Agent 可用；採用時重跑完整安裝後功能驗收並按目標 OS／arch 建置。onefile 暫存解壓與子程序存活／清理均須驗證。
  - 依賴：P09、P15、P23；選配，不阻擋內附 runtime 首版。

- [ ] **P30 — 完全離線功能組與硬體品質驗收**
  - 工作：依 P14 選定本機文字／圖片／embedding、guard、文件與選配 STT／TTS 的引擎／模型，提供受授權的 offline resource bundle、離線安裝與 CPU／GPU profile。網路依賴工具明確停用；不在離線 mode 失敗時自動改用雲端。
  - 交付：離線能力矩陣、硬體需求、資源版本／授權、品質／延遲／RAM 報告、離線操作指南。
  - 驗收：完全斷網且無先前 cache 的乾淨環境可安裝／啟動；固定中文、圖片、記憶檢索與重啟案例驗證，網路外連監測為零。語音與特定 GPU／架構未測即列未支援；不以 runtime 在本機執行代表模型與工具已離線。
  - 依賴：P14、P15、P16–P22。
  - 首版前置：若為首版必需，先完成本任務再通過 P23，避免循環依賴。

## 工作紀錄與完成標準

1. 每項記錄實作來源、對應 commit、測試命令／結果、OS／arch、artifact digest、驗收證據與外部限制。只有文件規劃不勾選實作完成；選配未採用時記錄決策，不假勾為已實作。
2. 每次程式修改增加／更新相關測試，交付前相關測試與必要完整回歸通過；新增平台需實際安裝後驗證。文件單獨修改不新增程式測試，但須驗證編號、依賴、文件連結與 `git diff --check`。
3. 每次完成變更建立範圍明確的 commit；保留既有使用者修改，不把 `.env`、roles credentials、DB、模型 cache、workspace、build 輸出或簽章憑證納入 Git。
4. build、簽章、安裝、使用者旅程、外部 provider／正式平台、對外發布分別記錄結果。遠端 CI 設定已存在不等同 CI 執行成功，容器或模擬器結果不能直接等同 native 實機。
5. 更新程式不丟失個人資料、角色、批准政策或不確定工作狀態；更新後不自動重放外部副作用。不提供會悄悄降低沙箱或身份驗證的「修復」。

## 技術參考

以下用於技術選型；具體版本與支援矩陣須在 P01／P02 執行時再次確認。

- [uv Python versions](https://docs.astral.sh/uv/concepts/python-versions/)：managed Python 與平台／架構版本。
- [platformdirs](https://platformdirs.readthedocs.io/en/latest/)：跨平台使用者資料、設定與 cache 目錄。
- [PyInstaller operating modes](https://pyinstaller.org/en/stable/operating-mode.html)：平台分別建置、onedir／onefile。
- [PyInstaller runtime information](https://pyinstaller.org/en/stable/runtime-information.html)：bundle 資源與 `sys.executable` 的差異。
- [Electron packaging](https://www.electronjs.org/docs/latest/tutorial/tutorial-packaging)：Forge、平台 installers 與簽章／公證。

本次僅新增發行工作規劃，未修改程式、執行建置、平台安裝或對外發布。舊功能驗收不能代替本清單的安裝包驗收。
