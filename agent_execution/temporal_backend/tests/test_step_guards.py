import threading
from unittest.mock import Mock, patch

import pytest

from agent_execution.temporal_backend.context import host_slot


def test_host_slot_cancellation_does_not_leak_capacity(monkeypatch):
    import agent_execution.temporal_backend.context as context
    slot=threading.BoundedSemaphore(1)
    monkeypatch.setattr(context,'_slots',slot)
    with pytest.raises(InterruptedError):
        with host_slot(lambda:True):
            raise AssertionError('Cancelled body executed')
    assert slot.acquire(blocking=False)
    slot.release()


def test_host_slot_waiter_observes_cancellation_before_execution(monkeypatch):
    import agent_execution.temporal_backend.context as context
    slot=threading.BoundedSemaphore(1)
    monkeypatch.setattr(context,'_slots',slot)
    slot.acquire()
    try:
        with pytest.raises(InterruptedError):
            with host_slot(lambda:True):
                raise AssertionError('Cancelled waiter executed')
    finally:
        slot.release()


@pytest.mark.parametrize('error,code',[(PermissionError('secret'),'PermissionDenied'),
    (KeyError('secret'),'MissingArtifact'),(ValueError('secret'),'InvalidStateOrArtifact'),
    (RuntimeError('secret'),'StepExecutionUnknown')])
def test_activity_error_is_classified_without_serializing_source_details(error,code):
    pytest.importorskip('temporalio')
    from temporalio.exceptions import ApplicationError
    from temporalio.converter import DefaultFailureConverter, DefaultPayloadConverter
    from temporalio.api.failure.v1 import Failure
    from agent_execution.temporal_backend.step_activities import protected
    @protected
    def failing(payload): raise error
    with patch('agent_execution.temporal_backend.step_activities.activity.is_cancelled',return_value=False):
        with pytest.raises(ApplicationError) as caught:
            failing({})
    assert caught.value.type==code
    failure=Failure()
    DefaultFailureConverter().to_failure(caught.value,DefaultPayloadConverter(),failure)
    assert 'secret' not in failure.SerializeToString().decode('utf-8',errors='ignore')
