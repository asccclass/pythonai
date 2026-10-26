"""Controlled coordinator runner; every Python child uses this project's runtime."""
import sys
import traceback
from pathlib import Path
from unittest.mock import patch
import base
from agent_execution.policy import current_context,execution_scope
from agent_execution.coordinator_worker import main,execute_node
from agent_execution.tests.coordinator_fixture import fixture_runner
from task_evaluator import TaskEvaluator


def runner(messages,**kwargs):
    original=base.TOOLS['run_command']
    def command(command,*args,**params):
        if isinstance(command,list) and command and command[0]=='python':
            command=[sys.executable,*command[1:]]
            params['trusted_asset_roots']=[Path(sys.executable).parent]
        return original(command,*args,**params)
    with execution_scope(trusted_assets=(*current_context().trusted_assets,Path(sys.executable).parent)),patch.dict(base.TOOLS,{'run_command':command}):
        return fixture_runner(messages,**kwargs)


def diagnostic(store,node,*args,**kwargs):
    try:return execute_node(store,node,*args,**kwargs)
    except Exception:
        (Path(node['payload']['storage'])/(node['id']+'.error.log')).write_text(traceback.format_exc(),encoding='utf-8')
        raise


if __name__=='__main__':
    with patch('agent_execution.coordinator_worker.execute_node',diagnostic):
        raise SystemExit(main(runner=runner,evaluator_factory=lambda workspace:TaskEvaluator(workspace)))
