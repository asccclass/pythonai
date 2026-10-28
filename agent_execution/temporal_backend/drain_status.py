"""Read-only local inventory for deciding whether legacy polling can drain."""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3


def inspect(path=None):
    root = Path(os.environ.get('LOCALAPPDATA') or Path.home()/'.local'/'share')/'PythonAI'
    database = Path(path or os.environ.get('AGENT_TASK_DB') or root/'tasks.db').resolve()
    if not database.is_file() or database.is_symlink():
        raise FileNotFoundError('Configured TaskStore database is missing or redirected')
    with closing(sqlite3.connect(database.as_uri()+'?mode=ro', uri=True)) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'tasks', 'execution_bindings', 'temporal_submissions', 'temporal_protocols'} <= tables:
            raise ValueError('TaskStore does not contain the Temporal drain inventory schema')
        legacy_tasks = {state: count for state, count in conn.execute('''
            SELECT t.state, count(*) FROM tasks t
            LEFT JOIN execution_bindings b ON b.owner=t.owner AND b.task_id=t.id
            WHERE COALESCE(b.backend,'sqlite')='sqlite' GROUP BY t.state
        ''')}
        temporal = {(workflow or 'PythonAIReadOnlyBridgeV1', state): count
                    for workflow, state, count in conn.execute('''
            SELECT p.workflow_type, s.state, count(*) FROM temporal_submissions s
            LEFT JOIN temporal_protocols p ON p.submission_id=s.id
            GROUP BY p.workflow_type, s.state
        ''')}
        pending_cancellations = (conn.execute("SELECT count(*) FROM temporal_cancellations WHERE delivery!='delivered'").fetchone()[0]
                                 if 'temporal_cancellations' in tables else 0)
    terminal = {'completed', 'failed', 'cancelled'}
    open_bridge = sum(count for (workflow, state), count in temporal.items()
                      if workflow == 'PythonAIReadOnlyBridgeV1' and state not in terminal)
    open_temporal = sum(count for (_, state), count in temporal.items() if state not in terminal)
    legacy_nonterminal = sum(count for state, count in legacy_tasks.items() if state not in terminal)
    return {
        'scope': 'local TaskStore counts only; no identifiers returned',
        'legacy_unbound_task_states': legacy_tasks,
        'temporal_submission_states': {f'{workflow}:{state}': count for (workflow, state), count in temporal.items()},
        'open_legacy_bridge_submissions': open_bridge,
        'open_temporal_submissions': open_temporal,
        'legacy_unbound_nonterminal_tasks': legacy_nonterminal,
        'pending_cancellation_deliveries': pending_cancellations,
        'local_inventory_drained': not (open_bridge or open_temporal or legacy_nonterminal or pending_cancellations),
        'safe_to_remove_legacy_worker': False,
        'external_checks_required': ['Temporal executions and schedules', 'other Worker fleets',
                                     'rollback retention window', 'unmanaged legacy tasks'],
    }


def main():
    print(json.dumps(inspect(), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
