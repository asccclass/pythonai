"""Human review projections scoped to the authenticated task owner.

This module never executes artifact content, applies patches or publishes Git.
"""
from contextlib import closing
import json
from urllib.parse import unquote

from .policy import current_context
from .tasks import TaskStore, task_owner
from .patches import ArtifactStore
from .approvals import ApprovalStore


def clipped(value, maximum=65536):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return {'text': text[:maximum], 'truncated': len(text) > maximum}


def handle_review(route, method, role):
    context = current_context()
    owner = task_owner(context)
    store = TaskStore()
    if route == '/api/review/context' and method == 'GET':
        return 200, {'identity': context.identity, 'project': context.project,
                     'session': context.session, 'role': role}
    if route == '/api/review/tasks' and method == 'GET':
        return 200, store.list(owner)
    parts = route.split('/')
    if len(parts) not in {5, 6} or parts[1:4] != ['api', 'review', 'tasks']:
        return 404, {'error': 'not_found'}
    key = unquote(parts[4])
    try:
        task = store.get(owner, key)
        if len(parts) == 6:
            if method != 'POST' or parts[5] != 'cancel':
                return 404, {'error': 'not_found'}
            if role != 'admin':
                return 403, {'error': 'forbidden'}
            return 200, {'cancelled': store.cancel(owner, key)}
        if method != 'GET':
            return 404, {'error': 'not_found'}
        data = task['checkpoint']
        artifacts = ArtifactStore()
        with closing(artifacts.connect()) as conn:
            rows = conn.execute('SELECT id,kind,state,payload,updated FROM delivery_artifacts '
                                'WHERE owner=? AND task=? ORDER BY updated DESC LIMIT 20', (owner, key)).fetchall()
        projected = []
        for row in rows:
            payload = json.loads(row['payload'])
            # Deliberate allowlist: no provider configuration, full before/after or filesystem reads.
            projected.append({'id': row['id'], 'kind': row['kind'], 'state': row['state'],
                              'updated': row['updated'], 'diff': clipped(payload.get('diff', '')),
                              'history_diff': clipped(payload.get('history_diff', '')),
                              'head': payload.get('head'), 'digest': payload.get('digest'),
                              'evidence': clipped({k: payload[k] for k in
                                  ('local_acceptance', 'observation', 'feedback', 'summary') if k in payload})})
        with closing(store.connect()) as conn:
            steps = conn.execute('SELECT id,name,substr(input,1,65537) AS input,dependencies,state,'
                                 'substr(result,1,65537) AS result FROM task_steps '
                                 'WHERE owner=? AND task_id=? ORDER BY rowid LIMIT 101', (owner, key)).fetchall()
        events = [{k: clipped(v) if k in {'input', 'result'} else v for k, v in dict(row).items()}
                  for row in steps[:100]]
        plan = data.get('plan', data.get('analysis'))
        if plan is None:
            plan = [item.get('content') for item in data.get('messages', [])
                    if item.get('role') == 'assistant' and item.get('content')][-20:]
        return 200, {'task': {k: task[k] for k in
                        ('id', 'goal', 'state', 'phase', 'verification', 'updated', 'attempts')},
                     'plan': clipped(plan),
                     'criteria': clipped(task['criteria']), 'evaluation': clipped(data.get('evaluation', {})),
                     'question': clipped(data.get('question', data.get('human_question', task.get('reply') if task['state'] == 'suspended' else ''))),
                     'reply': clipped(task.get('reply') or ''), 'events': events,
                     'events_truncated': len(steps) > 100, 'artifacts': projected,
                     'approvals': ApprovalStore().pending(context.identity, context.session)}
    except KeyError:
        return 404, {'error': 'not_found'}
