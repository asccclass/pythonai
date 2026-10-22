"""Native WebSocket reliable event pump, ACK replay and disconnect behavior."""
import asyncio
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace as NS
from unittest.mock import patch

from fastapi.testclient import TestClient
import pytest

from agent_execution.delivery import EventStore, CapacityError
from websocket_server import app, hub, Session, reset_hub_for_tests


@pytest.fixture(autouse=True)
def isolated(tmp_path,monkeypatch):
    reset_hub_for_tests()
    monkeypatch.setenv('COMPANION_WS_TOKENS','token:alice')
    monkeypatch.setenv('AGENT_TASK_DB',str(tmp_path/'tasks.db'))
    monkeypatch.setenv('AGENT_TENANT_ROOT',str(tmp_path/'tenants'))
    monkeypatch.setenv('AGENT_WORKSPACE_ROOT',str(tmp_path))
    yield
    reset_hub_for_tests()


def hello(ws,last=0):
    ws.send_json({'type':'hello','payload':{'token':'token','device_id':'device','project':'default','last_seq':last}})
    result = ws.receive_json()
    assert result['type']=='welcome'
    return result


def wait_for(check):
    deadline = time.monotonic()+5
    while not check():
        assert time.monotonic()<deadline
        time.sleep(.01)


def test_worker_outbox_is_pushed_in_sequence_and_ack_prevents_replay():
    with TestClient(app) as client:
        with client.websocket_connect('/ws/v1') as ws:
            hello(ws)
            session = hub.sessions[('alice','default','device')]
            session.durable_task = True
            session.cancel_chat = threading.Event()
            store,owner = session.event_store,session.owner
            store.accept_input(owner,'task','input')
            delta = {'v':1,'type':'chat.delta','ref':'task','payload':{'task_id':'task','temporal_event_id':'a'*64,'text':'繁體中文'}}
            end = {'v':1,'type':'chat.end','ref':'task','payload':{'task_id':'task','temporal_event_id':'b'*64,'text':'final','suspended':False,'failed':False}}
            store.append(owner,delta,event_key='a'*64)
            store.append(owner,end,event_key='b'*64)
            frames = [ws.receive_json(),ws.receive_json()]
            assert [frame['type'] for frame in frames]==['chat.delta','chat.end']
            assert [frame['seq'] for frame in frames]==[1,2]
            assert store.accept_input(owner,'task','input')=='completed'
            ws.send_json({'type':'ack','payload':{'seq':2}})
            ws.send_json({'type':'ping','id':'before-close'})
            assert ws.receive_json()['type']=='pong'
        wait_for(lambda:session.ws is None)
        assert not session.cancel_chat.is_set()
        with client.websocket_connect('/ws/v1') as ws:
            welcome = hello(ws,2)
            assert welcome['payload']['resumed']
            assert store.append(owner,end,event_key='b'*64) is None
            ws.send_json({'type':'ping','id':'after-replay'})
            assert ws.receive_json()['type']=='pong'


@pytest.mark.parametrize('durable',[False,True])
def test_send_failure_preserves_history_and_only_cancels_legacy(tmp_path,durable):
    class Broken:
        async def send_text(self,value):
            raise OSError('offline')
    session = Session(device_id='device',device_type='desktop',user_id='alice',project='default')
    session.event_store = EventStore(tmp_path/'events.db')
    session.ws = Broken()
    session.durable_task = durable
    session.cancel_chat = threading.Event()
    asyncio.run(session.send('chat.end',{'text':'result'}))
    assert session.ws is None
    assert session.cancel_chat.is_set() is (not durable)
    assert len(session.event_store.snapshot(session.owner)[2])==1


def test_durable_key_capacity_rolls_back_sequence(tmp_path,monkeypatch):
    monkeypatch.setenv('COMPANION_EVENT_KEY_LIMIT','1')
    store = EventStore(tmp_path/'keys.db')
    store.append('owner',{'type':'chat.end','payload':{'text':'one'}},event_key='c'*64)
    store.ack('owner',1)
    with pytest.raises(CapacityError):
        store.append('owner',{'type':'chat.end','payload':{'text':'two'}},event_key='d'*64)
    assert store.snapshot('owner')[:2]==(1,1)


@pytest.mark.parametrize('excluded',[False,True])
def test_disconnect_detaches_temporal_brain_and_duplicate_input_does_not_execute(monkeypatch,excluded):
    monkeypatch.setenv('AGENT_EXECUTION_BACKEND','temporal')
    if excluded:
        monkeypatch.setenv('AGENT_TEMPORAL_TRIAL_OWNERS','[]')
    else:
        monkeypatch.delenv('AGENT_TEMPORAL_TRIAL_OWNERS',raising=False)
    from agent_runtime import AgentTurnResult
    entered,release = threading.Event(),threading.Event()
    calls = []
    def controlled(text,runtime,**kwargs):
        calls.append(text)
        entered.set()
        assert release.wait(5)
        return AgentTurnResult('durable result',None)
    with patch('agent_runtime.run_agent_turn',side_effect=controlled),patch('agent_execution.tasks.dispatch_task_input',return_value=None):
        with TestClient(app) as client:
            try:
                with client.websocket_connect('/ws/v1') as ws:
                    hello(ws)
                    session = hub.sessions[('alice','default','device')]
                    session.runtime = NS(check_cancelled=None,event_callback=None)
                    ws.send_json({'type':'chat.send','id':'stable-request','payload':{'text':'controlled input'}})
                    assert ws.receive_json()['type']=='chat.start'
                    assert entered.wait(3)
                wait_for(lambda:session.ws is None)
                if excluded:
                    wait_for(session.cancel_chat.is_set)
                else:
                    assert not session.cancel_chat.is_set()
            finally:
                release.set()
            wait_for(lambda:session.current_chat_task.done())
            with client.websocket_connect('/ws/v1') as ws:
                hello(ws)
                if excluded:
                    for frame in session.event_store.snapshot(session.owner)[2]:
                        assert ws.receive_json()['seq']==frame['seq']
                ws.send_json({'type':'chat.send','id':'stable-request','payload':{'text':'controlled input'}})
                result = ws.receive_json()
                assert result['type']=='notify' and result['payload']['request_id']=='stable-request'
    assert calls==['controlled input']


@pytest.mark.parametrize('durable',[False,True])
def test_cancelled_subscriber_does_not_invent_temporal_final(tmp_path,durable):
    from websocket_server import handle_chat
    received = []
    class Socket:
        async def send_text(self,value):
            received.append(json.loads(value))
    session = Session(device_id='device',device_type='desktop',user_id='alice',project='default')
    session.ws = Socket()
    session.event_store = EventStore(tmp_path/'subscriber.db')
    session.durable_task = durable
    async def exercise():
        entered = asyncio.Event()
        async def wait_forever(*args):
            entered.set()
            await asyncio.Future()
            yield 'end',{}
        with patch('websocket_server.brain_reply_stream',wait_forever):
            task = asyncio.create_task(handle_chat(session,'input','task'))
            await entered.wait()
            task.cancel()
            await task
    asyncio.run(exercise())
    assert sum(item['type']=='chat.end' for item in received)==int(not durable)
