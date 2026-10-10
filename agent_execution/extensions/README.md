# Trusted extensions and MCP

Generated Python is staged, never imported by create_dynamic_tool. Each version
stores immutable files, claimed provenance, digest and Docker unittest evidence.
Python and skill.json/SKILL.md packages use the same owner-scoped lifecycle.

Human CLI: `python -m agent_execution.extensions stage package.json`, `show ID`,
`test ID`, `activate ID --approval-id APPROVAL`, `disable NAME`, `list`.
Package JSON keys: name, kind (tool/skill), files, origin. Tool files: tool.py,
schema.json, test_extension.py; skill files: skill.json, SKILL.md, test_extension.py.
Testing uses the existing offline Docker sandbox, a 60 second deadline and 64 KiB
evidence. Failed/zero/missing tests cannot enable a version. Review source and
test quality, request activation, approve through the existing human channel,
then activate the exact digest. Rollback activates an earlier tested version with
a fresh approval. Restart to refresh advertised tool schemas; existing wrappers
reject disabled/replaced versions. Activated Python is trusted host code, not an
OS sandbox; tests and hashes alone cannot prove third-party code is safe.

Only the committed builtins.json deployment inventory is loaded automatically
from tools/ and default skills/. Custom SkillRegistry roots remain trusted explicit
configuration. DB defaults to LOCALAPPDATA/PythonAI/extensions.db; configure
AGENT_EXTENSION_DB for isolation. Max 1000 versions, 1 MiB/package, 64 MiB DB.

MCP has exact collision-resistant routes, bounded offline JSON Schema validation,
structured output/error validation, stdio UTF-8/EOF/concurrency limits and silent
third-party stderr. Startup uses four independent workers; missing/invalid servers
are skipped. Reconnect refreshes discovery; tools/call is never retried after an
uncertain response. Remote config uses url (HTTPS or loopback HTTP), timeout
(1..30, default 10), optional token_env for a pre-obtained OAuth access token.
2025-11-25 POST JSON/SSE and server session headers are supported. Interactive
OAuth discovery, PKCE, refresh and older HTTP+SSE transport remain pending.
Remote responses are capped at 1 MiB; unsupported recursive/$ref/regex schemas
are rejected without network resolution. No sampling/roots authority advertised.

Protocol references: [MCP transport](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports),
[authorization](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization),
[JSON Schema validator](https://pypi.org/project/jsonschema/).
