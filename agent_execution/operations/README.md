# Operational verification (T21)

CLI: `python -m agent_execution.operations doctor|readiness|traces|syntax|benchmark`.
Admin-authenticated `/api/ops/readiness` and `/api/ops/traces` use the same identity/
project/conversation boundary as review APIs. `/health` remains liveness only.
Readiness checks workspace writes, required dependencies and configured storage
integrity/admission. It does not contact a provider or promise remote availability.
Doctor additionally reports optional browser/document/OCR/Node/Docker dependencies.

Task/tool/model/attempt/audio spans carry owner, trace, parent span, task/parent
task, episode and coordinator node. Root budget IDs correlate native children;
in-process spans also carry parent_span. AGENT_TRACE_DB defaults to
LOCALAPPDATA/PythonAI/traces.db; AGENT_TRACE_ENABLED=0 disables tracing. Storage is
owner-scoped, keeps seven days, admits 20000 rows and prunes 500 oldest at capacity;
metadata is at most 8 KiB. This is retention, not a hard disk quota. Observation
failure never retries or blocks execution. An interrupted span remains running
for diagnosis; only T08 checkpoints decide task recovery.

Trace summaries cover latest 1000 owner spans, failures, unfinished spans, latency,
model requests and actual retry attempts. Model token/cost deltas are conservative
T09 budget charges/reservations; authoritative streaming reconciliation remains
task_budgets. Unknown rates/audio costs are marked unknown, never a zero-price
claim. Model span latency covers SDK request acquisition; task/attempt span covers
the full consumed stream. Cost accounting is not a provider invoice.

Trace metadata and non-conversation memory audit events are redacted for credential
keys, configured secret values, Bearer tokens, provider keys and credential URLs;
observability CLI output is redacted too. Original user/assistant conversation and
authoritative checkpoints remain private operational data needed for retrieval/
resume, not sanitized public logs. OS permissions must protect all private stores;
rule-based redaction cannot guarantee detection of an unknown secret format.

`backup --database PATH [--database PATH] --destination NEW_DIR` uses SQLite online
backup and stores schema digest/hash/integrity evidence. Each DB is consistent;
stop workers for a consistent cross-database snapshot. `restore --source BACKUP
--destination NEW_DIR` verifies paths/hashes/schema and refuses existing targets.
Restored numbered files map to original paths in manifest.json; review and configure
the intended paths explicitly, never blindly replace live databases. Keep backups
private: they contain conversation, approvals, checkpoints and other sensitive data.
`migrate --kind memory|tasks|scheduler|approvals|extensions|improvement|trace
--database PATH --destination NEW_BACKUP --offline` first backs up, then calls the
official idempotent store migration and verifies integrity. Failure retains the
backup/changed DB for review; it does not overwrite possible concurrent edits.
limits: 32 DBs, 256 MiB each, backup deadline 10 seconds/DB. Browser profile files
and arbitrary workspace artifacts are not included in this SQLite backup helper.

suite.json defines ten fixed acceptance/failure-injection scenarios: native UTF-8/
cancel, false completion, failed Skill write prevention, crash checkpoint recovery,
uncertain scheduled writes, concurrency, notification stopping, exact MCP routing
and memory quality. Benchmark executes a 120 second private worker and reports
completion/recovery rates, case latency/failures, tracked-source changes and unknown
external cost. Local fixtures are labeled and must not be represented as real
provider success or model quality. Case assertions verify selected effects; this
is not a global monitor of all host/external side effects.

pytest.ini confines collection to tests/ and agent_execution/tests/, avoiding old
ignored temp experiments. Syntax checks parse tracked Python, including maintenance
scripts; patch_server.py's literal backslash-n syntax error is repaired. CI separates
Windows unit/native, opt-in Linux Docker/Chromium, VS Code Host and manually enabled
real provider jobs. requirements-ci.txt is a minimal test runtime; local neural
classification is mocked. CI configuration itself has not run on GitHub until pushed.
The story text loader imports its optional CKIP runtime only when starting neural
analysis; offline text splitting never requires downloading a neural model.
benchmark_baseline.json records the local ten-case fixture report, not provider quality.
