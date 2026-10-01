# 通訊軟體指揮 Agent 規劃 (Communication Plan for Agent Control)

## 目標 (Goal)

本計畫旨在讓使用者能夠透過常見的通訊軟體（例如 LINE、WhatsApp、Telegram、Discord）向本專案的 Agent 下達指令、接收結果並確認高風險操作，同時確保保留現有 CLI Agent 的安全邊界、記憶系統與工具執行流程。

**核心原則：**

- **通道分離：** 通訊軟體僅作為新的輸入/輸出通道，不得繞過現有的 Agent 核心、工具鏈、Laya guard、命令確認機制與片段記憶 (episodic memory)。
- **身分驗證：** 每條外部訊息必須經過來源驗證、使用者辨識並套用權限控制，最後才轉換為統一的內部 `AgentCommand`。
- **風險控制：** 高風險操作必須經過二階段確認，不因來自通訊平台而自動執行。
- **非同步處理：** 長任務必須非同步執行。通訊平台 Webhook 應快速回應以避免平台重發請求或判定為失敗。
- **平台抽象：** 各平台的差異應封裝在 Adapter 層，核心 Agent 邏輯不應依賴於特定平台的 Payload 格式。

## 平台分析與限制 (Platform Facts & Constraints)

### LINE
LINE Messaging API 在收到訊息時會向 Webhook URL 發送 HTTPS POST。
- **設計要點：** 必須實作 `X-Line-Signature` 驗證以確保請求未被竄改；Webhook Handler 應僅負責驗證、轉換與入列，並快速回覆。
- **回覆機制：** 使用 `reply token` 回覆；若超過時效或為背景任務完成，則需改用 `push message`。

### WhatsApp Business Platform
提供 Cloud API 與 Webhooks，需經由 Meta App 與 WhatsApp Business Account 設定。
- **設計要點：** 需處理 Meta 的驗證 Challenge 與 POST 事件；發送訊息透過 Graph API。
- **注意：** WhatsApp 對商業訊息、模板與服務對話有嚴格的費用與規則，需將通知類與回覆類訊息分開設計。

### Telegram
Telegram Bot API 支援 Webhook 或 Long Polling，對開發者相對友善。
- **設計要點：** 可作為首個端到端 Adapter 用於驗證架構；Bot Token 必須視為機密 (Secret)。

### Discord
推薦使用 Application Commands / Slash Commands。
- **設計要點：** 適合團隊協作場景；Slash Command 可有效降低自然語言誤觸高風險操作的機率。

## 總體架構 (Overall Architecture)

建議新增通訊入口服務層，而非直接將 Webhook 接至 `server.py` 的互動迴圈中。

```text
LINE / WhatsApp / Telegram / Discord
        |
        v
Communication Webhook Server (接收請求)
        |
        v
Platform Adapter (平台適配)
  - 驗證簽章 / Token
  - 解析 Payload
  - 正規化使用者與訊息
        |
        v
Command Router (命令路由)
  - 權限檢查
  - 去重 / 速率限制 (Rate Limit)
  - 確認狀態機 (Confirmation State Machine)
  - 轉換為 AgentCommand
        |
        v
Agent Job Queue (任務隊列)
  - 狀態追蹤: pending / running / completed / failed
  - 支持恢復 (Recoverable)
  - 背景 Worker 執行
        |
        v
Agent Runtime (Agent 執行環境)
  - 記憶檢索 (Retrieval)
  - Laya Guard (安全攔截)
  - 工具調用 (Tool Calls)
  - 記憶記錄 (Memory Logging)
        |
        v
Outbound Adapter (輸出適配)
  - 分段回覆
  - 任務完成通知
  - 錯誤訊息回饋
  - 確認按鈕或確認碼
```

## 模組規劃 (Module Planning)

### 1. `communication_models.py`
定義平台無關的資料模型，如 `InboundMessage`（入站訊息）、`AgentCommand`（Agent 指令）以及 `OutboundMessage`（出站訊息）。

### 2. `communication_store.py`
使用 SQLite 記錄通訊事件與工作狀態。
- **關鍵資料表：** `comm_channels` (頻道設定)、`comm_identities` (身分對應)、`comm_messages` (訊息記錄)、`agent_command_jobs` (指令任務)、`confirmation_requests` (確認請求)、`comm_rate_limits` (速率限制)。

