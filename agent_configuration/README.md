# Agent 動態文字設定

本目錄收攏載入器、外部文字設定與回歸測試。

## 檢查結果與需求

原系統只有 `AGENTS.md`、Skill 的 `SKILL.md` 和動態工具 JSON 已外部化。
`server.py` 的主提示詞、核心工具與桌面工具說明、Laya 判斷問題、記憶萃取、
評估器、profile、環境使用規則與 coordinator persona 仍內嵌 Python。
初始化後的主提示詞和工具說明也沒有重新載入。

本次將這些文字移到 `data/` 的 UTF-8 Markdown / JSON，讓既有 CLI、
`communication_server.py`（Web / WebSocket / Telegram）及直接呼叫 `server.run_agent()`
共用載入路徑。既有 SkillRegistry 已在每次 match/get 時讀取檔案，沿用其行為。

## 設定檔

| 檔案 | 用途 |
| --- | --- |
| `system.md` | Agent 身分與主要行為 |
| `tool_usage.md` | 規劃、工具使用與完成任務的方法 |
| `safety.md` | 安全限制與不可信資料處理原則 |
| `tool_descriptions.json` | 核心及桌面工具與參數的說明；以工具名稱及 JSON 路徑索引 |
| `environment.json` | 共通、Windows、POSIX、container 使用規則；實際環境由程式偵測 |
| `guard_questions.json` | Laya 意圖、風險與確認判斷 |
| `memory_questions.json`、`relevance_questions.json`、`preservation_questions.json` | 記憶分類、排序、保留問題 |
| `semantic_extraction.md`、`procedure_similarity.md` | 語意記憶萃取與程序比對 |
| `profile_extraction.md`、`skill_doctor.md` | Profile 萃取及診斷 |
| `personas.json` | Planner、Coder、Reviewer 文字 |
| `instructions.json` | 評估、報告、修正回饋、檢索及 profile 等共用文字與模板 |

`tools/*.json` 的既有工具說明也會在模型請求前重新讀取；MCP 與經核准擴充工具的
描述仍由各自來源提供。開發用舊腳本（例如 `fix_*.py`、`create_delegate_tool.py`）
不是正式 Agent 設定入口。

## 載入時機

1. `server.build_system_prompt()`：每次初始化讀取主提示詞、工具方法、安全限制及專案根目錄的 `AGENTS.md`。
2. `agent_runtime.run_agent_turn()`：每次任務開始更新 managed instructions，包含 Skill 直接執行路徑。
3. `server.run_agent()`：直接執行和 checkpoint 恢復也重新讀取；保留 persona、profile、歷史及 tool call ID。
4. 模型請求：重讀工具文字；只更新 description，不藉此變更已註冊函式、參數型別或權限。
5. 輔助評估器及分類器：每次呼叫讀取自己的文字檔案。

不使用 import-time 提示詞快照或載入快取。更新檔案後，下一個任務即可套用；
已送出的模型請求不會被追溯修改。升級本次 Python 程式後，應重新啟動服務；之後只改文字檔不必重啟。

預設路徑相對於套件檔案位置解析，不依賴啟動時的工作目錄。可在啟動前指定完整替代目錄：

```powershell
Copy-Item -Recurse .\agent_configuration\data C:\PythonAIConfig
$env:AGENT_CONFIG_DIR = 'C:\PythonAIConfig'
.\venv\Scripts\python.exe communication_server.py
```

替代目錄需包含完整的設定檔，沒有隱式回退到預設目錄。產生器應先寫入暫存檔，
完成後以 `os.replace()` 原子替換目標，避免讀到未寫完的內容。
Markdown 必須非空；JSON 必須有效，工具說明必須是非空字串。
主提示詞、工具或安全設定無法載入時會回報 `ConfigurationError`，不使用舊快取繼續執行。
輔助記憶服務仍保留既有的離線/失敗降級流程。

模板採用 `string.Template` 的 `${name}`。替換內容視為原文，不執行 Python、Jinja 或
其中再出現的模板。保留既有 key 與必要欄位；純 JSON 範例的 `{}` 不須跳脫。

設定目錄是受信任的管理設定來源，部署時應讓一般使用者無法改寫。
外部化的是給模型的文字；RBAC、workspace 邊界、可用工具清單、核准機制及參數驗證
仍由程式執行。修改文字不能賦予額外權限。

## 驗收

```powershell
.\venv\Scripts\python.exe -m pytest agent_configuration/tests -q
.\venv\Scripts\python.exe -m pytest -q
git diff --check
```

測試以模型替身檢查實際送出的 messages / tools，涵蓋初始化、既有對話的下一次任務、
輔助服務、工具說明、設定錯誤、模板、Unicode 及歷史保留。完整 suite 包含 CLI、
communication runtime、WebSocket、排程與 checkpoint 路徑；這些是離線回歸證據，
不代表已驗證遠端供應商、部署服務或真實使用者裝置。
