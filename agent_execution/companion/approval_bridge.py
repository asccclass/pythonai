"""Transport-owned human waiters; no model or client can grant approval here."""
from contextlib import contextmanager
from contextvars import ContextVar

_waiter = ContextVar('companion_approval_waiter', default=None)


@contextmanager
def approval_bridge(callback):
    token = _waiter.set(callback)
    try:
        yield
    finally:
        _waiter.reset(token)


def wait_for_approval(approval_id, name, arguments):
    callback = _waiter.get()
    return None if callback is None else callback(approval_id, name, arguments)
