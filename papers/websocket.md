# 桌面 AI 伴侶 — WebSocket 協定 v1 與 Python 端點骨架

## 一、傳輸層

| 項目 | 規格 |
|---|---|
| 端點 | `wss://你的網域/ws/v1` |
| 文字幀 | UTF-8 JSON（所有控制訊息） |
| 二進位幀 | 音訊串流（避免 base64 多 33% 流量） |
| 認證 | 連線後**第一則訊息必須是 `hello`**（5 秒內），不要把 token 放 URL（會進 log） |
| 心跳 | 客戶端每 20 秒送 `ping`，60 秒無任何訊息則伺服器斷線 |
| 重連 | 指數退避 1→2→4→…→30 秒，重連時帶 `last_seq` 補收漏訊息 |

## 二、訊息信封

```json
{
  "v": 1,
  "type": "chat.delta",
  "id": "01J...",
  "ref": "01J...",
  "seq": 42,
  "ts": 1760000000123,
  "payload": { }
}
```

| 欄位 | 說明 |
|---|---|
| `v` | 協定版本，不相容變更才升級 |
| `type` | `領域.動作` 格式 |
| `id` | 訊息唯一 ID（ULID/UUID，由發送方產生） |
| `ref` | 回應哪一則訊息的 id（串流、工具結果、錯誤都靠它關聯） |
| `seq` | **僅伺服器→客戶端**，每個裝置遞增，用於斷線補發 |
| `ts` | 發送時間（毫秒） |
| `payload` | 內容，依 type 而定 |

## 三、連線生命週期

```
Client                          Server
  │── hello ───────────────────►│  驗證 token、登記裝置
  │◄────────────────── welcome ─│  session_id、能力、補發起點
  │◄── (補發 seq > last_seq) ───│
  │── ack {seq} ───────────────►│  定期確認，伺服器清理緩衝
  │── ping ────────────────────►│
  │◄────────────────────── pong ─│
```

### `hello`（C→S）

```json
{"type":"hello","payload":{
  "token":"...",
  "device_id":"desktop-andy-01",
  "device_type":"desktop",
  "app_version":"0.1.0",
  "last_seq":41,
  "capabilities":["tool.open_url","tool.system_info","tool.now_playing","vision.presence","audio.play","audio.record"]
}}
```

`device_type`：`desktop` | `mobile` | `web`

### `welcome`（S→C）

```json
{"type":"welcome","payload":{
  "session_id":"...",
  "server_time":1760000000000,
  "resumed":true,
  "features":{"stt":true,"tts":true,"tts_voices":["zh-TW-HsiaoChenNeural"]}
}}
```

## 四、客戶端 → 伺服器

| type | payload | 說明 |
|---|---|---|
| `hello` | 見上 | 第一則訊息 |
| `ping` | `{}` | 心跳 |
| `ack` | `{seq}` | 已處理到 seq（建議每 5 則或 5 秒） |
| `chat.send` | `{text, attachments?}` | 使用者文字訊息 |
| `chat.interrupt` | `{ref?}` | 使用者打斷（停止生成與 TTS） |
| `audio.start` | `{stream_id, codec, sample_rate, mode}` | 開始錄音。`mode`: `push_to_talk` / `vad` |
| `audio.end` | `{stream_id}` | 錄音結束，伺服器開始 STT |
| `event` | `{name, data}` | 客戶端事件，見下表 |
| `state.report` | `{cpu, mem, now_playing, active_app?, idle_seconds}` | 系統狀態，建議 30–60 秒一次或變化時 |
| `tool.result` | `{ok, data?, error?}`，`ref` = tool.call 的 id | 工具執行結果 |
| `settings.update` | `{voice?, proactive?, quiet_hours?}` | 偏好設定 |

### `event.name` 清單

| name | data | 觸發時機 |
|---|---|---|
| `user.idle` | `{seconds}` | 閒置達門檻（如 10 分鐘） |
| `user.active` | `{idle_was}` | 閒置後回來 |
| `user.arrived` | `{}` | 攝像頭偵測從無人到有人（本機冷卻 30 分鐘） |
| `user.left` | `{}` | 有人到無人 |
| `app.focus` | `{focused: bool}` | 視窗聚焦變化 |
| `character.poke` | `{area}` | 使用者點擊角色 |
| `playback.finished` | `{ref}` | TTS 播放完畢（用於同步狀態） |

