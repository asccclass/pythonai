"""Low-cardinality aggregate metrics from the bounded local trace store."""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3


def snapshot(path=None):
    default_database = Path(os.environ.get('LOCALAPPDATA', Path.home()))/'PythonAI'/'traces.db'
    database = Path(path or os.environ.get('AGENT_TRACE_DB') or default_database).resolve()
    empty = {'available': False, 'spans_total': 0, 'spans_failed': 0,
             'spans_unfinished': 0, 'latency_seconds_sum': 0.0,
             'latency_seconds_p95': 0.0, 'model_requests_total': 0,
             'tool_spans_total': 0, 'workflow_activity_spans_total': 0,
             'model_attempt_spans_total': 0, 'tokens_total': 0,
             'known_cost_usd_total': 0.0, 'scope': 'aggregate only; no owner or task labels'}
    if not database.is_file() or database.is_symlink():
        return empty
    with closing(sqlite3.connect(database.as_uri()+'?mode=ro', uri=True, timeout=2)) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='spans'").fetchone():
            return empty
        total, failed, unfinished, latency = conn.execute('''
            SELECT count(*), sum(status='failed'), sum(status='running'),
                   coalesce(sum(elapsed),0) FROM spans
        ''').fetchone()
        counts = dict(conn.execute('SELECT kind,count(*) FROM spans GROUP BY kind'))
        rows = conn.execute('SELECT elapsed,metadata FROM spans WHERE elapsed IS NOT NULL').fetchall()
    durations = sorted(row[0] for row in rows)
    p95 = durations[max(0, (95*len(durations)+99)//100-1)] if durations else 0.0
    tokens = cost = 0
    for _, raw in rows:
        try:
            metadata = json.loads(raw)
        except (TypeError, ValueError):
            continue
        tokens += metadata.get('tokens', 0) if isinstance(metadata.get('tokens', 0), int) else 0
        value = metadata.get('cost_usd', 0)
        if isinstance(value, (int, float)) and value >= 0:
            cost += value
    return {'available': True, 'spans_total': total, 'spans_failed': failed or 0,
            'spans_unfinished': unfinished or 0, 'latency_seconds_sum': round(latency, 6),
            'latency_seconds_p95': round(p95, 6), 'model_requests_total': counts.get('model', 0),
            'tool_spans_total': counts.get('tool', 0),
            'workflow_activity_spans_total': counts.get('workflow_activity', 0),
            'model_attempt_spans_total': counts.get('attempt', 0), 'tokens_total': tokens,
            'known_cost_usd_total': round(cost, 8),
            'scope': 'aggregate only; no owner or task labels'}


def prometheus(path=None):
    """Render bounded aggregate snapshots for a Prometheus textfile collector."""
    values = snapshot(path)
    metrics = (
        ('agent_execution_trace_store_available', 'Whether the local trace store is available',
         int(values['available'])),
        ('agent_execution_spans', 'Spans currently retained in the local trace window', values['spans_total']),
        ('agent_execution_spans_failed', 'Failed spans currently retained', values['spans_failed']),
        ('agent_execution_spans_unfinished', 'Unfinished spans currently retained', values['spans_unfinished']),
        ('agent_execution_latency_seconds_sum', 'Sum of retained span latencies in seconds', values['latency_seconds_sum']),
        ('agent_execution_latency_seconds_p95', 'P95 of completed retained span latencies in seconds', values['latency_seconds_p95']),
        ('agent_execution_model_requests', 'Retained model request spans', values['model_requests_total']),
        ('agent_execution_tool_spans', 'Retained tool spans', values['tool_spans_total']),
        ('agent_execution_workflow_activity_spans', 'Retained Temporal workflow activity spans', values['workflow_activity_spans_total']),
        ('agent_execution_model_attempt_spans', 'Retained model attempt spans', values['model_attempt_spans_total']),
        ('agent_execution_tokens', 'Known tokens across completed retained spans', values['tokens_total']),
        ('agent_execution_known_cost_usd', 'Known cost in USD across completed retained spans', values['known_cost_usd_total']),
    )
    return ''.join(f'# HELP {name} {help_text}\n# TYPE {name} gauge\n{name} {value}\n'
                   for name, help_text, value in metrics)
