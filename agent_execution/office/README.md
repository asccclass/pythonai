# Office 架構、目錄與原生派工（R0–R2）

本套件提供 owner、引擎能力、事件、設定契約，以及 owner-scoped 員工／團隊／成員 CRUD、版本遷移與備份。完整規劃見 [rebuild.md](../../papers/rebuild.md)，設計決策見 [ADR 0001](docs/0001-office-boundaries.md) 與 [ADR 0002](docs/0002-office-directory.md)。

R1.4 新增 assignment／operation／outbox 與 request ID 冪等、EventStore／ApprovalStore 收據對帳、容量限制及三 DB 清理，契約見 [ADR 0003](docs/0003-office-intents.md)，實測見 [R1.4 驗證](docs/intents-validation.md)。R2 的原生 adapter／可信 admission／FIFO、lease、預算及取消見 [ADR 0004](docs/0004-native-dispatch.md)。

`OfficeStore` 使用應用程式既有 TaskStore，明確呼叫 `migrate()` 才安裝 office 資料表；本次未遷移正在使用的資料庫。套件沒有註冊模型工具、HTTP／WS route 或啟動派工 worker。`AGENT_OFFICE_ENABLED` 預設為 `0`，目前仍未接入服務 startup；設定為 `1` 不會自行啟動執行器。provider 設定可記錄 pythonai／codex／claude，實際 adapter 能力須另行驗證。

## 模組

| 模組 | 職責 |
| --- | --- |
| `contracts.py` | `OwnerScope` 與 TaskStore owner key 相容；`RuntimeCapabilities` 預設不宣告能力；immutable `RuntimeStartRequest`／`TurnReceipt`。 |
| `config.py` | 明確載入的 immutable `OfficeSettings`，驗證 concurrency／queue／approval TTL，不修改環境。 |
| `models.py` | 員工／團隊輸入驗證與 public DTO；只記錄 logical workspace project，不接受路徑或憑證欄位。 |
| `store.py` | TaskStore DB 內 `office_*` schema 0→1→2→3、離線備份與原子 migration、schema 相容檢查。 |
| `directory.py` | owner-scoped CRUD、revision CAS、成員／主管管理、停用及刪除規則。 |
| `ledger.py`／`persistence.py` | 不可變 request snapshot、任務 ID 保留、operation／outbox 與 row／byte 上限。 |
| `reconcile.py`／`targets.py` | sender lease／token fencing、固定收據對帳、pending 審批／事件與取消補償、目標 owner hash fence。 |
| `runtimes/base.py` | `AgentSessionRuntime`／`ResumableRuntime`／`ToolApprovalRuntime` Protocol 及帶 generation／turn 的 immutable JSON 事件。 |
| `examples/` | 預設關閉的設定與契約樣本；能力樣本不代表任何引擎已驗收。 |

Owner 必須來自既有認證入口建立的 execution scope，不能取自使用者 JSON。`OfficeDirectory` 每次操作自行取得 owner；viewer／operator 可讀自己的資料，修改需 admin。團隊 lead 身份不授予 admin。`OwnerScope.from_current()` 僅複製 scope，不會認證身份；後續執行服務仍需套用 role、path、lifecycle、budget、approval 檢查。

`RuntimeCapabilities.require(writes=True)` 只有在引擎能於所有工具路徑執行前攔截時才接受；它是能力檢查，不能取代授權。`session` 恢復只代表接續供應商對話，`durable` 才代表恢復確認過的執行邊界。任何引擎實作宣告這些能力前，都需版本固定的實測。

設定詳見 [office.env.example](examples/office.env.example)。Concurrency 為 1..64，owner 不大於 global；單 owner queue 上限為 1..10000；待批 TTL 為 1..604800 秒，300 秒僅是第一版可配置預設，尚未接入 ApprovalStore。

## 明確遷移與 CRUD

先停止使用相同 DB 的 worker。Office migration 要求可信 admin、`offline=True`，且備份目錄必須是所有已配置 project workspace 與目前 workspace 之外的新目錄；不是由模型工具執行。TaskStore 自身若也需舊版升級，先使用既有 `agent_execution.operations.backup.migrate('tasks', ..., offline=True)` 的備份升級流程。本 migrator 僅負責 office schema，不取代核心資料庫的升級流程。

