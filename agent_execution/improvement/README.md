# Memory quality and reviewed improvement

Doctor now saves immutable owner-scoped proposals instead of writing global
agent_persona memories. Legacy --apply saves a proposal only. Skills can be
proposed as extension-version references and must also pass the T18 test/approval
gate; no untested Skill code is activated by Doctor.

Persona CLI: `python -m agent_execution.improvement show ID`, `evaluate ID`,
`activate ID --approval-id APPROVAL`, `revoke NAME`, `rollback ID --approval-id
APPROVAL`, `list`. Activation requires 5..100 unique fixed cases, measurable higher
verified success, zero case regressions and zero candidate unexpected side effects.
Evidence/suite/proposal digests and the current baseline are frozen; changed
baseline invalidates activation. Human approval is one-use and owner/session bound.
Rollback requires a previously approved version plus a fresh human decision.
Max 20 active names, 20 rules/proposal, 8 KiB proposal/guidance, 1000 versions and
64 MiB DB. AGENT_IMPROVEMENT_DB defaults to LOCALAPPDATA/PythonAI/improvements.db.

Deploy trusted AGENT_IMPROVEMENT_RUNNER (Python module with run(case, proposals))
and AGENT_IMPROVEMENT_CASES (JSON list). Runner returns verified boolean,
unexpected_side_effects integer, evidence string. A configured adapter must perform
real acceptance checks; model success prose is not evidence. Each case/variant is
a native child with 15 second limit, total suite 120 seconds, READ_TOOLS admission
and normal model budget. The adapter itself is trusted host Python, not an OS
sandbox. Run writing benchmarks in isolated worktrees/containers and assert actual
effects. Offline fixtures verify gate mechanics; they do not establish real model
quality improvement. No production persona is activated by repository tests.

Retrieval excludes archived/expired/superseded data (existing store rules), excludes
unreviewed legacy agent_persona, and suppresses unresolved contradictory values for
explicit single-value predicates (e.g. timezone/prefers_language). Other predicates
remain multi-valued. Source roles are labeled, not treated as proof of truth.
Raw memory enters as quoted user reference data before the actual latest user goal,
never as a system instruction. Reviewed optional guidance has a separate advisory
message and never overrides policies/current requests. Physical tenant databases
remain isolated under T05; no cross-tenant profile guessing is introduced.

retrieval_eval.py reports labeled precision/recall and forbidden hits, including
empty retrieval. Tests exercise conflicts/expiry/hostile personas, source labels,
owner isolation, real native benchmark adapters, regressions, stale baseline,
approval/revoke/rollback and Doctor --apply without memory injection. Broader
provider-backed retrieval/persona/Skill quality datasets remain deployment work.
