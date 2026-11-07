# Office 架構基礎（R0／R1.1）

本套件目前提供 owner、引擎能力、啟動要求、事件及設定契約，作為後續員工管理、派工與審批的共同邊界。完整規劃見 [rebuild.md](../../papers/rebuild.md)，設計決策見 [ADR 0001](docs/0001-office-boundaries.md)。

目前沒有建立 DB、啟動 worker、註冊模型工具或 HTTP／WS route，也沒有實作／載入 Claude／Codex adapter。既有 `agent_runtime.AgentRuntime`、CLI 及 communication server 繼續使用原路徑。`AGENT_OFFICE_ENABLED` 預設為 `0`；目前只是未來 admission 的設定值，設為 `1` 也不會啟動服務或取得任何工具權限。

## 模組

| 模組 | 職責 |
| --- | --- |
| `contracts.py` | `OwnerScope` 與 TaskStore owner key 相容；`RuntimeCapabilities` 預設不宣告能力；immutable `RuntimeStartRequest`／`TurnReceipt`。 |
| `config.py` | 明確載入的 immutable `OfficeSettings`，驗證 concurrency／queue／approval TTL，不修改環境。 |
| `runtimes/base.py` | `AgentSessionRuntime`／`ResumableRuntime`／`ToolApprovalRuntime` Protocol 及帶 generation／turn 的 immutable JSON 事件。 |
| `examples/` | 預設關閉的設定與契約樣本；能力樣本不代表任何引擎已驗收。 |

Owner 必須來自既有認證入口建立的 execution scope，不能取自使用者 JSON。`OwnerScope.from_current()` 僅複製 scope，不會認證身份；`RuntimeStartRequest` 對絕對路徑的檢查也不是 workspace 授權。後續 service 必須重用 role、path、lifecycle、budget、approval 檢查。

`RuntimeCapabilities.require(writes=True)` 只有在引擎能於所有工具路徑執行前攔截時才接受；它是能力檢查，不能取代授權。`session` 恢復只代表接續供應商對話，`durable` 才代表恢復確認過的執行邊界。任何引擎實作宣告這些能力前，都需版本固定的實測。

設定詳見 [office.env.example](examples/office.env.example)。Concurrency 為 1..64，owner 不大於 global；單 owner queue 上限為 1..10000；待批 TTL 為 1..604800 秒，300 秒僅是第一版可配置預設，尚未接入 ApprovalStore。

## 驗證與下一步

從儲存庫根目錄執行：

```powershell
.\venv\Scripts\python.exe -m pytest agent_execution/office/tests -q
.\venv\Scripts\python.exe -m pytest -q
.\venv\Scripts\python.exe -m pip check
git diff --check
```

測試已納入根目錄 `pytest.ini`。下一階段為 R1.2／R1.3：在 TaskStore DB 加入版本化 `office_*` schema 與 owner-scoped 員工／團隊管理；在 R2／R3 完成持久派工及審批前，不接上可執行服務入口。