## 五、伺服器 → 客戶端

| type | payload | 說明 |
|---|---|---|
| `welcome` / `pong` | 見上 | |
| `chat.start` | `{role:"assistant"}`，`ref`=使用者訊息 id | 開始回覆，前端顯示「思考中→輸入中」 |
| `chat.delta` | `{text}` | 串流增量文字 |
| `chat.end` | `{text, emotion, action?}` | 完整文字與最終情緒 |
| `avatar.set` | `{emotion, intensity, action?, duration_ms?}` | 角色狀態（可獨立於聊天發送，例如主動提醒） |
| `tts.start` | `{stream_id, codec, sample_rate, voice}` | 接著是二進位音訊幀 |
| `tts.end` | `{stream_id}` | |
| `stt.result` | `{stream_id, text, final}` | 語音辨識結果（可先送 partial） |
| `proactive` | `{reason, text, emotion, tts?, priority}` | 伺服器主動推送 |
| `tool.call` | `{name, args, risk, confirm, timeout_ms}` | 要求客戶端執行 |
| `notify` | `{title, body, at?}` | 系統通知／提醒 |
| `error` | `{code, message, retryable}` | 錯誤，`ref` 指向觸發者 |
| `bye` | `{reason}` | 伺服器即將關閉連線 |

### 情緒與動作列舉

```
emotion: neutral | happy | sad | angry | surprised | shy | thinking | sleepy | worried | excited
action:  wave | nod | shake_head | stretch | yawn | look_around | none
intensity: 0.0 ~ 1.0
```

前端對**未知值一律降級為 `neutral`／`none`**，這樣後端新增情緒不會讓舊版客戶端壞掉。

### `proactive.reason` 列舉

`hydration` | `greeting` | `schedule` | `idle_checkin` | `welcome_back` | `custom`

`priority`：`low`（可丟棄）| `normal` | `high`（必須送達，走備援通道）

## 六、工具呼叫（系統控制）

**原則：後端只下指令，客戶端用白名單執行，且客戶端有最終否決權。**

```json
{"type":"tool.call","id":"t1","payload":{
  "name":"open_url","args":{"url":"https://weather.com"},
  "risk":"low","confirm":false,"timeout_ms":5000}}
```

```json
{"type":"tool.result","ref":"t1","payload":{"ok":true}}
```

| 工具 | args | risk | 備註 |
|---|---|---|---|
| `open_url` | `{url}` | low | 只允許 http/https |
| `system_info` | `{fields:[]}` | low | cpu / mem / battery / time |
| `now_playing` | `{}` | low | 各平台實作不同，可能回 `unsupported` |
| `set_reminder` | `{at, text}` | low | 客戶端本地排程，離線也會響 |
| `open_app` | `{name}` | medium | 僅限使用者設定的白名單 App |
| `clipboard_write` | `{text}` | medium | 建議彈窗確認 |

規則：

- `risk != low` 或 `confirm: true` → 客戶端彈窗確認，拒絕則回 `{ok:false, error:"user_denied"}`
- 客戶端收到**不在白名單**的工具 → 回 `{ok:false, error:"unsupported_tool"}`，**絕不執行**
- 逾時未回覆，伺服器視為失敗
- 只會發給宣告了對應 `tool.*` capability 的裝置（手機收不到 `open_app`）

## 七、音訊二進位幀格式

```
┌────────┬──────────────┬──────────────┬─────────────┐
│ 1 byte │   4 bytes    │   4 bytes    │   N bytes   │
│  kind  │  stream_id   │     seq      │   payload   │
└────────┴──────────────┴──────────────┴─────────────┘
 (數值皆為 big-endian uint32)
 kind: 0x01 = TTS 音訊（S→C）  0x02 = 麥克風音訊（C→S）
```

- 建議編碼：**麥克風上行 Opus（WebM/Ogg）或 16 kHz PCM16**；TTS 下行 Edge-TTS 預設輸出 MP3，先直接轉送即可
- `stream_id` 由發起方遞增產生，對應 `audio.start` / `tts.start`
- 客戶端收到 `chat.interrupt` 後，伺服器須停止送出該 `stream_id` 的後續幀；客戶端也要丟棄殘餘幀

