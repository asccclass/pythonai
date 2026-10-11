# Scheduled Agent and Skill execution

`store.py` owns the SQLite migration, owner-scoped management, atomic claim,
lease fencing, bounded retries and meaningful-change notifications. `engine.py`
runs each occurrence in the private `worker.py` subprocess through the native
process-tree runner. The worker restores the original execution context and
rechecks remote privileges. Uncertain writes stop for human review.

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

Tests: `python -m pytest agent_execution/tests/test_scheduling.py tests/test_scheduler.py`.
They cover real worker/script execution and termination, concurrency, fencing,
Taipei timing, ownership, legacy migration, approvals and notification deduplication.
