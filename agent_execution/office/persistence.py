"""Bounded canonical intent snapshots and validated persistence limits."""

from dataclasses import dataclass, fields
import hashlib
import json
import math
import os


def canonical(value, maximum=65536):
    def validate(item, depth=0):
        if depth > 32:
            raise ValueError('Intent nesting exceeds 32 levels')
        if item is None or type(item) in (str, bool, int):
            return
        if type(item) is float and math.isfinite(item):
            return
        if type(item) is dict and all(type(key) is str for key in item):
            for child in item.values():
                validate(child, depth+1)
            return
        if type(item) is list:
            for child in item:
                validate(child, depth+1)
            return
        raise ValueError('Intents require finite JSON values and string object keys')
    validate(value)
    data = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    if len(data.encode('utf-8')) > maximum:
        raise ValueError('Intent payload capacity exceeded')
    return data


def digest(data):
    return hashlib.sha256(data.encode('utf-8')).hexdigest()


@dataclass(frozen=True)
class PersistenceLimits:
    assignments: int = 10000
    owner_assignments: int = 1000
    operations: int = 50000
    outbox: int = 100000
    storage_bytes: int = 128 * 1024 * 1024
    payload_bytes: int = 65536
    target_receipts: int = 100000
    delivery_lease_seconds: int = 30

    def __post_init__(self):
        for field in fields(self):
            value = getattr(self, field.name)
            maximum = {'storage_bytes':1024*1024*1024, 'payload_bytes':1024*1024,
                       'delivery_lease_seconds':300}.get(field.name, 1000000)
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError(f'Invalid office persistence limit: {field.name}')
        if self.owner_assignments > self.assignments:
            raise ValueError('Owner assignment limit exceeds global limit')

    @classmethod
    def load(cls, environ=None):
        env = os.environ if environ is None else environ
        values = {}
        for field in fields(cls):
            raw = env.get('AGENT_OFFICE_PERSIST_' + field.name.upper())
            if raw is not None:
                if not isinstance(raw, str) or not raw.isascii() or not raw.isdecimal():
                    raise ValueError('Persistence limits require unsigned decimal integers')
                values[field.name] = int(raw)
        return cls(**values)
