"""Temporal Service and fresh task-queue poller health checks."""
import asyncio
from contextlib import closing
from datetime import datetime, timezone
from datetime import timedelta
import os
from pathlib import Path
import sqlite3

from temporalio.api.enums.v1.task_queue_pb2 import TASK_QUEUE_TYPE_ACTIVITY, TASK_QUEUE_TYPE_WORKFLOW
from temporalio.api.taskqueue.v1.message_pb2 import TaskQueue
from temporalio.api.workflowservice.v1.request_response_pb2 import DescribeTaskQueueRequest

from .config import Settings

POLL_FRESH_SECONDS = 60
_clients = {}


def _queue_inventory(settings):
    terminal = ('completed', 'failed', 'cancelled')
    root = Path(os.environ.get('LOCALAPPDATA') or Path.home()/'.local'/'share')/'PythonAI'
    database = Path(os.environ.get('AGENT_TASK_DB') or root/'tasks.db').resolve()
    if not database.is_file() or database.is_symlink():
        raise FileNotFoundError('Temporal TaskStore is missing or redirected')
    with closing(sqlite3.connect(database.as_uri()+'?mode=ro', uri=True)) as conn:
        token_row = conn.execute('SELECT token FROM temporal_host WHERE id=1').fetchone()
        if token_row is None:
            raise ValueError('Temporal host binding is missing')
        token = token_row[0]
        suffixes = {'bridge': '-readonly-v1', 'steps': '-steps-readonly-v1',
                    'human': '-steps-readonly-v2', 'approval': '-steps-readonly-v3'}
        queues = {f'pythonai-host-{token}{suffixes[settings.engine]}'}
        queries = {row[1] for row in conn.execute(
            'SELECT name, type FROM sqlite_master WHERE type=\'table\'')}
        if 'temporal_submissions' in queries:
            placeholders = ','.join('?' for _ in terminal)
            queues.update(row[0] for row in conn.execute(
                f'''SELECT queue FROM temporal_submissions
                    WHERE address=? AND namespace=? AND state NOT IN ({placeholders})''',
                (settings.address, settings.namespace, *terminal)))
    labels = {}
    suffixes = {
        '-readonly-v1': 'bridge', '-steps-readonly-v1': 'steps',
        '-steps-readonly-v2': 'human', '-steps-readonly-v3': 'approval',
    }
    for queue in queues:
        label = next((name for suffix, name in suffixes.items() if queue.endswith(suffix)), 'other_active')
        if not queue.startswith(f'pythonai-host-{token}-'):
            label = 'host_binding_mismatch'
        if label in labels and labels[label] != queue:
            label = f'{label}_{len(labels)}'
        labels[label] = queue
    return labels


async def inspect(settings=None, client=None, *, fresh_seconds=POLL_FRESH_SECONDS):
    """Report Service connectivity and fresh workflow/activity pollers without identities."""
    settings = settings or Settings.load()
    if client is None:
        key = (settings.address, settings.namespace)
        client = _clients.get(key)
        if client is None:
            from .client import connect
            client = await connect(settings)
            _clients[key] = client
    labels = _queue_inventory(settings)
    now = datetime.now(timezone.utc)
    reports = []
    for label, queue in sorted(labels.items()):
        counts = {}
        observed = {}
        backlog_count = 0
        backlog_ages = []
        ages = []
        for kind, queue_type in (('workflow', TASK_QUEUE_TYPE_WORKFLOW),
                                 ('activity', TASK_QUEUE_TYPE_ACTIVITY)):
            response = await asyncio.wait_for(client.workflow_service.describe_task_queue(
                DescribeTaskQueueRequest(namespace=settings.namespace,
                    task_queue=TaskQueue(name=queue), task_queue_type=queue_type,
                    report_pollers=True, report_stats=True), timeout=timedelta(seconds=3)), timeout=4)
            observed[kind] = len(response.pollers)
            if response.HasField('stats'):
                backlog_count += response.stats.approximate_backlog_count
                duration = response.stats.approximate_backlog_age
                backlog_ages.append(duration.seconds + duration.nanos / 1_000_000_000)
            fresh = []
            for poller in response.pollers:
                if not poller.HasField('last_access_time'):
                    continue
                age = max(0.0, (now-poller.last_access_time.ToDatetime(timezone.utc)).total_seconds())
                ages.append(age)
                if age <= fresh_seconds:
                    fresh.append(poller)
            counts[f'{kind}_pollers'] = len(fresh)
        reports.append({'queue_role': label, **counts,
                        'observed_workflow_pollers': observed['workflow'],
                        'observed_activity_pollers': observed['activity'],
                        'approximate_backlog_count': backlog_count,
                        'approximate_backlog_age_seconds': round(max(backlog_ages), 3) if backlog_ages else 0.0,
                        'freshest_poller_age_seconds': round(min(ages), 3) if ages else None,
                        'ready': not label.startswith('host_binding_mismatch') and
                                 counts['workflow_pollers'] > 0 and counts['activity_pollers'] > 0})
    return {'connected': True, 'ready': bool(reports) and all(item['ready'] for item in reports),
            'poller_fresh_seconds': fresh_seconds, 'queues': reports,
            'scope': 'Temporal task queues bound to this TaskStore; poller identities omitted'}


def inspect_sync(settings=None, timeout=12):
    """Run the SDK check on the client bridge loop for synchronous operator endpoints."""
    from .client import bridge
    return bridge().call(inspect(settings), timeout)
