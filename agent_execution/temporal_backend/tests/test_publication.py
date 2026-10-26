"""Task journal, Memory DB atomicity and frontend replay contracts."""
from contextlib import closing
import json
from unittest.mock import Mock, patch

import pytest

from agent_execution import execution_scope
from agent_execution.delivery import EventStore, CapacityError
from agent_execution.temporal_backend import publication as pub
from agent_execution.temporal_backend.runtime_state import prepare_profile
from agent_execution.temporal_backend.store import canonical
from agent_execution.temporal_backend.tests.test_step_store import task
from memory import MemoryStore


@pytest.fixture
def published(task, tmp_path, monkeypatch):
    store,steps,row,ledger,cursor = task
    path = tmp_path/'memory.db'
    monkeypatch.setenv('MEMORY_DB_PATH',str(path))
    memory = MemoryStore(path)
    episode = memory.start_episode()
    context = json.loads(row['context'])
    context['publication'] = {'version':1,'episode':episode,'memory_db':str(path.resolve()),'review':True,'delivery':None}
    with closing(store.tasks.connect()) as conn, conn:
        conn.execute('UPDATE temporal_submissions SET context=? WHERE id=?',(canonical(context),row['id']))
        conn.execute('UPDATE tasks SET episode=? WHERE owner=? AND id=?',(episode,row['owner'],row['task_id']))
        pub.install(conn)
    return store,store.get(row['id']),memory,episode


def complete(published, text='private final evidence'):
    store,row,memory,episode = published
    with closing(store.tasks.connect()) as conn, conn:
        conn.execute("UPDATE tasks SET state='completed',reply=? WHERE owner=? AND id=?",(text,row['owner'],row['task_id']))
    store.finish(row['id'],row['fence'],'completed',{'reply':text,'messages':[{'role':'assistant','content':text}]})


def test_journal_order_idempotency_and_modified_content(published):
    store,row,memory,episode = published
    pub.append(store,row,'model:1','model.start',{'iteration':1})
    pub.append(store,row,'model:2','model.delta',{'text':'private delta'})
    pub.append(store,row,'model:2','model.delta',{'text':'private delta'})
    items = pub.events(store,row)
    assert [item['seq'] for item in items]==[1,2]
    assert json.loads(items[1]['payload'])['task_id']==row['task_id']
    with pytest.raises(ValueError):
        pub.append(store,row,'model:2','model.delta',{'text':'different'})
    assert len(pub.events(store,row,after=1))==1


def test_each_approval_boundary_is_durable_without_a_frontend_subscriber(published):
    from agent_execution.temporal_backend.approval_gate import ApprovalGate
    store,row,_,_=published
    ApprovalGate(store)
    for revision in (1,2):
        key=str(revision)*32
        wait={'id':key,'revision':revision,'operation_id':'tool:'+str(revision)}
        payload={'tool':'write_file','arguments':{'path':'note.md','content':'private '+str(revision)}}
        with closing(store.tasks.connect()) as conn,conn:
            conn.execute("INSERT INTO temporal_approval_waits VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (row['id'],revision,wait['operation_id'],'call-'+str(revision),'write_file',
                 canonical(payload),'fingerprint','binding','action',9999999999,key,'pending'))
            conn.execute("UPDATE tasks SET state='suspended',phase='waiting_approval',checkpoint=?,reply=? WHERE owner=? AND id=?",
                (canonical({'approval_wait':wait,'suspended_tool_call_id':'call-'+str(revision)}),
                 'approval '+str(revision),row['owner'],row['task_id']))
        pub.waiting(store,row)
        pub.waiting(store,row)
    events=pub.events(store,row)
    assert [event['kind'] for event in events]==['approval.required','end']*2
    notifications=[json.loads(e['payload']) for e in events if e['kind']=='approval.required']
    assert [event['approval_id'] for event in notifications]==['1'*32,'2'*32]
    assert notifications[1]['arguments']=={'path':'note.md','content':'private 2'}
    assert all(event['auto_resume'] for event in notifications)


def test_missing_approval_intent_cannot_publish_an_action(published):
    from agent_execution.temporal_backend.approval_gate import ApprovalGate
    store,row,_,_=published
    ApprovalGate(store)
    with closing(store.tasks.connect()) as conn,conn:
        conn.execute("UPDATE tasks SET state='suspended',phase='waiting_approval',checkpoint=? WHERE owner=? AND id=?",
            (canonical({'approval_wait':{'id':'f'*32,'revision':1}}),row['owner'],row['task_id']))
    with pytest.raises(PermissionError,match='durable intent'):
        pub.waiting(store,row)
    assert not pub.events(store,row)


@pytest.mark.parametrize('reason',['cancel','stale_fence','other_owner'])
def test_event_owner_and_execution_fences(published,reason):
    store,row,memory,episode = published
    if reason=='cancel':
        store.tasks.cancel(row['owner'],row['task_id'])
    elif reason=='stale_fence':
        row = {**row,'fence':'not-the-current-fence'}
    else:
        with execution_scope(identity='websocket:other',source='websocket'):
            with pytest.raises(PermissionError):
                pub.events(store,row)
        return
    with pytest.raises((PermissionError,InterruptedError)):
        pub.append(store,row,'late','model.delta',{'text':'must never publish'})


