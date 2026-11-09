"""Real native resume/crash fixture, invoked with this project's venv Python."""
import asyncio
import json
import os
import sys
from types import SimpleNamespace as NS
from unittest.mock import patch

from openai.types.chat import ChatCompletionMessage
from agent_execution.approvals import ApprovalStore
from agent_execution.delivery import EventStore
from agent_execution.office.approvals import OfficeApprovals
from agent_execution.office.config import OfficeSettings
from agent_execution.office.directory import OfficeDirectory
from agent_execution.office.dispatcher import OfficeDispatcher
from agent_execution.office.ledger import OfficeLedger
from agent_execution.office.runtimes.pythonai import PythonAIRuntime
from agent_execution.office.store import OfficeStore
from agent_execution.policy import execution_scope
from agent_execution.tasks import TaskStore
from permissions import set_current_role
from task_evaluator import TaskEvaluator,EvaluationResult


def main(mode):
    with execution_scope(source='api',identity='api:alice',project='default',session='directory'),set_current_role('admin'):
        ledger=OfficeLedger(OfficeDirectory(OfficeStore(TaskStore())),settings=OfficeSettings(enabled=True))
        service=OfficeApprovals(ledger,events=EventStore(),approvals=ApprovalStore())
        class Client:
            def __init__(self): self.chat=NS(completions=NS(create=self.create))
            def create(self,**kwargs):
                return NS(choices=[NS(message=ChatCompletionMessage(role='assistant',content='done'))],usage=None)
            def close(self): pass
        if mode=='consume':
            target=service.bridge.targets['approval']
            original=target.consume_managed
            def consume(*args):
                original(*args)
                os._exit(23)
            target.consume_managed=consume
        if mode=='effect':
            service.record_result=lambda *args:os._exit(23)
        if mode=='result':
            original=TaskStore.checkpoint
            def checkpoint(self,owner,key,token,phase,data,step=None):
                if phase=='tool_done': os._exit(23)
                return original(self,owner,key,token,phase,data,step)
            TaskStore.checkpoint=checkpoint
        dispatcher=OfficeDispatcher(ledger,approval_service=service,runtime_factory=lambda:PythonAIRuntime(client_factory=Client))
        with patch.object(TaskEvaluator,'evaluate',lambda *a,**k:EvaluationResult(True,1,'fixture')):
            return asyncio.run(dispatcher.run_once())


if __name__=='__main__': main(sys.argv[1])
