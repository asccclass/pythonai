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
2025-11-25 POST JSON/SSE and server session headers are supported. OAuth public
clients support resource metadata discovery, OAuth/OIDC issuer metadata, S256 PKCE,
one-use owner-bound state, exact redirect validation and serialized refresh rotation.
Older HTTP+SSE transport remains unsupported.
ContactAI/MemAuth integration and setup: [contactai/README.md](contactai/README.md).
Remote `protocol_version` can select `2025-06-18` (default `2025-11-25`);
`health_method` can select `tools/list` (default `ping`). Authentication providers
`token_env`, `oauth`, and `memauth` are mutually exclusive.
Remote responses are capped at 1 MiB; unsupported recursive/$ref/regex schemas
are rejected without network resolution. No sampling/roots authority advertised.

For interactive OAuth, configure oauth instead of token_env:
```json
{"url":"https://mcp.example.com/mcp","oauth":{"issuer":"https://auth.example.com/tenant","client_id":"pre-registered-public-client","redirect_uri":"http://127.0.0.1:8765/callback","scope":"files:read"}}
```
Human CLI: `python -m agent_execution.extensions.oauth_cli begin server.json`.
Open authorization_url, complete the provider login, then pass the resulting URL
to `finish server.json --callback-url URL`. Alternatively use
`python -m agent_execution.extensions.oauth_cli login server.json --timeout 120`
to bind the registered loopback callback before advertising authorization_url;
open that URL in your browser and the CLI completes token exchange automatically.
Listener redirects must be HTTP with literal 127.0.0.1 or ::1, an explicit port
and path. The URI is preserved exactly; no alternate port/origin is substituted.
The manual begin/finish flow still supports other registered redirect URIs.
`revoke server.json` removes local credentials; revoke grants with
provider account controls too. No tokens/verifiers are printed. State lasts ten
minutes; credentials are private SQLite at AGENT_MCP_OAUTH_DB (default
LOCALAPPDATA/PythonAI/mcp-oauth.db), bounded 16 MiB, 100 pending states/1000 grants.
Protect it with OS account permissions and include it in private backups.

The callback listener verifies exact Host/path and constant-time state, accepts
one GET code/error callback, rejects bodies/duplicate fields, and never logs or
reflects code/token values. Requests are bounded 8 KiB/one second; waiting is
1..300 seconds (default 120). Invalid requests do not consume state. Timeout,
cancellation and normal exit close the socket and remove only that pending state,
preserving an existing grant. Port conflicts fail before authorization creation.
Local tests include a native CLI with real HTTP PKCE/token exchange; an external
provider's user login/consent is still a separate acceptance step. Loopback design
reference: [RFC 8252](https://www.rfc-editor.org/rfc/rfc8252#section-7.3).

The trusted issuer must match protected resource metadata exactly. Resource
metadata origins are bound to the MCP resource; authorization/token endpoints
must use the issuer origin or explicitly configured allowed_endpoint_origins.
Redirects are never followed and responses are bounded 64 KiB/10 seconds. Public
client registration is supplied by the deployment; dynamic registration and
confidential-client authentication are not implemented. Token exchange includes
the resource indicator. Refresh occurs before a request, never by replaying a
401 tools/call. Unknown refresh outcomes remove local tokens and require a new
human login. An OAuth client belongs to its creation owner; service-wide local
grants are not inherited by another remote identity. Initialize/discover clients
under the intended owner. Real provider login/consent is not proven by fixtures.

Protocol references: [MCP transport](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports),
[authorization](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization),
[JSON Schema validator](https://pypi.org/project/jsonschema/).

Authorization endpoint query parameters supplied by the issuer are preserved.
Collision with client-owned response_type/client_id/redirect_uri/state/PKCE/
resource/scope parameters (including percent-encoded keys and blank values) is
rejected before persisting pending state; existing grants are untouched.
Query parsing admits at most 32 fields. This prevents ambiguous authorization
URLs while retaining provider-specific tenant/prompt parameters. See
[RFC 6749 section 3.1](https://www.rfc-editor.org/rfc/rfc6749#section-3.1).
