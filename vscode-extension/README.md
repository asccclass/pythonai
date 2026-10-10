# PythonAI IDE proposal review

Run the existing `python ide_server.py` compatibility entry point (127.0.0.1:11435)
and the unified communication Dashboard service separately. Both use
`AGENT_API_TOKENS` and `ROLES_CONFIG`; IDE generation/completion and human mutations
require an admin API identity. The IDE server no longer accepts unauthenticated
requests or exposes wildcard CORS. Remote endpoints must use HTTPS; loopback HTTP
is supported for development.

Run **PythonAI: Set API Token** to store the credential in VS Code SecretStorage.
Configure `pythonai.ideUrl`, `pythonai.dashboardUrl`, `pythonai.project`, and
`pythonai.session`. Enter the same project/session in the Dashboard. Tokens are
never embedded in URLs or persisted in settings. The default Dashboard URL is
http://127.0.0.1:8000; configure the actual deployment port.

Select code and run **PythonAI: Generate Replacement Diff**. Generation buffers a
bounded UTF-8 NDJSON stream, requires the explicit completion marker, and opens
readonly original/proposed documents in VS Code's diff editor. Generation never
changes the original document. **Apply Reviewed Proposal** performs one editor edit
with undo stops; **Undo Proposal** restores that proposal with one edit if the
document has not changed. Native editor Undo is also supported. Apply is in-memory;
the user saves normally. **Discard Proposal** removes a proposal from memory.
Proposals are process-local and are discarded when the extension host exits.

Cancellation, incomplete/disconnected/error streams, oversized output and invalid
UTF-8 discard generated content. Concurrent generation in one document is rejected;
different documents are isolated. Document version checks prevent stale apply,
undo and completions; edits during generation discard the proposal. There are at
most 32 retained proposals and 1 MiB source/response text per proposal (2 MiB wire
response), a 100-second client deadline and no automatic retries. The server caps
requests at 128 KiB, selections at 64 KiB, prompts at 8 KiB, output at 1 MiB, and
uses T09 request/token/cost/concurrency budgets. Disconnect cleanup depends on the
next stream write/read timeout; cancellation does not undo provider work already
performed. Empty/unchanged replacements are rejected.

**Review Tasks in Dashboard** opens the configured URL without a token. Login
there separately. `/api/review/context`, `/api/review/tasks`, and
`/api/review/tasks/<encoded-id>` show only the authenticated API identity's exact
project/session tasks. The detail includes actual checkpoint plans (or assistant
plan text), tool steps/dependencies, frozen acceptance criteria, verification,
human questions, replies, bounded artifact diff/history/evidence and pending
approvals. Text is rendered as text, never executable HTML. Refresh is explicit;
scope changes clear old data and invalidate late review responses. Admin can cancel
their task and decide same-session pending approvals; viewers can only inspect.
Approval decisions do not execute/replay a tool, patch, publish or merge. Use the
original task's existing transport to retry/resume approved operations. Cross-channel
identity linking, background polling and arbitrary artifact filesystem downloads
are not provided. Completed generation remains `unverified`: it does not prove
that a proposal was applied, tested or delivered.

Tests: `npm ci`, `npm test`, `npm run test:host`. Host tests download VS Code to the
ignored `.vscode-test/` directory and run the actual extension in an isolated profile
against a local protocol fixture, with no external model. Server/API/Chromium tests
are in `agent_execution/tests/test_review_ui.py`; opt in with
`PYTHONAI_BROWSER_TEST=1`. Temporary screenshots/logs are saved under repo `temp/`.

References: [VS Code API](https://code.visualstudio.com/api/references/vscode-api),
[diff command](https://code.visualstudio.com/api/references/commands),
[Extension Host testing](https://code.visualstudio.com/api/working-with-extensions/testing-extension).

Verified 2026-10-10: real VS Code 1.141.0 Extension Host 14 cases; Node 9 tests; final Python UI/API integration 14 tests; full Docker/Chromium suite 576 passed, 1 external-provider skip, 30 subtests passed (884.64 seconds). One existing AnyIO deprecation warning. See tests/verification_baseline.json for scope, commands and local evidence paths.