```python
from pathlib import Path
from agent_execution.tasks import TaskStore
from agent_execution.office.store import OfficeStore
from agent_execution.office.directory import OfficeDirectory
from agent_execution.office.models import StaffConfig, TeamConfig, TeamRole

# 由可信應用程式提供原本的 TaskStore，沿用 AGENT_TASK_DB。
store = OfficeStore(TaskStore())
# 操作人員先選定 workspace 外、尚不存在的備份目錄。
store.migrate(Path(backup_directory), offline=True)
directory = OfficeDirectory(store)
# 以下在既有 transport 建立的 execution_scope 與 role scope 中呼叫。
staff = directory.create_staff(StaffConfig("研究員", enabled=True, allowed_tools=frozenset({"read_file"})))
team = directory.create_team(TeamConfig("研究團隊"))
team = directory.set_member(team.id, staff.id, TeamRole.LEAD, expected_revision=team.revision)
team = directory.update_team(team.id, TeamConfig(team.config.name, enabled=True), expected_revision=team.revision)
```

備份沿用 [operations backup](../operations/backup.py) 的 manifest／SHA-256／SQLite snapshot。restore 只寫入新目錄；回滾時先停止所有 writer，驗證備份並將原服務的 DB 設定改指向還原副本，不覆蓋或刪除目前 DB。未知版本、遺失索引、未完成 schema 都拒絕讀寫與升級；同版 migration 為 no-op。

遷移後重新啟動本次版本的後端，以載入 office metadata 的 erasure hook。若還原資料庫含既有 backend／host queue／路徑綁定，須依原模組規則核對後再啟動；本階段驗證 SQLite 還原，不涵蓋 Temporal 等外部服務的災難復原。

R1.4 target 固定 source origin；還原至不同 TaskStore 路徑時，須依 ADR 0003 停機核對三方收據與未決效果。本階段沒有自動 rebind 或單 DB 回退後的自動重送。

| 資源 | 方法 |
| --- | --- |
| 員工 | `create_staff`、`get_staff`、`list_staff`、`update_staff`、`delete_staff` |
| 團隊 | `create_team`、`get_team`、`list_teams`、`update_team`、`delete_team` |
| 成員 | `set_member`、`get_member`、`list_members`、`remove_member` |

staff／team list 支援 `limit=1..100` 與 `after_id`，以穩定 ID 排序。更新／刪除與成員變更必填 `expected_revision`；成員修改使用 team revision，主管移轉會在同一交易降任原主管並提升新主管。

每個 owner 最多 200 筆員工、100 筆團隊，每團隊最多 64 位成員。工具集合預設空，只接受目前已註冊且在 caller tool ceiling 內的名稱；未來執行仍須重新檢查權限。workspace 每次依 owner 的 project mapping 驗證，回應不包含絕對路徑、owner key 或認證設定。名稱採 NFC 顯示、NFKC＋casefold 去重，同 owner 的有效名稱不可重複。

員工必須停用且移除所有成員關係後才能刪除；團隊必須停用且清空成員。刪除採 tombstone 並保留 revision／歷史 ID，tombstone 也占容量；名稱可重用，舊 ID 不復活。啟用團隊需有已啟用的主管；須先移轉主管或停用團隊，才能停用／移除／降任現任主管。

建立與修改時沿用 LifecycleStore 登記會話所有權；既有 `delete_session` 的 outbox finalize 階段會在同一 TaskStore transaction 清除該 owner 的 staff／team／member 資料，其他 owner 保留。會話進入 deleting／deleted 後，讀寫都拒絕。資料庫備份依既有外部保存政策處理，不因刪除會話自動刪除。

## 驗證與下一步

從儲存庫根目錄執行：

```powershell
.\venv\Scripts\python.exe -m pytest agent_execution/office/tests -q
.\venv\Scripts\python.exe -m pytest -q
.\venv\Scripts\python.exe -m pip check
git diff --check
```

