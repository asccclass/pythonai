# 通訊軟體指揮 Agent 規劃

## 目標

本計畫要讓使用者可以透過常見通訊軟體，例如 LINE、WhatsApp、Telegram、Discord，向本專案的 Agent 下達指令、接收結果、確認高風險操作，並保留現有 CLI Agent 的安全邊界、記憶系統與工具執行流程。

核心原則：

- 通訊軟體只是新的輸入/輸出通道，不應繞過現有 Agent、工具、Laya guard、命令確認與 episodic memory。
- 每個外部訊息都必須驗證來源、辨識使用者、套用權限，再轉成統一的內部 `AgentCommand`。
- 高風險操作必須走二階段確認，不因為來自 LINE 或 WhatsApp 就自動執行。
- 長任務必須非同步執行，通訊平台 webhook 應快速回應，避免平台重送或判定失敗。
- 通訊平台差異應封裝在 adapter 層，核心 Agent 不應依賴 LINE/WhatsApp 特定 payload。

## 平台事實與限制

### LINE

LINE Messaging API 會在使用者加好友或傳訊息時，對已設定的 webhook URL 發送 HTTPS POST。LINE 官方明確要求 bot server 在處理 webhook event 前驗證簽章，以確認請求沒有被竄改。LINE 也建議 webhook event 非同步處理，避免 server 長時間無法接收 webhook。參考：LINE Developers「Receive messages (webhook)」與 Messaging API reference。

設計含義：

- 必須實作 `X-Line-Signature` 驗證。
- webhook handler 應只做驗證、轉換、入列、快速回覆。
- 回覆使用 reply token；超過 reply token 時效或背景任務完成後，需要改用 push message。

### WhatsApp Business Platform

WhatsApp Business Platform 提供 Cloud API、webhooks、測試號碼與 sandbox。實作上需要 Meta App、WhatsApp Business Account、phone number ID、access token、webhook verify token 與 webhook 訂閱。

設計含義：

- webhook 需處理 Meta 的驗證 challenge 與後續 POST event。
- 發送訊息通常透過 Graph API message endpoint。
- WhatsApp 對商業訊息、模板、服務對話與費用有平台規則；需要把長任務通知、主動通知、模板訊息和一般回覆分開設計。
- 應避免依賴非官方 WhatsApp Web 自動化方案，正式功能以官方 Business Platform 為準。

### Telegram

Telegram Bot API 支援 bot 接收 updates，並透過 `sendMessage` 等方法傳送訊息。可用 webhook 或 long polling。Telegram 對個人開發和測試較友善，可作為第一個端到端通訊 adapter。

設計含義：

- 第一階段可以用 Telegram 或 LINE 先驗證 adapter 架構。
- Telegram 的 bot token 要視為 secret，不能寫入 repo。

### Discord

Discord 推薦用 Application Commands / Slash Commands。互動可以透過 outgoing webhook 進入服務，並要求在短時間內 acknowledgement，之後再用 follow-up webhook 回覆。

設計含義：

- Discord 適合團隊或開發者場景。
- Slash command 可以降低自然語言誤觸高風險操作的機率。

## 總體架構

建議新增一個通訊入口服務層，而不是直接把平台 webhook 接到 `server.py` 的互動迴圈。

```text
LINE / WhatsApp / Telegram / Discord
        |
        v
Communication Webhook Server
        |
        v
Platform Adapter
  - 驗證簽章 / token
  - 解析 payload
  - 正規化使用者與訊息
        |
        v
Command Router
  - 權限檢查
  - 去重 / rate limit
  - confirmation 狀態機
  - 轉成 AgentCommand
        |
        v
Agent Job Queue
  - pending / running / completed / failed
  - 可恢復
  - 背景 worker 執行
        |
        v
Agent Runtime
  - retrieval
  - Laya guard
  - tool calls
  - memory logging
        |
        v
Outbound Adapter
  - 分段回覆
  - 任務完成通知
  - 錯誤訊息
  - 確認按鈕或確認碼
```

## 模組規劃

### 1. `communication_models.py`

新增平台無關的資料模型：

```python
@dataclass
class InboundMessage:
    platform: str
    platform_message_id: str
    conversation_id: str
    sender_id: str
    text: str
    raw_payload: dict[str, Any]
    received_at: str

@dataclass
class AgentCommand:
    command_id: str
    platform: str
    conversation_id: str
    sender_id: str
    text: str
    requires_confirmation: bool = False
    status: str = "pending"

@dataclass
class OutboundMessage:
    platform: str
    conversation_id: str
    text: str
    reply_to_message_id: str | None = None
```

