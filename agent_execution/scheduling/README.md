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
Notifications are a durable queryable queue; automatic platform push is pending.

Tests: `python -m pytest agent_execution/tests/test_scheduling.py tests/test_scheduler.py`.
They cover real worker/script execution and termination, concurrency, fencing,
Taipei timing, ownership, legacy migration, approvals and notification deduplication.