測試已納入根目錄 `pytest.ini`。R2 已加入 runtime lease／generation／budget 綁定；R3 接續完整工具審批，R4 才公開入口，不以 ready 觀察狀態代替執行授權。

## 持久意圖與對帳 API

Office v2／v3 資料庫須先依上述停機備份流程執行 `store.migrate(..., offline=True)` 至 v4。程式庫在既有可信 role／execution scope 中呼叫：

```python
from agent_execution.office.ledger import OfficeLedger
from agent_execution.office.reconcile import OfficeReconciler
from agent_execution.delivery import EventStore
from agent_execution.approvals import ApprovalStore

ledger = OfficeLedger(directory)
work = ledger.submit("request-1", staff.id, "整理資料", staff_revision=staff.revision)
# staff 先前須明確允許已註冊的 read_file；參數仍是待審意圖。
operation = ledger.prepare_operation(work["id"], "tool-1", "read_file", {"path": "input.txt"})
sender = OfficeReconciler(ledger, events=EventStore(), approvals=ApprovalStore())
sender.reconcile(limit=20)
# 停止此意圖並對帳 target 取消收據。
ledger.cancel_assignment(work["id"])
sender.reconcile(limit=20)
```

`get_assignment`／`get_operation`、`list_assignments`／`list_outbox` 回傳 allowlist metadata，不回傳完整 payload／參數／DB 路徑。相同 request ID 的相同內容回傳原 ID，不同內容拒絕；作用域為完整 owner，operation 再加 assignment。停止意圖不等於已清除 target：cancel_pending／uncertain 會阻擋相關設定變更與會話刪除，須先對帳或人工處理未知效果。

R1.4 只保留 task ID 與 goal／criteria snapshot，不建立可由舊入口執行的 core TaskStore task。Events 使用完整 owner key 的專用 feed，approvals 使用 `(identity, owner.key)`；尚未接上舊 dashboard／device 的 namespace 或審批 UI。收到 approved 不代表工具已執行，reconciler 也不消耗 grant。

## R2 原生派工 API

以下由可信應用程式在已認證的 admin／execution scope 中呼叫，使用已遷移至 v4 的 directory。必須明確設定 `AGENT_OFFICE_ENABLED=1`；不需也不應透過修改環境變數切換員工模型。

```python
from agent_execution.office.config import OfficeSettings
from agent_execution.office.dispatcher import OfficeDispatcher

ledger = OfficeLedger(directory, settings=OfficeSettings.load())
dispatcher = OfficeDispatcher(ledger, lease_seconds=30, timeout_seconds=600)
work = ledger.submit("job-1", staff.id, "讀取 input.txt 並摘要", staff_revision=staff.revision)
await dispatcher.run_once()  # 一次原生工作；多 worker 共用 DB／限制。
dispatcher.reconcile()      # 重啟或輪詢時，將過期執行標為 uncertain。

# 自動路由回傳 assignment 與可解釋原因；重送同 request 保持原指派。
routed = dispatcher.submit_auto("job-2", "整理可讀資料")
dispatcher.set_staff_state(staff.id, paused=True)  # 保留 queued、取消 active。
dispatcher.cancel(routed["assignment"]["id"])
```

`set_staff_state(..., paused=True, disable=True)` 會停用員工；重新指派使用 `reassign(assignment_id, new_staff_id, expected_staff_id=old_staff_id, staff_revision=new_revision)`，僅限未啟動且沒有 operation 的個人工作。已有團隊／operation 綁定的工作須取消後重新建立。終態 outbox 仍透過 OfficeReconciler 對帳。

adapter 使用每任務獨立 history／checkpoint，不啟用跨任務 MemoryStore 或背景記憶；預算共用原生 TaskBudget，所有 model／evaluator 呼叫都受相同根限制。interrupt 為 cooperative：無法確認 SDK thread 停止時，保留 uncertain lease 容量與清理阻擋。R2 不自動 replay 未知執行，也不宣告 session／durable resume。完整能力、恢復限制及資料清理順序見 [ADR 0004](docs/0004-native-dispatch.md)，驗證見 [R2 紀錄](docs/runtime-validation.md)。
