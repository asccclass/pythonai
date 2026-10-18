# PythonAI Harness² Probe

本目錄將既有 `server.run_agent()` ReAct 工具迴圈接到 Harness² 的
`BenchmarkAdapter`。也提供單次任務的 JSON CLI，無須先安裝 Harness²。
程式、範例、測試與規格都集中於本目錄。

## 執行環境

使用本專案已安裝 `requirements.txt` 的 Python 環境。沿用 PythonAI 的
`OLLAMA_BASE_URL`、`OLLAMA_MODEL`、`OLLAMA_API_KEY` 設定（環境變數或專案 `.env`）。
不要把憑證放入 Task JSON、harness 或版控。

在專案根目錄執行（PowerShell）：

```powershell
venv/Scripts/python.exe -m harness_probe --task harness_probe/examples/task.json --runs-root harness_probe/runs --run-id hello-001 --timeout 300
```

可使用 `--task -` 從 stdin 讀取一個 JSON object；每次 CLI 處理一個 Task。
stdout 為一個 JSON 結果；Agent 的 stdout/stderr 另存檔案。
exit code：`0` 完成、`1` 執行失敗／逾時／等待人工處理、`2` 請求無效。

## Task 與結果

Task 必填 `id`、`prompt`；選填 `domain`、`split`、`files_dir`。
CLI 檔案輸入與 Adapter manifest 的 `files_dir` 相對於該 JSON 檔；stdin 與
直接呼叫 `ProbeRunner` 則相對於呼叫端 cwd。來源檔案複製到 rollout 的 `workspace/inputs/`。
只提供任務可見素材；不得把答案、rubric 或評分用資料放入來源目錄。

結果包含 `schema_version`、`task_id`、`run_id`、`status`、`error`、`exit_code`、
`wall_s`、`final_output`、`artifacts`、`fingerprint`、`run_dir`。
`artifacts` 記錄 workspace 內產出的相對路徑、大小與 SHA-256，排除 inputs 與 harness。
`completed` 代表 Agent 正常返回，並非外部評分已通過。`failed`、`timeout`、
`blocked` 分別表示執行失敗、逾時與需要核准／補充資訊。

每次執行保存在 `runs-root/<SHA256(run_id)>/`：

* `request.json`：任務與執行 ID。
* `events.jsonl`：即時 flush 的模型文字 delta、工具開始／結果、核准與錯誤事件。
* `messages.json`：Agent 對話與工具訊息快照（正常退出或可捕捉例外時寫入）。
* `stdout.log`、`stderr.log`：完整程序輸出與 traceback。
* `answer.txt`、`worker_result.json`、`result.json`：最終文字與執行狀態。
* `workspace/`：任務素材、候選 harness、Agent 產出。
* `state/`：此 rollout 專屬的任務、核准、追蹤等資料庫。

逾時會停止 worker 程序樹並保留已寫入的事件與產出，不能保證外部副作用已取消。
相同 `run_id` 與相同輸入直接讀回既有終態結果；輸入或 timeout 改變會拒絕共用 ID。
中斷或仍在執行的目錄會拒絕重跑，須先檢查紀錄並使用新 ID。
`run_id` 為不含控制字元的 1..1024 字元識別值，可包含 Harness² 原生的 `/` 分隔；
實際目錄僅使用其 SHA-256，不將 ID 當成路徑。
此為執行去重，並非跨版本、模型或環境變更後的可重現性保證。

## 接入 Harness²

已對照上游 `google-research/harness2` commit
`1cfe254e8d25b15f37048c3730134686abe60335` 的
[`BenchmarkAdapter`](https://github.com/google-research/harness2/blob/1cfe254e8d25b15f37048c3730134686abe60335/harness2/core/spec.py)。
上游建議 Linux / Python 3.12；本 Wrapper 的獨立 CLI 可在 Windows 執行。
Harness² 請另外依其官方文件安裝，並讓同一 Python 環境能 import 本專案與 `harness2`。
不要將上游原始碼複製到本目錄。

```python
from pathlib import Path
from harness2.core.spec import ChannelSpec
from harness_probe.adapter import PythonAIAdapter

channel = ChannelSpec("opencode", "your-provider/your-improver-model")
adapter = PythonAIAdapter(
    "harness_probe/examples/tasks.json",
    "harness_probe/runs/experiment-001",
    channels={"proposer": channel, "selector": channel},
    timeout=300,
)

# Harness² Probe 呼叫的原生介面：
run_dir = adapter.execute("hello-1", harness_dir=None, run_id="baseline-001")
print(adapter.trajectory(run_dir, obs_trunc=50000))
print(adapter.deliverable_files(run_dir))
```

將 `adapter` 傳給 Harness² `core.recursion.run_parallel()`、`run_sequential()`
或既有 Python orchestration；`harness_dir` 指向該次候選 harness。
本實作沒有替上游內建 CLI 新增 `--substrate pythonai` 選項。
改善模型的 `channels` 與 PythonAI 執行模型的 `OLLAMA_*` 設定分開。

目前支援的 editable surface 為 `systemprompt.md`、`guardrails.md`、`memory.md`、
`tool_guidance.md` 與 `scripts/[a-z0-9_]+.py`。四個 Markdown 以額外 system 訊息
送入 Agent，scripts 複製到 `workspace/harness/scripts/` 供工具執行。
修改內容不會覆寫專案本身的系統規則、工具註冊或權限政策。
本版拒絕尚未支援的 skills、subagents、plugins、hooks，避免候選修改被靜默忽略。
`memory.md` 是此候選的靜態記憶內容；本版不啟動互動客戶端的長期記憶 worker。

`trajectory()` 使用 `[ACTION]`／`[OBSERVATION]`／`[ERROR]`／`[ASSISTANT]` 格式。
呈現可截斷，但原始事件仍完整保存。文字產出可透過 `deliverable_files()` 讀取；
二進位產出保存在 workspace 並附 hash。
`score()` 預設返回 `None`，可透過建構子的 `judge(run_dir, task_id)` 注入外部評分器；
不會自行捏造分數，也不會將外部評分資料傳給 Agent。

## 權限與執行界線

每次 rollout 使用獨立 subprocess、workspace、資料庫與 budget；保留既有工具政策、
hooks、核准與角色檢查。worker 沿用呼叫端角色，直接啟動 worker 的預設為 viewer。
CLI 的本機角色沿用 PythonAI 的本機預設。這是狀態隔離，並非 OS 安全沙箱；
工具的 host/container 模式仍依原專案設定。執行紀錄與 Agent 產出可能含任務敏感內容，
請保留於受控的本機目錄。本目錄的 `runs/` 已加入忽略規則。

## 驗證

```powershell
venv/Scripts/python.exe -m pytest harness_probe/tests -q
# 上游契約測試需讓 harness2 可 import；未安裝時只有此項會 skip。
```

測試包含真實 subprocess、UTF-8、候選 prompt、來源與產出、事件、例外、
provider 錯誤、核准／暫停、執行上限、逾時、角色繼承、輸入驗證、去重，
以及既有 `server.run_agent()` 搭配受控模型回應執行真正 `write_file` 工具。
另有完整 CLI → 正式 worker → 本機串流 HTTP 模型服務 → 真實工具的整合測試。
受控測試不代表真實 provider 或正式 benchmark 已驗證。
