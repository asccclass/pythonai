"""Opt-in writer and regression reader for sanitized, committed Workflow histories."""
import hashlib
import json
import os
from pathlib import Path
import re
import sys

FIXTURE_DIR = Path(__file__).parent / 'fixtures' / 'workflow_history'
WRITE_ENV = 'PYTHONAI_WRITE_TEMPORAL_REPLAY_FIXTURES'


def save(name, history, submission_id):
    """Write a stable fixture only during an explicit local generation run."""
    if os.environ.get(WRITE_ENV) != '1':
        return
    if name not in {'v1', 'v1_pre_patch', 'v2', 'v3'}:
        raise ValueError('Unknown replay fixture name')
    stable_id = hashlib.sha256(('pythonai-replay-fixture:' + name).encode()).hexdigest()[:32]
    raw = history.to_json().replace(submission_id, stable_id)

    def sanitize(value, key=''):
        if isinstance(value, dict):
            return {field: sanitize(child, field) for field, child in value.items()}
        if isinstance(value, list):
            return [sanitize(child, key) for child in value]
        if isinstance(value, str):
            if key == 'stackTrace':
                return '<redacted local stack trace>'
            if key in {'identity', 'workerIdentity'} or re.fullmatch(r'\d+@.+-[a-f0-9]{32}', value):
                return 'pythonai-replay-fixture'
            if key == 'workflowId' and value.startswith('pythonai/'):
                return 'pythonai/replay-fixture/' + name
            if key in {'name', 'normalName'} and value.startswith('pythonai-host-'):
                return 'pythonai-replay-fixture-queue'
            for root in {str(Path(__file__).resolve().parents[3]), sys.prefix, sys.base_prefix}:
                if root and root != os.path.abspath(os.sep):
                    value = value.replace(root, '<LOCAL_ROOT>')
            return value
        return value

    payload = {'workflow_id': 'pythonai-replay-fixture-' + name,
               'history': sanitize(json.loads(raw))}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + '\n'
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    (FIXTURE_DIR / (name + '.json')).write_text(encoded, encoding='utf-8')