### 2. `communication_store.py`

用 SQLite 記錄通訊事件與工作狀態，延續目前 memory/job 的風格。

建議資料表：

| Table | 用途 |
|---|---|
| `comm_channels` | 平台設定摘要與啟用狀態，不存明文 secret。 |
| `comm_identities` | 平台 sender id 對應本地使用者/角色。 |
| `comm_messages` | inbound/outbound 訊息、原始 payload hash、處理狀態。 |
| `agent_command_jobs` | 待執行 Agent 指令、狀態、嘗試次數、錯誤、結果摘要。 |
| `confirmation_requests` | 高風險操作的確認碼、過期時間、狀態。 |
| `comm_rate_limits` | 每個 sender/conversation 的節流狀態。 |

重要欄位：

- `platform`
- `conversation_id`
- `sender_id`
- `platform_message_id`
- `idempotency_key`
- `status`
- `created_at`
- `updated_at`
- `expires_at`

### 3. `communication_adapters/`

每個平台一個 adapter，實作共同介面：

```python
class CommunicationAdapter(Protocol):
    platform: str

    def verify_request(self, headers: dict[str, str], body: bytes, query: dict[str, str]) -> bool:
        ...

    def parse_events(self, headers: dict[str, str], body: bytes, query: dict[str, str]) -> list[InboundMessage]:
        ...

    def send_message(self, message: OutboundMessage) -> None:
        ...
```

初始 adapter：

- `line_adapter.py`
- `whatsapp_adapter.py`
- `telegram_adapter.py`
- `discord_adapter.py`

優先順序建議：

1. Telegram：最快完成端到端測試。
2. LINE：台灣使用者常見，webhook/signature 模型清楚。
3. WhatsApp：正式商用價值高，但帳號、商業規則、模板與審核較多。
4. Discord：適合團隊與 slash command。

### 4. `communication_server.py`

新增 HTTP webhook server。可以先用標準庫或現有輕量框架；若願意新增依賴，建議使用 FastAPI。

建議 endpoint：

| Endpoint | 用途 |
|---|---|
| `GET /health` | 健康檢查。 |
| `POST /webhooks/line` | LINE webhook。 |
| `GET /webhooks/whatsapp` | Meta webhook verification challenge。 |
| `POST /webhooks/whatsapp` | WhatsApp webhook event。 |
| `POST /webhooks/telegram` | Telegram webhook。 |
| `POST /webhooks/discord` | Discord interactions webhook。 |

Webhook handler 流程：

1. 讀取 raw body。
2. 驗證 signature/token。
3. 解析為 `InboundMessage`。
4. 檢查 idempotency，避免平台重送造成重複執行。
5. 套用 sender allowlist / role policy。
6. 建立 `agent_command_jobs`。
7. 立即回覆平台成功或簡短 acknowledgement。
8. 背景 worker 執行 job，完成後主動送出結果。

### 5. `communication_worker.py`

背景 worker 負責執行 queued command。不要在 webhook request thread 直接跑 Agent。

流程：

1. 取出 pending command job。
2. 建立 episode，寫入 `comm_inbound` event。
3. 套用權限與安全策略。
4. 對需要 Laya 的任務執行 Laya guard；文件翻譯/摘要/內容轉換可沿用目前 skip policy。
5. 如需確認，送出確認訊息並停止 job，等待確認回覆。
6. 呼叫 Agent runtime。
7. 依平台訊息長度限制分段回覆。
8. 寫入 `comm_outbound`、`agent_command_result`、memory events。

## Agent Runtime 重構建議

目前 `server.py` 主要是 CLI loop，建議抽出可重用函式：

```python
def run_agent_turn(
    user_input: str,
    *,
    source: str = "cli",
    sender_id: str | None = None,
    conversation_id: str | None = None,
    memory: MemoryStore | None = None,
) -> AgentTurnResult:
    ...
```

`server.py` 的 CLI loop 和 `communication_worker.py` 都呼叫同一個 `run_agent_turn()`。

好處：

- LINE/WhatsApp 不需要複製 CLI 流程。
- Laya skip policy、memory retrieval、tool logging、procedure tracking 共用。
- 測試可以直接測 `run_agent_turn()`，不用模擬 stdin/stdout。

## 權限與安全策略

### 身分驗證

每個平台的 `sender_id` 必須映射到本地 identity。

建議 `.env` 初始設定：

