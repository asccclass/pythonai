# Project source map
- Three entrypoints share the Agent runtime: server.py (CLI), telegram_polling_worker.py (Telegram getUpdates), communication_server.py (unified FastAPI webhook/dashboard/REST/WebSocket). Do not create another core for a new channel.
- agent_runtime.py coordinates AgentRuntime/run_agent_turn/resume_agent_turn; server.py supplies model/tool loop; base.py registers builtins; tools/ contains tool implementations and schemas.
- Execution, permission, checkpoint, budget and acceptance boundaries: `mem:execution/core`.
- Communication reliability, remote identity and UI integration: `mem:communication/core`.
- Episodic/semantic/procedural storage and background processing: `mem:memory/core`.
- agent_configuration/data owns editable model-facing text; loader semantics and code practices: `mem:conventions`.
- Runtime/dependency boundaries: `mem:tech_stack`; local launch and diagnostics: `mem:suggested_commands`; delivery checks: `mem:task_completion`.
- AGENTS.md is authoritative: scoped directories for new programs, related regression tests for code edits, project venv for all Python validation, one corresponding commit per completed change. Preserve unrelated dirty paths.
- README feature tables and papers/todo.md include dated evidence; implementation presence is not proof of external provider/platform success.
