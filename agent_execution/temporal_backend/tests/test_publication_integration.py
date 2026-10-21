"""Real SDK/Service streaming, finalization recovery and replay (controlled provider)."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import time
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

import pytest
pytest.importorskip('temporalio')
from temporalio import activity
from temporalio.worker import Worker, Replayer
from temporalio.exceptions import ApplicationError
from openai.types.chat import ChatCompletionMessage

from agent_runtime import AgentRuntime, invoke_runtime_agent, run_agent_turn
from agent_execution import execution_scope
from agent_execution.tasks import task_owner
from agent_execution.policy import current_context
from agent_execution.delivery import EventStore
from agent_execution.temporal_backend.store import Store
from agent_execution.temporal_backend.config import Settings
from agent_execution.temporal_backend.step_activities import ACTIVITIES, finalize
from agent_execution.temporal_backend.contracts import STEP_ACTIVITIES
from agent_execution.temporal_backend.workflows import AgentTaskWorkflowV1
from agent_execution.temporal_backend import publication as pub
from agent_execution.temporal_backend.tests.test_integration import configured, server
from memory import MemoryStore
from memory_worker import QueueOnlyMemoryWorker


def provider():
    class Stream:
        def __init__(self,chunks):
            self.chunks = iter(chunks)
            self.closed = False
        def __iter__(self):
            return self
        def __next__(self):
            if self.closed:
                raise StopIteration
            time.sleep(.06)  # Exercise the monitor thread while Activity authority is live.
            return next(self.chunks)
        def close(self):
            self.closed = True
    def chunk(content=None, calls=None, finish=None):
        return NS(choices=[NS(index=0,delta=NS(content=content,tool_calls=calls),finish_reason=finish)])
    streams = [
        [chunk(calls=[NS(index=0,id='read-one',function=NS(name='read_file',arguments='{"path":"note.txt"}'))],finish='tool_calls')],
        [chunk('private '),chunk('stream evidence 繁體中文'),chunk(finish='stop')]]
    def response(**kwargs):
        if kwargs.get('stream'):
            return Stream(streams.pop(0))
        return NS(choices=[NS(message=ChatCompletionMessage(role='assistant',content='{"success":true,"score":1.0,"feedback":"ok"}'))])
    create = Mock(side_effect=response)
    return NS(chat=NS(completions=NS(create=create))),create


@pytest.mark.parametrize('lost_final_reply',[False,True,'memory_ack','outbox_full'])
def test_stream_and_finalization_survive_retry_resume_and_event_ack(server,configured,monkeypatch,lost_final_reply):
    monkeypatch.setenv('AGENT_TEMPORAL_ENGINE','steps')
    roles = configured/'.system_generated'/'roles.json'
    roles.write_text(json.dumps({'websocket:alice':'admin'}))
    monkeypatch.setenv('ROLES_CONFIG',str(roles))
    path = configured/'.system_generated'/'events.db'
    monkeypatch.setenv('COMPANION_EVENT_DB',str(path))
    (configured/'note.txt').write_text('private stream evidence 繁體中文',encoding='utf-8')
    outbox = EventStore(path)
    owner = outbox.owner('alice','default','device')
    target = {'path':str(path.resolve()),'owner':owner,'ref':'integration-task'}
    raw,create = provider()
    if lost_final_reply=='outbox_full':
        monkeypatch.setenv('COMPANION_OUTBOX_LIMIT','1')
        outbox.append(owner,{'type':'notify','payload':{'text':'existing unacked event'}})
    received,lost = [],[]

    @activity.defn(name=STEP_ACTIVITIES['finalize'])
    def final_reply_lost(payload:dict)->dict:
        value = finalize(payload)
        if lost_final_reply is True and not lost:
            lost.append(1)
            raise ApplicationError('reply delivery unknown',type='FinalReplyUnknown',non_retryable=False)
        return value

    original_memory_final = MemoryStore.finalize_temporal
    def memory_ack_lost(self,*args,**kwargs):
        value = original_memory_final(self,*args,**kwargs)
        if lost_final_reply=='memory_ack' and not lost:
            lost.append(1)
            raise RuntimeError('memory commit succeeded but acknowledgement lost')
        return value
    monkeypatch.setattr(MemoryStore,'finalize_temporal',memory_ack_lost)

    async def exercise():
        with execution_scope(source='websocket',identity='websocket:alice',session='default:device',event_delivery=target):
            memory = MemoryStore()
            episode = memory.start_episode()
            memory.add_event(episode,'message',role='user',content='Read note.txt and report its content.')
            messages = [{'role':'user','content':'Read note.txt and report its content.'}]
            runtime = AgentRuntime(messages=messages,guard=Mock(),memory=memory,memory_searcher=None,
                skill_matcher=Mock(),memory_worker=QueueOnlyMemoryWorker(),memory_classifier_factory=None,
                semantic_extractor=None,procedure_matcher=None,run_agent=Mock(side_effect=AssertionError('no frontend model')),
                event_callback=lambda kind,payload:received.append((kind,payload)),profile_extractor=Mock())
            client = await server.client()
            store = Store()
            activities = [fn for fn in ACTIVITIES if fn is not finalize]+[final_reply_lost]
            with ThreadPoolExecutor(max_workers=2) as executor,patch('server.get_client',return_value=raw):
                async with Worker(client,task_queue=store.queue_for('steps'),workflows=[AgentTaskWorkflowV1],
                                  activities=activities,activity_executor=executor):
                    reply = await asyncio.wait_for(asyncio.to_thread(invoke_runtime_agent,runtime,messages,episode),60)
                    assert reply=='private stream evidence 繁體中文'
                    row = store.for_task(task_owner(current_context()),'integration-task')
                    # Actual Worker path publishes to EventStore without frontend callback/socket objects.
                    await asyncio.to_thread(pub.reconcile,store,Settings.load(),{row['queue']})
                    seq,floor,frames = outbox.snapshot(owner)
                    if lost_final_reply=='outbox_full':
                        assert frames[0]['type']=='notify'
                        outbox.ack(owner,seq)
                        recovered = []
                        for _ in range(30):
                            await asyncio.to_thread(pub.reconcile,store,Settings.load(),{row['queue']})
                            seq,floor,frames = outbox.snapshot(owner)
                            recovered.extend(frames)
                            outbox.ack(owner,seq)
                            if any(frame['type']=='chat.end' for frame in frames):
                                break
                        frames = recovered
                    assert ''.join(frame['payload']['text'] for frame in frames if frame['type']=='chat.delta')==reply
                    assert len([frame for frame in frames if frame['type']=='chat.end'])==1
                    assert all(frame['ref']=='integration-task' for frame in frames)
                    outbox.ack(owner,seq)
                    await asyncio.to_thread(pub.reconcile,store,Settings.load(),{row['queue']})
                    # Duplicate whole frontend turn takes the persisted result before episode/profile preparation.
                    for _ in range(2):
                        result = await asyncio.to_thread(run_agent_turn,'Read note.txt and report its content.',runtime)
                        assert result.reply==reply and result.episode_id==episode
                        assert [(item.get('role'),item.get('content')) for item in runtime.messages[-2:]] != [('assistant',reply),('assistant',reply)]
                    assert outbox.snapshot(owner)[:2]==(seq,seq)
                    assert not outbox.snapshot(owner)[2]
                    history = await client.get_workflow_handle(row['workflow_id']).fetch_history()
                    assert 'private stream evidence' not in history.to_json()
                    await Replayer(workflows=[AgentTaskWorkflowV1]).replay_workflow(history)
            assert create.call_count==3
            assert any(kind=='model.delta' and '繁體中文' in payload['text'] for kind,payload in received)
            runtime.run_agent.assert_not_called()
            runtime.profile_extractor.extract.assert_not_called()
            assert len(memory.memory_jobs())==1
            assert len([event for event in memory.episode_events(episode) if event['role']=='assistant'])==1
            with closing(memory.connect()) as conn:
                assert conn.execute('SELECT count(*) FROM episodes').fetchone()[0]==1
            with closing(store.tasks.connect()) as conn:
                assert conn.execute('SELECT requests FROM task_budgets').fetchone()[0]==3
                assert conn.execute('SELECT state FROM temporal_finalizations').fetchone()[0]=='completed'
    asyncio.run(exercise())
