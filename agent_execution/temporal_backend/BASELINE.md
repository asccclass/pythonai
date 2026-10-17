# TP01–TP03 基準與技術驗證

日期：2026-10-17，Asia/Taipei。

- 程式基準：`ceb7c80eaa0c8c0cf293e540370a425ea913efc5`。
- 基準完整套件：**802 passed、8 skipped、95 subtests passed**，622.34 秒。紀錄 `temp/temporal-baseline-pytest.log`（不入 Git）。執行期間未修改既有 tracked source；已新增尚未進入 testpaths 的 backend 草稿，並安裝 optional SDK，不將此結果稱為乾淨安裝環境驗證。
- 安裝前／後 `pip check` 均無衝突。SDK 1.34.0 與目前 Python 3.12.0、protobuf 7.36.2 相容；只新增三項 optional packages，未覆蓋根 requirements。
- 官方 CLI 1.9.1 ZIP checksum 已核對，版本輸出 Server 1.32.0／UI 2.54.1；實體 CLI binary 位於 ignored temp，未安裝到系統 PATH。
- Windows 真實持久化 development service 測試：重複提交只執行一次、History 不含 private prompt、Replayer 通過、重新啟動服務後仍取回相同結果；Activity 失敗一次即停止。Linux／macOS／ARM 尚無實機驗證，所以 TP02 的跨平台部分仍待完成。

## 原有改動保護

開始時已存在 api、tracing、policy、scheduling、workspace lifecycle、communication models/store/worker、web 與相關測試的未提交改動；另有未追蹤的 communication/workspace HTTP 測試與模組，以及 `workspace/communication.db` 刪除狀態。這些不屬此次提交範圍。未清空、還原或暫存它們，也未將其視為本次已交付功能。

本次新 backend 集中在 `agent_execution/temporal_backend/`，必要共用接點只修改 `agent_execution/tasks.py`、`pytest.ini`、`.env.example` 與導入規劃的進度紀錄；提交前逐一核對 paths。

## 備份與遷移前置

`python -m agent_execution.temporal_backend.preflight` 只列設定、SDK 版本及 tasks/approvals/schedules/tenants 備份來源，不建 DB、不備份、不連線；本機清單在 `temp/temporal-preflight.json`，不入 Git。使用者正式資料庫尚未進行 Temporal schema migration，驗證使用隔離測試資料。

對正式 DB 啟用前，先以 SQLite backup API／一致性快照備份 tasks.db（含 budgets/coordinator/lifecycle）、approvals.db、scheduler.db、tenant profile/memory、實際 communication DB 及 EventStore，另備份 worktrees、artifacts、受控設定與憑證 reference。若 WAL 模式，不直接只複製主 DB 檔。先做停派工／排空、復原演練並保存版本清單，才能切換新任務；不可「還原 DB」後盲重放已產生副作用的任務。

ADR 詳見同目錄 README：Python SDK、same-host loopback、單一 coarse Activity max-attempts=1、既有預算與權限不變、reference-only History，後續正式工具級流程仍按 TP09–TP30 推進。
