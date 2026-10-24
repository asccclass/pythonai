# Scheduled Agent and Skill execution

`store.py` owns the SQLite migration, owner-scoped management, atomic claim,
lease fencing, bounded retries and meaningful-change notifications. `engine.py`
runs each occurrence in the private `worker.py` subprocess through the native
process-tree runner. The worker restores the original execution context and
rechecks remote privileges. Uncertain writes stop for human review.

The default backend remains SQLite. New opt-in Temporal schedules use the trusted
`agent_execution.temporal_backend.schedule_cli` / `Schedules` bridge; see
`../temporal_backend/README.md` (TP19). Legacy claims explicitly exclude Temporal
rows; native desired enable is stored separately and the legacy is_enabled stays
false. Existing read-only agent/watch jobs can explicitly preview, stage, cut over
and roll back with schedule_migration_cli (TP20). No automatic migration or
historical backfill is performed; active/uncertain jobs remain fenced. See the
backend README for source digests, UTC fences and restartable pending intents.

Configuration: `SCHEDULER_DB_PATH` (default communication/scheduler.db), five-field
cron and IANA timezone (new jobs default Asia/Taipei), timeout 1..300 seconds,
max_runs 1..1000. The CLI and manage_schedule tool expose owner-scoped list,
disable and notifications. The model tool requires a one-use human approval.
Notifications are a durable queryable queue. The communication service dispatches
Telegram/Discord/LINE notifications to the original conversation through its
configured adapters, rechecking the original identity's admin role and sender
allowlist. Local/API/WebSocket owners retain queryable notifications; there is no
guessed cross-channel forwarding. Unconfigured adapters leave notifications pending.
Watch run-limit exhaustion and final retry failure become needs_input and notify
even when the last observation is unchanged. Failure digests include bounded
error evidence; identical intermediate failures remain quiet.
Actionable failure/stop reasons precede observations in queued notifications so
both the 64 KiB storage limit and 1800-character platform limit preserve them;
run output remains separately available and unchanged.
Each notification is atomically claimed once, with one bounded 1800-character
message. Failures or a lost 60-second delivery claim become uncertain and are never
automatically resent; operators inspect delivery/error_type with notifications.
Manual disable cancels unclaimed notifications. Shutdown stops new claims; an
already sent request cannot be recalled. Batch admission stops after ten seconds;
the configured adapter HTTP request retains its existing 20-second timeout.
Successful fixture delivery does not prove production provider receipt.
Telegram long polling also starts the scheduler and dispatches after each poll;
notification latency includes the configured polling timeout. Failed notification
storage reports a diagnostic without terminating the primary communication loop.

Tests (Windows): `.\venv\Scripts\python.exe -m pytest agent_execution/tests/test_scheduling.py tests/test_scheduler.py`.
They cover real worker/script execution and termination, concurrency, fencing,
Taipei timing, ownership, legacy migration, approvals and notification deduplication.