@pytest.mark.parametrize('setting,value',[('AGENT_TEMPORAL_EVENT_LIMIT','1'),('AGENT_TEMPORAL_EVENT_BYTES','1')])
def test_journal_capacity_is_bounded_and_atomic(published,monkeypatch,setting,value):
    store,row,memory,episode = published
    monkeypatch.setenv(setting,value)
    if setting.endswith('LIMIT'):
        pub.append(store,row,'first','model.start',{})
    with pytest.raises(CapacityError):
        pub.append(store,row,'second','model.delta',{'text':'capacity'})
    assert len(pub.events(store,row))==int(setting.endswith('LIMIT'))


def test_coalesced_unicode_deltas_are_lossless(published):
    store,row,memory,episode = published
    sink = pub.writer(store,row,'model:1')
    text = '繁體中文'*2000
    for part in [text[:3],text[3:1000],text[1000:]]:
        sink('model.delta',{'text':part})
    sink.flush()
    items = pub.events(store,row)
    assert ''.join(json.loads(item['payload'])['text'] for item in items)==text
    assert len(items)<10


def test_finalization_is_atomic_and_idempotent_across_stores(published):
    store,row,memory,episode = published
    complete(published)
    for _ in range(3):
        pub.finalize(store,store.get(row['id']))
    events = memory.episode_events(episode)
    assert len([event for event in events if event['role']=='assistant'])==1
    assert len(memory.memory_jobs())==1
    assert memory.get_episode(episode)['status']=='verified_completed'
    assert len([item for item in pub.events(store,row) if item['kind']=='end'])==1
    with closing(store.tasks.connect()) as conn:
        assert conn.execute('SELECT state FROM temporal_finalizations').fetchone()[0]=='completed'


def test_crash_between_memory_commit_and_task_ack_does_not_repeat(published):
    store,row,memory,episode = published
    original = MemoryStore.finalize_temporal
    def lost_ack(self,*args,**kwargs):
        original(self,*args,**kwargs)
        raise RuntimeError('worker lost after memory transaction')
    with patch.object(MemoryStore,'finalize_temporal',lost_ack),pytest.raises(RuntimeError):
        complete(published)
    assert store.get(row['id'])['state']=='completed'
    assert len(memory.memory_jobs())==1
    pub.finalize(store,store.get(row['id']))
    assert len(memory.memory_jobs())==1
    assert len(memory.episode_events(episode))==1


def test_memory_finalization_transaction_rolls_back_before_review_intent(published):
    store,row,memory,episode = published
    with closing(memory.connect()) as conn,conn:
        conn.execute("CREATE TRIGGER fail_job BEFORE INSERT ON memory_jobs BEGIN SELECT RAISE(ABORT,'injected'); END")
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        complete(published)
    assert not memory.episode_events(episode)
    assert memory.get_episode(episode)['status']=='running'
    with closing(memory.connect()) as conn,conn:
        conn.execute('DROP TRIGGER fail_job')
    pub.finalize(store,store.get(row['id']))
    assert len(memory.memory_jobs())==1


@pytest.mark.parametrize('reason',['deleted','relocated'])
def test_memory_finalization_fails_closed(published,monkeypatch,reason):
    store,row,memory,episode = published
    if reason=='deleted':
        with closing(memory.connect()) as conn,conn:
            conn.execute("UPDATE episodes SET status='deleted' WHERE id=?",(episode,))
    else:
        monkeypatch.setenv('MEMORY_DB_PATH',str(memory.db_path.parent/'other.db'))
    with pytest.raises(PermissionError):
        complete(published)
    assert not memory.episode_events(episode)
    assert not memory.memory_jobs()


def test_eventstore_key_survives_ack_and_rejects_conflict(tmp_path):
    store = EventStore(tmp_path/'events.db')
    key = 'a'*64
    message = {'type':'chat.end','ref':'task','payload':{'text':'one'}}
    first = store.append('owner',message,event_key=key)
    assert store.append('owner',{**message,'id':'different'},event_key=key)['seq']==first['seq']
    store.ack('owner',first['seq'])
    assert store.append('owner',message,event_key=key) is None
    with pytest.raises(ValueError):
        store.append('owner',{**message,'payload':{'text':'other'}},event_key=key)
    assert store.snapshot('owner')[:2]==(1,1)


def test_profile_same_task_input_is_not_extracted_twice(task):
    with patch('agent_execution.user_profile.prepare_turn',return_value=(None,'saved')) as extract:
        assert prepare_profile('stable fact',Mock())==(None,'saved')
        assert prepare_profile('stable fact',Mock())==(None,'saved')
        assert prepare_profile('new fact',Mock())==(None,'saved')
    assert extract.call_count==2


