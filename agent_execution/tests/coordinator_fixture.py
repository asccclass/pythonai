"""Offline SDK model in a real coordinator worker process; no production bypass flags."""
from contextlib import closing
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace as NS
from unittest.mock import patch

from openai.types.chat import ChatCompletionMessage
from agent_execution.coordinator import Coordinator,scope_owner
from agent_execution.coordinator_worker import main
from agent_execution.policy import current_context
from agent_execution.budgets import budget_client
from task_evaluator import TaskEvaluator
from agent_execution import tasks
import server
import base


def fixture_runner(messages,**kwargs):
    store=Coordinator();node=store.get(scope_owner(),current_context().coordinator_node)
    marker=Path(node['payload']['storage'])/(node['id']+'.crashed')
    checkpoint=kwargs.get('resume_state')
    goal=node['goal'];persona=node['persona']
    if persona=='reviewer':
        original=store.get(scope_owner(),node['review_of']) if node['review_of'] else node
        approved='reject' not in original['goal']
        content=json.dumps({'approve':approved,'findings':[] if approved else [{'path':'result.txt','line':1,'message':'fixture finding'}]})
        values=[ChatCompletionMessage(role='assistant',content=content)]
    elif persona=='planner':values=[ChatCompletionMessage(role='assistant',content='Plan steps')]
    elif 'long' in goal:
        code="import os,time; from pathlib import Path; Path('worker.pid').write_text(str(os.getpid())); time.sleep(30)"
        call={'id':'effect','type':'function','function':{'name':'run_command','arguments':json.dumps({'command':['python','-c',code]})}}
        values=[ChatCompletionMessage(role='assistant',tool_calls=[call]),ChatCompletionMessage(role='assistant',content='done')]
    else:
        target='second.txt' if 'second' in goal else 'result.txt'
        call={'id':'effect','type':'function','function':{'name':'write_file','arguments':json.dumps({'path':target,'content':'result'})}}
        values=[ChatCompletionMessage(role='assistant',tool_calls=[call]),ChatCompletionMessage(role='assistant',content='done')]
    if checkpoint and persona=='coder' and 'long' not in goal:values=[ChatCompletionMessage(role='assistant',content='done')]
    iterator=iter(values)
    def completion(**params):
        offered={tool['function']['name'] for tool in params.get('tools',[])}
        if not offered<=current_context().allowed_tools:raise AssertionError('Model received tools outside the persona scope')
        return NS(choices=[NS(message=next(iterator))])
    client=NS(chat=NS(completions=NS(create=completion)))
    original_checkpoint=tasks.save_checkpoint
    def tracked_write(path,content):
        counter=Path(node['payload']['storage'])/(node['id']+'.effects.log')
        counter.write_text((counter.read_text() if counter.exists() else '')+'hit\n')
        return base.write_file(path,content)
    def save(phase,*args,**params):
        original_checkpoint(phase,*args,**params)
        trigger='tool_started' if 'crash-before' in goal else 'tool_done'
        if 'crash' in goal and phase==trigger and not marker.exists():
            marker.write_text('once');os._exit(23)
    with patch('server.get_client',return_value=budget_client(client)),patch.object(tasks,'save_checkpoint',save),patch.dict(base.TOOLS,{'write_file':tracked_write}):
        return server.run_agent(messages,**kwargs)


if __name__=='__main__':raise SystemExit(main(runner=fixture_runner,evaluator_factory=lambda workspace:TaskEvaluator(workspace)))