## 八、多裝置路由規則

同一使用者可同時連線桌面與手機，伺服器依下列順序決定 `proactive` 送給誰：

1. 最近有 `user.active`／互動、且 `app.focus` 為 true 的裝置
2. 桌面且未閒置
3. 手機（前景連線中）
4. 都沒有 → **走 Telegram**（`priority: high` 時一定要送）

`chat.*` 回覆只回給發問的那個裝置；`tool.call` 只發給有對應能力的桌面裝置。

## 九、可靠性與錯誤碼

- 伺服器對每個裝置保留最近 200 則 `seq` 訊息；重連時補發 `seq > last_seq`
- 若 `last_seq` 已超出緩衝，`welcome.resumed = false`，客戶端清除未完成的串流狀態
- `chat.delta`、音訊幀**不進補發緩衝**（過期無意義），只有 `chat.end`、`proactive`、`notify`、`tool.call` 會

| code | 意義 | retryable |
|---|---|---|
| `auth_failed` | token 無效 | ✗（關閉連線，code 4401） |
| `bad_message` | JSON／欄位錯誤 | ✗ |
| `unsupported_version` | `v` 不支援 | ✗ |
| `rate_limited` | 太頻繁 | ✓ |
| `llm_error` | 模型生成失敗 | ✓ |
| `stt_failed` / `tts_failed` | 語音處理失敗 | ✓ |
| `tool_unsupported` / `tool_denied` / `tool_timeout` | 工具相關 | ✗ |
| `busy` | 上一輪還沒結束 | ✓ |

## 十、Python 端點骨架（FastAPI）

```bash
pip install "fastapi" "uvicorn[standard]"
uvicorn server:app --host 0.0.0.0 --port 8765
```