### 3. `communication_adapters/`
每個平台實作一個 Adapter 並遵循統一介面 (`CommunicationAdapter`)，包含 `verify_request`、`parse_events` 與 `send_message` 等方法。
**實作優先順序：** Telegram $\rightarrow$ LINE $\rightarrow$ WhatsApp $\rightarrow$ Discord。

### 4. `communication_server.py`
建立 HTTP Webhook Server (建議使用 FastAPI)。
- **流程：** 讀取 Raw Body $\rightarrow$ 驗證 $\rightarrow$ 解析為 `InboundMessage` $\rightarrow$ 檢查冪等性 (Idempotency) $\rightarrow$ 驗證權限 $\rightarrow$ 建立 `agent_command_jobs` $\rightarrow$ 快速回覆平台 $\rightarrow$ 由背景 Worker 執行。

### 5. `communication_worker.py`
背景 Worker 負責處理隊列中的指令，避免阻塞 Webhook 請求執行緒。
- **流程：** 提取 Job $\rightarrow$ 建立 Episode $\rightarrow$ 執行 Laya Guard $\rightarrow$ (若需確認) 發送確認請求並暫停 $\rightarrow$ 呼叫 Agent Runtime $\rightarrow$ 分段回覆結果 $\rightarrow$ 記錄記憶體事件。

## Agent Runtime 重構建議

將 `server.py` 中的 CLI 互動迴圈抽出為可重用的函數 `run_agent_turn()`，使 CLI 與通訊 Worker 共用同一套邏輯（包含 Laya 策略、記憶檢索與工具記錄），簡化測試且避免邏輯重複。

## 權限與安全策略 (Permissions & Security)

- **身分驗證：** 將平台 `sender_id` 映射至本地身分。定義 `viewer`、`operator`、`admin` 三種權限等級。
- **高風險動作確認：** 對於 `run_command`、檔案寫入/刪除、設定修改等操作，採取「摘要告知 $\rightarrow$ 發送確認碼 $\rightarrow$ 使用者回覆確認碼」的二階段驗證流程。
- **防重放與去重：** 使用 `{platform}:{platform_message_id}` 作為冪等金鑰 (Idempotency Key)。
- **速率限制：** 實作單個使用者每分鐘指令上限，以及單個對話僅允許一個運行中任務。

## Laya 邊界 (Laya Boundary)

Laya 僅在需要判斷「意圖是否觸發副作用」、「評估操作風險」或「決定是否需要確認」時介入。單純的文件翻譯、摘要、格式轉換或確定的允許清單判斷則不經過 Laya。

## 訊息格式與 UX (Message Format & UX)

- **指令格式：** 支援自然語言，同時提供 `/ask`、`/run`、`/read`、`/translate`、`/status`、`/cancel` 等保守命令格式。
- **回覆格式：** 針對短任務、長任務（排隊中）、需確認任務及失敗任務設計不同的視覺回饋 (使用 Emoji 區分)。
- **分段輸出：** Adapter 層需根據平台長度限制提供 `split_message` 函數。

## 測試計畫 (Testing Plan)

- **單元測試：** 驗證各平台簽章、Payload 解析、冪等性去重、權限控制及確認碼流程。
- **整合測試：** 使用假 Adapter 驗證從訊息進入到建立 Episode $\rightarrow$ 指令執行 $\rightarrow$ 分段回覆的完整路徑。
- **煙霧測試：** 使用 ngrok/Cloudflare Tunnel 進行真實平台端到端測試。

## 分階段實作 (Phased Implementation)

1. **Phase 1：** 抽出 `agent_runtime.py`，確保 CLI 行為不變。
2. **Phase 2：** 建立通訊核心模型與 SQLite Job Store。
3. **Phase 3：** 實作 Telegram Adapter (快速驗證)。
4. **Phase 4：** 實作 LINE Adapter。
5. **Phase 5：** 實作 WhatsApp Adapter。
6. **Phase 6：** 實作 Discord Adapter。
7. **Phase 7：** 部署治理、更新 README 並新增 `observability.py` 的監控功能。

## 風險與緩解 (Risks & Mitigation)

- **重複執行：** 透過冪等金鑰與資料庫唯一限制解決。
- **惡意請求：** 嚴格執行簽章與 Token 驗證。
- **誤觸危險操作：** 結合 Role Policy + Laya Guard + 二階段確認碼。
- **Webhook 超時：** 採取「立即入列 $\rightarrow$ 背景執行 $\rightarrow$ 主動推送」模式。
- **Secret 外洩：** 使用 `.env` 並在正式環境改用 Secret Manager。
