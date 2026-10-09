# Shared execution policy

T01 common policy for model tools, Skill steps, registered Python tools, MCP routing,
subagents and scheduled commands. The context carries source, identity and audit
store through ContextVars. Worker threads must copy the caller context.

Roles are checked on each nested tool call. Skills may grant their own resolved
asset directory only to run_command, never a root supplied in Skill inputs.
Path arguments are resolved against AGENT_WORKSPACE_ROOT (including symlinks).

This is application policy, not an OS sandbox: arbitrary Python, shell programs
and third-party MCP servers still require T02 isolation. Tests live in
agent_execution/tests and are collected by the main test suite.

## Bounded subprocesses and Docker sandbox (T02)

process.py applies a default 60 second timeout, 2 MiB output limit per stream,
cancellation and descendant cleanup (Windows Job Object or POSIX process group).
Exit codes: 124 timeout, 130 cancellation, 125 output overflow.

sandbox.py uses a read-only filtered snapshot and offline resource-limited Docker
container, with bounded tmpfs scratch/artifacts and validated tar export. Docker
unavailability never falls back to host execution. Built-in trusted host helpers
use the bounded runner; third-party MCP/generated-code lifecycle remains T18.

Opt-in live tests: PYTHONAI_DOCKER_TEST=1 python -m pytest agent_execution/tests.

## Human approvals and authenticated APIs (T03)

Remote identities default to viewer; explicit ROLES_CONFIG grants are required.
api.py applies Bearer authentication and role checks to both HTTP transports.
approvals.py stores exact-action decisions outside the workspace by default,
with identity/session binding, expiry, atomic one-use consumption and audit events.
Only human CLI/communication commands or authenticated dashboard actions grant
decisions. Model confirm=True is never a grant. Host execution and installed
Python/MCP code remain trusted; application approvals cannot constrain arbitrary
host code which can access the user's private storage.

Opt-in actual Chromium tests: PYTHONAI_BROWSER_TEST=1 python -m pytest
agent_execution/tests/test_auth_transport.py.

## Session and memory isolation (T05)

sessions.py creates independent runtime/messages/search stats/locks per identity,
conversation and project. The original runtime is a template only. Physical SQLite
databases isolate remote memory; the legacy local database is not copied to remote
users. Trusted project-to-workspace mappings are carried by ContextVar rather than
mutating process environment. Cancellation, status and suspended job queries are
owner-scoped, and atomic claims prevent duplicate starts. API approvals also bind
project. WebSocket devices/outboxes/proactive routing are owner-scoped.
