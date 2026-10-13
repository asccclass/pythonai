# Desktop companion extensions

The existing `communication_server.py` `/ws/v1` route remains the single desktop entry point. Restart that service after updating; no additional core server is needed.

`hello.payload.conversation_id` is optional (empty preserves legacy sessions). A nonempty ID must match `[A-Za-z0-9_-]{1,64}`. Runtime, task/approval scope, reliable outbox and input deduplication use the conversation scope. Each physical device has at most 50 in-memory sessions; inactive sessions may be evicted. Durable event replay does not guarantee restoration of model context after eviction/restart.

`welcome.features` advertises `conversation_sessions`, `attachments` limits and `desktop_tools`. `welcome.connection_id` binds desktop tool calls to this connection, so reliable replay of a previous connection's tool request must never execute an effect.

`chat.send.payload.attachments` is a list of `{filename, content_type, data_base64}`. Limits: 4 files, 1 MiB each, 2 MiB total, 4 MiB JSON envelope. Filenames must be basenames; image MIME and actual PNG/JPEG/WebP/GIF bytes/dimensions are validated. Bytes become `InboundAttachment` objects passed to the existing runtime/media parser, including suspended user answers. Transport slash commands reject attachments. The input deduplication fingerprint includes attachment names, types and byte hashes. Attachments are not copied to an extra persistent file store by this transport.

Clients may opt into `desktop.system_info`, `desktop.open_url`, `desktop.open_path`, `desktop.clipboard_write` capabilities. The model tool `desktop_tool(action, arguments)` and human `/desktop <action> <value>` command share a per-turn bridge. viewer is limited to system_info; operator cannot write the clipboard; admin can use all four. Other transports return an unavailable result and never execute local effects on the server.

Server sends reliable `tool.call` with `{action, arguments, timeout_ms:60000, connection_id}` and `ref` equal to the chat request. Client replies `tool.result` with the call ID in `ref` and `{ok:bool,data,error}`. Matching is per session and current WebSocket connection, with a 16 KiB result limit. The client validates its own URL/path allowlist and requires local human approval for effects. Timeout, chat cancellation, disconnection and replacement cancel the waiter; uncertain effects must not be retried automatically.

Tests: `tests/test_companion_features.py`, existing WS tests, and MyAI's controlled real ASGI/runtime integration suite. None require an external model provider.
