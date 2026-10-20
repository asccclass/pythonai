# Execution and trust boundaries
- agent_execution/policy.py centralizes execution context/tool policy; sessions.py scopes identity/project/session data; approvals.py binds one-use human approvals. Model-visible tool filtering does not replace execution-side checks.
- tasks.py persists task/checkpoint state; budgets.py handles reservations and reconciliation across retries/children. Restarting an existing task must not replenish its persisted budget/deadline. Diagnose the exact task/owner, task_budgets and budget_calls.
- acceptance.py/results.py plus task_evaluator.py govern completion evidence; task-local history boundaries matter. RBAC denial is not successful code execution and must not trigger generic Critic retry loops.
- coordinator.py/worktrees.py isolate child work and delivery; git_delivery.py/git_provider.py/patches.py govern reviewed patch/PR actions.
- Temporal optional backend is in agent_execution/temporal_backend/; inspect its current README/contracts/tests before changing it. Never replay unknown side effects or fall back across backends after uncertain dispatch.
- Focused directory boundaries: turn_control/, user_profile/, scheduling/, workspace_lifecycle/, extensions/, code_index/, media/, improvement/, operations/. Keep same-purpose implementation and tests together.
