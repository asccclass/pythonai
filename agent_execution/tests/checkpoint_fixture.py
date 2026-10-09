"""Subprocess fixture: crash at a committed execution boundary without cleanup."""
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace as NS
from unittest.mock import patch

import base
import server
from agent_execution import execution_scope
from agent_execution import tasks
from task_evaluator import EvaluationResult


def append_artifact(path, content):
    target = base.resolve_workspace_path(path)
    target.write_text((target.read_text() if target.exists() else '')+content,encoding='utf-8')
    return 'written'


def response(content=None,calls=None):
    return NS(choices=[NS(message=NS(content=content,tool_calls=calls))])


if __name__ == '__main__':
    root, phase = Path(sys.argv[1]), sys.argv[2]
    os.environ['AGENT_TASK_DB'] = str(root/'tasks.db')
    original = tasks.save_checkpoint
    def crash(when,*args,**kwargs):
        original(when,*args,**kwargs)
        if when == phase:
            os._exit(23)
    calls = [NS(id='write-1',function=NS(name='write_file',arguments=json.dumps({'path':'artifact.txt','content':'hit'})))]
    values = iter([response(calls=calls),response('done')])
    client = NS(chat=NS(completions=NS(create=lambda **kwargs:next(values))))
    evaluator = NS(capture_state=lambda:{'files':{}},evaluate=lambda **kwargs:EvaluationResult(True,1,'verified fixture'))
    messages=[{'role':'user','content':'write fixture'}]
    with patch.object(tasks,'save_checkpoint',crash),patch('server.get_client',return_value=client), \
         patch.dict(base.TOOLS,{'write_file':append_artifact}), \
         execution_scope(task_id='crash',session='session',workspace=root):
        tasks.durable_invoke(lambda saved:server.run_agent(messages,task_evaluator=evaluator,resume_state=saved),messages,99)
