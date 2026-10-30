# Browser, documents and audio

`browser_session(url, actions, session_id, screenshot)` runs a native Chromium
worker with an owner/source/project/conversation keyed private profile and cookie
state. Supported actions: click, type, wait_for, assert_text, assert_url, download.
Assertions fail the turn rather than silently continuing. Downloads/screenshots
are private artifacts, never shared workspace files. Max 32 actions, 64 KiB
request, 60 seconds/turn, 5 seconds/action, 10 MiB/artifact. Profile/state admission
limits are 64 MiB/1 MiB, not a disk quota. Host Chromium is not an OS sandbox.
HTTP(S) only; credentials in URL, private-network destinations and WebSockets
are blocked. AGENT_BROWSER_ALLOWED_HOSTS explicitly permits trusted local hosts.
DNS checks are not an egress proxy; deploy a firewall for hostile network content.
Profiles persist cookies/storage, not live JS/DOM or unsubmitted form edits.
AGENT_BROWSER_ROOT defaults to LOCALAPPDATA/PythonAI/browser; OS permissions protect
that directory. Parent cancellation kills the worker/browser process tree.

Navigation waits for `domcontentloaded` (30-second limit), rather than waiting for
all network traffic to stop. Polling/analytics or a pending image must not prevent
reading an already loaded DOM. Dynamic content still requires explicit bounded
`wait_for`/assertion actions. HTTP 4xx/5xx and genuine navigation timeouts remain
failures, and actions do not run after those failures. See the
[Playwright navigation documentation](https://playwright.dev/python/docs/api/class-page#page-goto).

2026-10-30 regression: a real Chromium/local HTTP fixture keeps an image request
pending while returning readable currency text. The normal browser worker must
return that text without waiting for the image. The reported exchange-rates.org
ZAR/TWD URL also returned currency text and its source timestamp in 2.09 seconds
with an isolated temporary profile. This is a direct source read, not a production
LLM/Judge or desktop end-to-end replay.

PDF/DOCX/PPTX/XLSX attachments are parsed in a 45 second worker with source units:
PDF page, Word paragraph (no invented page numbers), slide, worksheet/row. Limits:
10 MiB input, 200 units/65536 characters, Office archive 2048 entries/32 MiB
expanded/8 MiB per entry. XML entities/encrypted files are rejected. Scanned PDF
uses installed Tesseract, 10 seconds/page, at most 8M render pixels; absent/failed
OCR is marked explicitly. Configure AGENT_TESSERACT_COMMAND/LANGUAGE (default
tesseract/eng; e.g. eng+chi_tra). Unknown binary files are reported unsupported,
never sent as truncated Base64. Parsing libraries run on the host in a bounded
process, not a hardened content-sanitization sandbox.

Configure AGENT_AUDIO_BASE_URL (HTTPS or loopback HTTP), AGENT_AUDIO_API_KEY,
AGENT_STT_MODEL, AGENT_TTS_MODEL, AGENT_TTS_VOICES (default alloy). Actual SDK audio
API calls use no automatic retry, 10 second network timeout and 30 second native
worker deadline. WebSocket advertises configured capabilities. audio.start has
integer stream_id and format wav/mp3/webm; frames use kind=2 + uint32 stream_id +
uint32 sequence (starting at zero) + encoded bytes. Max 256 frames, 64 KiB/frame,
2 MiB/stream, 60 second ingestion/WAV duration. audio.end returns stt.result;
tts.request has stream_id/text/voice, returns tts.result with playable WAV Base64.
TTS input 4096 characters, output 2 MiB/60 seconds. audio.cancel/reconnect/disconnect
cancel the native worker and suppress stale results; streams are not resumed or
merged after reconnect. STT does not automatically send a chat request. Voice
costs require provider billing monitoring; the chat token meter does not estimate
audio pricing. The protocol currently sends TTS as one bounded text message.

Tests: actual Chromium profile login/resume, isolation/assertions/download;
native Office parsing and actual scanned PDF/Tesseract; real SDK/local HTTP audio
wire contract and playable WAV, cancellation/stale-turn suppression. These do not
prove external sites, provider speech recognition quality or real device playback.
