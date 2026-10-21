"""Cooperative Activity liveness shared by bridge and step protocols."""
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
import threading

from temporalio import activity
from .ledger import OperationUncertain


class ActivityLeaseLost(OperationUncertain):
    """Liveness could not be confirmed; effects must not be retried blindly."""

_guard = ContextVar('temporal_activity_guard', default=None)
HEARTBEAT_SECONDS = 10


def cancelled():
    if activity.is_cancelled():
        return True
    guard = _guard.get()
    if guard is not None and guard.is_set():
        raise ActivityLeaseLost('Activity heartbeat delivery lost')
    return False


@contextmanager
def activity_guard():
    stop, lost = threading.Event(), threading.Event()
    token = _guard.set(lost)

    def heartbeat():
        while not stop.wait(HEARTBEAT_SECONDS):
            try:
                activity.heartbeat()
            except Exception:
                lost.set()
                return

    thread = threading.Thread(target=copy_context().run, args=(heartbeat,),
                              name='temporal-activity-heartbeat', daemon=True)
    thread.start()
    try:
        yield
        if cancelled():
            raise InterruptedError('Activity cancelled or heartbeat delivery lost')
    finally:
        stop.set()
        thread.join()
        _guard.reset(token)
