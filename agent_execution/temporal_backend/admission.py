"""SDK-free rollout routing for new tasks; never an authorization grant."""
import hashlib
import json
import os
import re

SETTING = 'AGENT_TEMPORAL_TRIAL_OWNERS'


def owner_key(owner):
    """Hash the canonical trusted task_owner string, never user message fields."""
    return hashlib.sha256(owner.encode('utf-8')).hexdigest()


def trial_owners():
    raw = os.environ.get(SETTING)
    if raw is None:
        return None  # Compatibility: the existing explicit backend opt-in.
    if len(raw.encode('utf-8')) > 70 * 1024:
        raise ValueError(SETTING + ' exceeds its configuration limit')
    try:
        values = json.loads(raw)
    except (ValueError, TypeError):
        raise ValueError(SETTING + ' must be a JSON list of SHA-256 owner keys') from None
    if (not isinstance(values, list) or len(values) > 1024
            or any(not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{64}', value) for value in values)
            or len(set(values)) != len(values)):
        raise ValueError(SETTING + ' must contain at most 1024 unique SHA-256 owner keys')
    return frozenset(values)


def new_backend(owner, requested=None):
    from .config import selected_backend
    requested = selected_backend() if requested is None else requested
    if requested not in {'sqlite', 'temporal'}:
        raise ValueError('Unknown task backend')
    if requested == 'sqlite':
        return requested
    owners = trial_owners()
    return 'temporal' if owners is None or owner_key(owner) in owners else 'sqlite'


def summary():
    owners = trial_owners()
    return {'mode': 'explicit_backend' if owners is None else 'owner_allowlist',
            'owner_count': None if owners is None else len(owners)}