```env
COMM_ALLOWED_SENDERS=line:Uxxxx,telegram:123456789,whatsapp:+8869xxxxxxxx
COMM_ADMIN_SENDERS=line:Uxxxx
```

正式版改存 SQLite：

| Role | 能力 |
|---|---|
| `viewer` | 問問題、讀取非敏感狀態。 |
| `operator` | 允許讀檔、跑低風險 workflow。 |
| `admin` | 可確認高風險命令、管理通訊設定。 |

### 高風險動作確認

對以下操作要求二階段確認：

- `run_command`
- 寫入或覆蓋檔案
- 刪除檔案
- 修改設定
- 匯入大量 memory
- 發送外部訊息給第三方

確認方式：

1. Agent 回覆摘要：「將執行 X，影響 Y」。
2. 系統產生 6 位確認碼，例如 `CONFIRM 482913`。
3. 使用者必須在同一平台、同一 conversation 回覆確認碼。
4. 確認碼過期時間建議 5 分鐘。

LINE/Discord 可用 quick reply/button；WhatsApp 可用互動訊息或純文字確認；Telegram 可用 inline keyboard 或文字確認。

### 防重放與去重

每個 inbound message 產生 idempotency key：

```text
{platform}:{platform_message_id}
```

若平台沒有穩定 message id，使用 raw body hash + sender + timestamp window。

### Rate limit

至少實作：

- 每個 sender 每分鐘最大指令數。
- 每個 conversation 同時最多一個 running job，或明確支援 queue。
- 長任務輸出分段與總字數上限。

## Laya 邊界

通訊入口不應把平台 raw payload、整段附件、整份文件內容直接送給 Laya。

需要 Laya 的情況：

- 不確定使用者意圖是否會觸發本地副作用。
- 需要估計本地操作風險。
- 需要決定是否要求確認。

不需要 Laya 的情況：

- 單純文件翻譯、摘要、格式轉換。
- Agent 已完成的自然語言回覆。
- 平台 webhook 驗證。
- 固定格式 confirmation code 檢查。
- deterministic allowlist/denylist 可以直接判斷的情況。

對 Laya 的 state 應是小型摘要，例如：

```json
{
  "message": "User asks to run a local shell command",
  "requested_tool": "run_command",
  "paths": ["memoryx.md"],
  "will_modify_files": true
}
```

## 訊息格式與 UX

### 指令格式

支援自然語言，但建議提供保守命令格式：

```text
/ask 這個 repo 的 memory 架構是什麼？
/run python -m unittest tests.test_memory
/read papers/memory_plan.md
/translate papers/memory_plan.md zh-TW -> memoryx.md
/status
/cancel <job_id>
```

### 回覆格式

短任務：

```text
✅ 已完成
摘要：...
```

長任務：

```text
🕒 已排入佇列
Job: comm_20261001_000123
狀態：running
```

需要確認：

```text
⚠️ 需要確認
操作：執行 PowerShell command
原因：可能修改本機檔案
若要繼續，請回覆：CONFIRM 482913
```

失敗：

```text
❌ 執行失敗
原因：...
Job: ...
```

### 分段輸出

不同平台訊息長度限制不同，adapter 應提供：

```python
def split_message(text: str) -> list[str]:
    ...
```

不要在核心 Agent 裡硬寫 LINE 或 WhatsApp 的長度限制。

## 設定項目

`.env.example` 建議新增：

```env
COMM_SERVER_HOST=127.0.0.1
COMM_SERVER_PORT=8080
COMM_PUBLIC_BASE_URL=https://example.ngrok-free.app

COMM_ALLOWED_SENDERS=
COMM_ADMIN_SENDERS=

LINE_CHANNEL_SECRET=
LINE_CHANNEL_ACCESS_TOKEN=

WHATSAPP_VERIFY_TOKEN=
WHATSAPP_ACCESS_TOKEN=
WHATSAPP_PHONE_NUMBER_ID=

TELEGRAM_BOT_TOKEN=

DISCORD_APPLICATION_ID=
DISCORD_PUBLIC_KEY=
DISCORD_BOT_TOKEN=
```

secret 不應寫入 SQLite 明文；初期放 `.env`，正式部署改用 OS secret store 或雲端 secret manager。

## 測試計畫

### 單元測試

- LINE signature 驗證正確與錯誤。
- WhatsApp webhook verification challenge。
- Telegram payload parse。
- Discord signature/timestamp 驗證。
- idempotency 去重。
- sender allowlist。
- confirmation code 流程。
- 長訊息分段。
- webhook handler 在背景 job 建立後立即回應。

### 整合測試

