# Authenticated IDE service

Implementation for the existing `ide_server.py` entry point. Admin API tokens,
project workspace confinement, bounded JSON input, complete UTF-8 NDJSON proposals,
T08 persistent generation checkpoints and T09 model budgets are enforced here.
No file is read for context or modified: the editor's selected code is authoritative.
`/api/autocomplete` returns a bounded JSON completion; `/api/inline_chat` emits
start/delta/done or error. A failed/disconnected stream never emits a successful
completion marker. Failed provider diagnostics are sanitized.

The Dashboard and IDE use the same owner scope (`api`, identity, project,
`project:session`). A successful generation is a completed **unverified** task.
Provider cancellation is bounded by SDK read timeout, shared budget monitoring and
the next stream iteration/write. See `vscode-extension/README.md` for user flows,
configuration, limits and test commands. Tests/resources are in the existing
`agent_execution/tests/` and `vscode-extension/tests/` directories.
