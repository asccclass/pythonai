# Communication and clients
- communication_server.py composes unified ASGI; web_api.py serves dashboard/REST, websocket_server.py serves /ws/v1. /health is HTTP liveness only, not authentication/provider/reliability proof.
- communication_adapters/ handles platform-specific transport; communication_store.py persists inbound/outbound/commands; communication_worker.py executes queued turns through shared agent_runtime.py.
- Telegram polling is its own transport entrypoint, not a separate Agent implementation. Polling/webhook receiving modes, per-bot leases and uncertain delivery need distinct handling; do not consume live updates for a diagnostic check.
- Remote identity/project/session scoping, runtime role checks and one-use approvals are invariants. Check actual ROLES_CONFIG/token identity for permission errors; a viewer cannot execute Skills simply because prompts request them.
- agent_execution/companion/ and media/ support client/protocol capability; MyAI desktop lives in a separate repository, not this checkout. vscode-extension/ and ide_server.py provide IDE integration; web/ is the dashboard.