def test_unknown_profile_result_never_remints_provider_call(task):
    with patch('agent_execution.user_profile.prepare_turn',side_effect=BaseException('lost')):
        with pytest.raises(BaseException):
            prepare_profile('private fact',Mock())
    with patch('agent_execution.user_profile.prepare_turn') as extract:
        reply,status = prepare_profile('private fact',Mock())
    assert reply is None and '未自動重試' in status
    extract.assert_not_called()


@pytest.mark.parametrize('state',['cancelled','uncertain'])
def test_fenced_terminal_status_never_records_late_result_or_review(published,state):
    store,row,memory,episode = published
    pub.append(store,row,'early','model.delta',{'text':'early draft'})
    if state=='cancelled':
        store.tasks.cancel(row['owner'],row['task_id'])
    else:
        store.workflow_failed(row['id'],row['owner'],'unknown effect')
    pub.finalize(store,store.get(row['id']))
    payload = pub.end_payload(store,row)
    assert payload['task_state']==state and not payload['suspended']
    assert payload['interrupted']==(state=='cancelled')
    assert 'early draft' not in payload['text']
    assert not memory.memory_jobs()


@pytest.mark.parametrize('field,value',[('version',True),('version',2),('episode',True),('review','yes'),('memory_db','relative.db'),('delivery',{'path':'relative'})])
def test_malformed_publication_binding_fails_closed(published,field,value):
    store,row,memory,episode = published
    context = json.loads(row['context'])
    context['publication'][field]=value
    with pytest.raises(ValueError):
        pub.binding({**row,'context':canonical(context)})


def test_profile_deadline_does_not_start_extraction(task):
    with execution_scope(check_cancelled=lambda:True),patch('agent_execution.user_profile.prepare_turn') as extract:
        from agent_execution.budgets import BudgetExhausted
        with pytest.raises(BudgetExhausted):
            prepare_profile('private input',Mock())
    extract.assert_not_called()


def test_background_memory_poller_picks_up_worker_created_final_job(published):
    import threading
    from memory_worker import MemoryReviewWorker
    store,row,memory,episode = published
    processed = threading.Event()
    count = []
    def process(self,item):
        _,job,source,*rest = item
        if source.claim_memory_job(job):
            count.append(job)
            source.complete_memory_job(job)
            processed.set()
    with patch.object(MemoryReviewWorker,'_process_item',process):
        worker = MemoryReviewWorker(async_mode=True)
        try:
            worker.enqueue_pending_reviews(memory,None,None,None)
            complete(published)
            worker._last_review_poll = -100
            assert processed.wait(3)
            worker.enqueue_pending_reviews(memory,None,None,None)
            worker.join()
        finally:
            worker.stop()
    assert len(count)==1


@pytest.mark.parametrize('explicit',[False,True])
def test_client_wait_shutdown_detaches_but_explicit_cancel_fences(published,monkeypatch,explicit):
    pytest.importorskip('temporalio')
    import asyncio
    from concurrent.futures import CancelledError
    from unittest.mock import AsyncMock
    from agent_execution.temporal_backend import backend,client
    store,row,memory,episode = published
    entered = None
    requested = [False]
    handle = Mock()
    async def result():
        entered.set()
        await asyncio.Future()
    handle.result = result
    handle.cancel = AsyncMock()
    sdk = Mock(get_workflow_handle=Mock(return_value=handle))
    monkeypatch.setattr(client,'connect',AsyncMock(return_value=sdk))
    monkeypatch.setattr(client,'dispatch',AsyncMock(return_value=handle))
    class ClosingBridge:
        def call(self,coroutine,*args):
            async def close_waiter():
                nonlocal entered
                entered = asyncio.Event()
                task = asyncio.create_task(coroutine)
                await entered.wait()
                requested[0] = explicit
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            asyncio.run(close_waiter())
            raise CancelledError()
    monkeypatch.setattr(client,'bridge',lambda:ClosingBridge())
    with execution_scope(check_cancelled=lambda:requested[0]):
        reply = backend.invoke(store,json.loads(row['input']))
    assert reply.startswith('Error: Temporal')
    assert store.get(row['id'])['state']==('cancelled' if explicit else 'running')
    assert handle.cancel.await_count==int(explicit)


def test_enabling_temporal_does_not_add_profile_guard_to_legacy_task(tmp_path,monkeypatch):
    from agent_execution.tasks import TaskStore,task_owner
    from agent_execution.policy import current_context
    monkeypatch.setenv('AGENT_TASK_DB',str(tmp_path/'legacy.db'))
    monkeypatch.setenv('AGENT_EXECUTION_BACKEND','temporal')
    with execution_scope(source='local',identity='local',task_id='legacy-profile'):
        tasks = TaskStore()
        tasks.claim(task_owner(current_context()),'legacy-profile','goal',[{'role':'user','content':'goal'}])
        with patch('agent_execution.user_profile.prepare_turn',return_value=(None,'legacy')) as extract:
            prepare_profile('same text',Mock())
            prepare_profile('same text',Mock())
        assert extract.call_count==2
        with closing(tasks.connect()) as conn:
            assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='temporal_profile_inputs'").fetchone()