- 假 adapter 送入文字，確認建立 episode 與 command job。
- 模擬高風險 `run_command`，確認停在 confirmation。
- 模擬確認碼，確認 job 繼續執行。
- 模擬 Agent 完成後 outbound adapter 收到分段訊息。
- 模擬平台重送同一 webhook，確認不重複執行。

### 手動 smoke test

1. 啟動 communication server。
2. 用 ngrok 或 Cloudflare Tunnel 暴露 webhook URL。
3. LINE/Telegram 送 `/ask hello`。
4. 確認收到 queued/running/completed 回覆。
5. 送高風險命令，確認需要 confirmation。
6. 送文件轉換任務，確認不觸發 Laya guard。

## 分階段實作

### Phase 1：抽出 Agent Turn Runtime

交付：

- 新增 `agent_runtime.py`。
- 從 `server.py` 抽出 `run_agent_turn()`。
- CLI 行為不變。
- 測試覆蓋既有 main loop 的核心流程。

完成標準：

- CLI 仍可運作。
- memory、retrieval、tool logging、Laya skip policy 共用同一條 runtime。

### Phase 2：通訊核心模型與假 adapter

交付：

- `communication_models.py`
- `communication_store.py`
- `communication_worker.py`
- fake/in-memory adapter for tests

完成標準：

- 測試可不用真平台，直接送入 `InboundMessage` 並取得 `OutboundMessage`。

### Phase 3：Telegram Adapter

交付：

- `telegram_adapter.py`
- webhook endpoint
- `sendMessage`

完成標準：

- 可從 Telegram 私訊 Agent。
- 支援 `/ask`、`/status`、`/cancel`。

### Phase 4：LINE Adapter

交付：

- `line_adapter.py`
- signature verification
- reply/push message
- rich confirmation text 或 quick reply

完成標準：

- 可從 LINE Official Account 私訊 Agent。
- webhook 重送不會造成重複執行。

### Phase 5：WhatsApp Adapter

交付：

- `whatsapp_adapter.py`
- Meta webhook verification
- inbound text parse
- outbound Graph API messages

完成標準：

- 可用 Meta test number 收發訊息。
- 區分一般回覆、主動通知、模板需求。

### Phase 6：Discord Adapter

交付：

- `discord_adapter.py`
- slash command registration helper
- interaction response/follow-up

完成標準：

- 可用 Discord slash command 指揮 Agent。
- 支援 ephemeral confirmation。

### Phase 7：部署與治理

交付：

- `.env.example` 更新。
- README 通訊功能章節。
- `observability.py` 新增 comm jobs / comm messages 檢視。
- migration/backfill 文件。

完成標準：

- 可以查看 pending/running/failed command jobs。
- 可以查詢某平台 sender 的歷史指令。
- 可以停用某平台或某 sender。

## 風險與緩解

| 風險 | 緩解 |
|---|---|
| 平台 webhook 重送造成重複執行 | idempotency key + unique constraint。 |
| 外部惡意請求偽造 webhook | signature/token verification。 |
| 手機訊息誤觸危險操作 | role policy + Laya guard + confirmation code。 |
| Agent 長任務造成 webhook timeout | webhook 只入列，worker 背景執行。 |
| 訊息過長無法送出 | adapter 分段、摘要、附檔策略。 |
| secret 外洩 | `.env` ignored、secret manager、不要記錄 token。 |
| WhatsApp 商業規則與費用變動 | adapter 層隔離，文件註明需按 Meta 最新規則設定。 |
| 多平台使用者身份混淆 | `comm_identities` 顯式綁定 sender id。 |

## 建議先做的最小可行版本

最小版本建議不要一開始就同時做 LINE 和 WhatsApp。先做：

1. `agent_runtime.py` 抽出共用 turn runner。
2. `communication_models.py` 與 SQLite job store。
3. fake adapter 測試完整流程。
4. Telegram adapter 端到端。
5. LINE adapter。
6. WhatsApp adapter。

這樣可以先把安全模型、queue、confirmation、memory integration 做穩，再處理各平台帳號與商業設定差異。

## 參考資料

- LINE Developers: Receive messages (webhook): https://developers.line.biz/en/docs/messaging-api/receiving-messages/
- LINE Developers: Messaging API reference: https://developers.line.biz/en/reference/messaging-api/
- WhatsApp Business Platform Developer Hub: https://whatsappbusiness.com/developers/developer-hub/
- Telegram Bot API: https://core.telegram.org/bots/api
- Discord Application Commands: https://docs.discord.com/developers/docs/interactions/application-commands
