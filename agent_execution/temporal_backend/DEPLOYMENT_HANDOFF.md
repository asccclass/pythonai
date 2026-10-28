# TP25–TP30 正式環境驗收交接

更新：2026-10-29。此文件列出實際上線前需要的環境與證據；未勾選的項目尚未完成。
隔離 development Service、mock provider、Windows venv 測試各自保留原驗證範圍。

使用者本輪選擇「還沒決定，先完成本機版本」。當前執行範圍為 Windows 同機 venv／原有入口；
正式服務選型、Linux、多機及生產 legacy 移除延後，仍保留原 TP 驗收要求。
Windows frozen executable 亦須另有 build 與實際啟動證據，不能由 venv 驗證推定完成。

本機已有 `temp/temporal-tools/1.9.1/temporal.exe`（不入 Git），即使 PATH 沒有 temporal 仍可使用。
從 repo 目錄執行該檔的 `server start-dev` 指令，即可沿用 backend README 的開發服務步驟；
服務持久資料放在 `%LOCALAPPDATA%/PythonAI/temporal-dev`，前端與 Worker 使用同一 TaskStore、
workspace、provider 與 namespace。不要建立第二份資料庫來接手既有 run。

## 用一般使用者的方式理解目前缺什麼

目前程式可以在你的 Windows 電腦上驗證，但尚未指定日常服務要運行的地方。
「自管」是由你管理 Temporal 服務及其資料庫；「Cloud」是使用託管服務。
兩種方式都還需要安裝 PythonAI Worker、設定存取權、保存應用資料並演練還原。
現有 Settings 仍限制 loopback，故不能只換遠端地址就宣稱支援 Cloud 或多機。

備份需求中的 RPO 是可接受遺失多久的資料，RTO 是故障後可接受多久恢復。
例如「最多遺失一小時資料、四小時內恢復」是一組需求範例，並非目前已達到的成績。

只需先確認服務要一直運行在目前這台 Windows，還是已有另一台伺服器可用。
後續再決定通知接收端、備份位置及跨機方式。Secrets 留在主機設定中，文件只保存參照。

## 驗收缺口與必需證據

| 項目 | 尚需完成的工作 | 結案證據 |
| --- | --- | --- |
| TP25 | 設定持續執行的 health checker、告警接收端及正式觀測 | 接收端實際收到故障通知；正式 Worker／Service metrics 與保留策略 |
| TP26 | 選定環境；完成遠端認證／TLS 連線支援、部署程序、完整資料備份與權限 | 鎖定版本、部署記錄、原生服務備份、在新環境還原、量得 RPO/RTO |
| TP27 | 正式 owner 放量、壓測與持續觀察 | 真實流量／延遲／失敗率、至少七日或完整排程週期、停止條件及回滾紀錄 |
| TP28 | 建立 frozen build、保留三種啟動模式、Windows/Linux 能力驗證 | 可重現 build 及各 OS 實際啟動、Worker、資源載入證據 |
| TP29 | 集中化資料權威、artifacts、secrets 及 workspace 路由；實作與遷移 | 第二台授權 Worker 接手原 run，原主機中斷後預算／審批／副作用 fencing 正確 |
| TP30 | 遠端 executions／schedules 與所有 legacy worker 排空、觀察及回滾期結束 | 各環境 inventory、保留期與業務驗收紀錄；通過後才刪除 legacy 程式 |

目前沒有正式部署／生產流量、Linux/frozen 實機或第二台 Worker 接手證據。
不能以本機 drain 計數為零代替所有環境排空，也不能把 SQLite 檔案硬共享當成集中化權威。

## 可先使用的告警命令

在與日常服務相同的設定、服務帳號及 workspace 下執行：

```powershell
.\venv\Scripts\python.exe -m agent_execution.operations alerts
# 在主機私有環境設定 AGENT_OPS_ALERT_WEBHOOK_URL 及可選 AGENT_OPS_ALERT_TOKEN 後：
.\venv\Scripts\python.exe -m agent_execution.operations alerts --deliver
```

接收端須接受 HTTPS POST，JSON 格式為 `component=pythonai` 與固定 alert code 陣列。
目前代碼為 `readiness_failed` 和 `trace_store_unavailable`；內容不含 task、owner、URL、錯誤正文。
token 若設定則使用 Bearer header。只嘗試一次、不跟隨重新導向，避免把 token 轉送到其他位置。
timeout／錯誤只回報錯誤類型，投遞失敗由操作方處理。

此為單次檢查；持續排程、去重、通知抑制、恢復通知與實際人員接收驗收仍需部署設定。
有告警時 CLI 回傳 exit code 1，即使接收端已接受通知；HTTP 成功只代表接收端接受請求。
