"""Subprocess Worker used by the isolated deployment rollout test."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import sys
import time

from temporalio import activity
from temporalio.client import Client
from temporalio.worker import Worker

from agent_execution.temporal_backend.config import Settings
from agent_execution.temporal_backend.contracts import STEP_ACTIVITIES
from agent_execution.temporal_backend.store import Store
from agent_execution.temporal_backend.workflows import AgentTaskWorkflowV1


async def serve(mode, marker):
    settings = Settings.load()
    store = Store()
    client = await Client.connect(settings.address, namespace=settings.namespace)
    if mode == 'old':
        @activity.defn(name=STEP_ACTIVITIES['bootstrap'])
        def interrupted_bootstrap(payload: dict) -> dict:
            Path(marker).write_text('started', encoding='utf-8')
            while True:
                activity.heartbeat('old-worker')
                time.sleep(.2)

        activities = [interrupted_bootstrap]
    else:
        from agent_execution.temporal_backend.step_activities import ACTIVITIES
        from agent_execution.temporal_backend.tests.test_step_integration import provider
        from task_evaluator import TaskEvaluator, EvaluationResult
        from unittest.mock import patch

        class Evaluator(TaskEvaluator):
            def capture_state(self):
                return {}

            def evaluate(self, **kwargs):
                return EvaluationResult(True, 1, 'ok')

        raw, _ = provider()
        patcher = patch('server.get_client', return_value=raw)
        patcher.start()
        activities = ACTIVITIES
    with ThreadPoolExecutor(max_workers=2) as executor:
        worker = Worker(client, task_queue=store.queue_for('steps'),
                        workflows=[AgentTaskWorkflowV1], activities=activities,
                        activity_executor=executor, max_concurrent_activities=2)
        if mode == 'new':
            from unittest.mock import patch
            patch('server.TaskEvaluator', Evaluator).start()
        async with worker:
            Path(marker).write_text('ready', encoding='utf-8')
            await asyncio.Future()


if __name__ == '__main__':
    asyncio.run(serve(sys.argv[1], sys.argv[2]))