```python
# server.py
import asyncio, json, time, uuid
from collections import deque
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

app = FastAPI()
TOKENS = {"change-me": "andy"}          # 之後改為環境變數／資料庫
RELIABLE = {"chat.end", "proactive", "notify", "tool.call"}


def new_id() -> str:
    return uuid.uuid4().hex


class Session:
    """以 device_id 為鍵，斷線後保留，用來補發訊息。"""
    def __init__(self, device_id, device_type):
        self.device_id = device_id
        self.device_type = device_type
        self.caps: set[str] = set()
        self.ws: WebSocket | None = None
        self.seq = 0
        self.outbox: deque = deque(maxlen=200)
        self.last_active = time.time()
        self.focused = False

    async def send(self, type_: str, payload: dict, ref: str | None = None):
        msg = {"v": 1, "type": type_, "id": new_id(), "ref": ref,
               "ts": int(time.time() * 1000), "payload": payload}
        if type_ in RELIABLE:
            self.seq += 1
            msg["seq"] = self.seq
            self.outbox.append(msg)
        if self.ws:
            try:
                await self.ws.send_text(json.dumps(msg, ensure_ascii=False))
            except Exception:
                self.ws = None


class Hub:
    def __init__(self):
        self.sessions: dict[str, Session] = {}

    def get(self, device_id, device_type) -> Session:
        s = self.sessions.get(device_id)
        if not s:
            s = self.sessions[device_id] = Session(device_id, device_type)
        return s

    def pick_for_proactive(self) -> Session | None:
        online = [s for s in self.sessions.values() if s.ws]
        if not online:
            return None
        online.sort(key=lambda s: (s.focused, s.device_type == "desktop",
                                   s.last_active), reverse=True)
        return online[0]

    async def push_proactive(self, reason, text, emotion="happy", priority="normal"):
        s = self.pick_for_proactive()
        payload = {"reason": reason, "text": text, "emotion": emotion,
                   "tts": True, "priority": priority}
        if s:
            await s.send("proactive", payload)
        elif priority == "high":
            await send_via_telegram(text)      # 你現有的 Telegram 發送函式


hub = Hub()


async def send_via_telegram(text: str):
    ...  # 接你現有的程式


# ---- 接你現有的 LLM：回傳 async generator，最後給 (完整文字, 情緒) ----
async def brain_reply_stream(user_text: str):
    for ch in "（示範）你說：" + user_text:
        await asyncio.sleep(0.02)
        yield ("delta", ch)
    yield ("end", {"emotion": "happy", "action": "nod"})


@app.websocket("/ws/v1")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    session: Session | None = None
    try:
        # 1) 必須先 hello
        raw = await asyncio.wait_for(ws.receive_text(), timeout=5)
        hello = json.loads(raw)
        p = hello.get("payload", {})
        if hello.get("type") != "hello" or p.get("token") not in TOKENS:
            await ws.close(code=4401)
            return

        session = hub.get(p["device_id"], p.get("device_type", "web"))
        session.ws = ws
        session.caps = set(p.get("capabilities", []))
        last_seq = p.get("last_seq", 0)
        resumable = (not session.outbox) or session.outbox[0]["seq"] <= last_seq + 1

        await session.send("welcome", {
            "session_id": new_id(), "server_time": int(time.time() * 1000),
            "resumed": resumable,
            "features": {"stt": True, "tts": True}})
        if resumable:
            for m in list(session.outbox):
                if m["seq"] > last_seq:
                    await ws.send_text(json.dumps(m, ensure_ascii=False))

        # 2) 主迴圈（60 秒沒訊息視為死線）
        while True:
            msg = await asyncio.wait_for(ws.receive(), timeout=60)
            if msg.get("type") == "websocket.disconnect":
                break
            session.last_active = time.time()

            if msg.get("bytes") is not None:
                handle_audio_frame(session, msg["bytes"])
                continue

            data = json.loads(msg["text"])
            t, pl, mid = data.get("type"), data.get("payload", {}), data.get("id")

            if t == "ping":
                await session.send("pong", {}, ref=mid)
            elif t == "ack":
                while session.outbox and session.outbox[0]["seq"] <= pl.get("seq", 0):
                    session.outbox.popleft()
            elif t == "chat.send":
                asyncio.create_task(handle_chat(session, pl["text"], mid))
            elif t == "event":
                await handle_event(session, pl)
            elif t == "tool.result":
                handle_tool_result(data.get("ref"), pl)
            else:
                await session.send("error", {"code": "bad_message",
                    "message": f"unknown type {t}", "retryable": False}, ref=mid)

    except (WebSocketDisconnect, asyncio.TimeoutError):
        pass
    finally:
        if session:
            session.ws = None


async def handle_chat(s: Session, text: str, ref: str):
    await s.send("chat.start", {"role": "assistant"}, ref=ref)
    full = []
    async for kind, val in brain_reply_stream(text):
        if kind == "delta":
            full.append(val)
            await s.send("chat.delta", {"text": val}, ref=ref)
        else:
            await s.send("chat.end", {"text": "".join(full), **val}, ref=ref)


async def handle_event(s: Session, ev: dict):
    name = ev.get("name")
    if name == "app.focus":
        s.focused = ev["data"]["focused"]
    elif name == "user.arrived":
        await hub.push_proactive("greeting", "歡迎回來！", "happy")
    # user.idle 等：交給你的決策邏輯決定要不要打招呼


def handle_audio_frame(s: Session, b: bytes): ...      # kind=0x02 → 送進 STT
def handle_tool_result(ref, payload): ...              # 喚醒等待該 ref 的 Future


# ---- 排程示範：每小時整點主動提醒（用 APScheduler 取代更好）----
@app.on_event("startup")
async def start_scheduler():
    async def loop():
        while True:
            await asyncio.sleep(3600)
            await hub.push_proactive("hydration", "主人，記得喝水喔！", "happy")
    asyncio.create_task(loop())
```

骨架已涵蓋：hello 認證、斷線補發、ack 清理、多裝置路由、串流回覆、主動推送。**尚未實作**的部分（STT、TTS、工具呼叫的 Future 等待）在協定裡已預留位置，接上時不需要改協定。

## 十一、建議的決定

1. **上線一定要 TLS（wss）**，並在反向代理（Caddy/Nginx）設好 WebSocket 升級與較長的 read timeout。
2. token 先用長隨機字串即可，之後再換成每裝置獨立簽發、可撤銷的 token。
3. 把這份協定轉成 **JSON Schema 或 TypeScript 型別**，前後端共用，之後改協定時型別檢查會幫你抓漏。
4. 保留 Telegram 作為手機背景／離線時的推播備援通道。
