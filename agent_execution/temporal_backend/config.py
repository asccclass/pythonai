"""Validated settings for the first, same-host Temporal milestone."""
from dataclasses import dataclass
import os
import re


def operation_limits():
    from agent_execution.delivery import positive_limit
    return {name: positive_limit(name, default) for name, default in {
        'AGENT_TEMPORAL_ARTIFACT_MAX_BYTES': 2*1024*1024,
        'AGENT_TEMPORAL_LEDGER_STORAGE_BYTES': 256*1024*1024,
        'AGENT_TEMPORAL_OPERATION_LIMIT': 100000,
    }.items()}


def selected_backend():
    value = os.environ.get('AGENT_EXECUTION_BACKEND', 'sqlite').strip().lower()
    if value not in {'sqlite', 'temporal'}:
        raise ValueError('AGENT_EXECUTION_BACKEND must be sqlite or temporal')
    return value


@dataclass(frozen=True)
class Settings:
    address: str
    namespace: str
    concurrency: int = 2
    engine: str = 'bridge'

    @classmethod
    def load(cls):
        address = os.environ.get('AGENT_TEMPORAL_ADDRESS', '127.0.0.1:7233')
        if not re.fullmatch(r'(127\.0\.0\.1|localhost):[0-9]{1,5}', address):
            raise ValueError('M0 Temporal supports loopback only; remote TLS deployment is not enabled')
        if not 1 <= int(address.rsplit(':', 1)[1]) <= 65535:
            raise ValueError('Invalid Temporal port')
        namespace = os.environ.get('AGENT_TEMPORAL_NAMESPACE', 'pythonai-dev')
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', namespace):
            raise ValueError('Invalid Temporal namespace')
        concurrency = int(os.environ.get('AGENT_TEMPORAL_ACTIVITY_CONCURRENCY', '2'))
        if not 1 <= concurrency <= 8:
            raise ValueError('Temporal Activity concurrency must be 1..8')
        operation_limits()
        engine = os.environ.get('AGENT_TEMPORAL_ENGINE', 'bridge')
        if engine not in {'bridge', 'steps', 'human'}:
            raise ValueError('AGENT_TEMPORAL_ENGINE must be bridge, steps or human')
        return cls(address, namespace, concurrency, engine)
