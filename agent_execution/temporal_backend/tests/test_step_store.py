from contextlib import closing
import asyncio
import copy
import json
import sqlite3
from unittest.mock import Mock

import pytest

from agent_execution import execution_scope
from agent_execution.policy import current_context
from agent_execution.tasks import task_owner
from agent_execution.temporal_backend.config import Settings
from agent_execution.temporal_backend.context import execution_context
from agent_execution.temporal_backend.contracts import READ_TOOLS, STEP_TOOLS, STEP_WORKFLOW_NAME, WORKFLOW_NAME, validate_cursor, validate_snapshot
from agent_execution.temporal_backend.ledger import OperationUncertain
from agent_execution.temporal_backend.step_store import StepStore
from agent_execution.temporal_backend.store import Store, canonical


@pytest.fixture
def task(tmp_path, monkeypatch, request):
    engine = getattr(request, 'param', 'steps')
    for key, value in {'AGENT_TASK_DB': str(tmp_path/'tasks.db'), 'AGENT_TENANT_ROOT': str(tmp_path/'tenants'),
        'AGENT_WORKSPACE_ROOT': str(tmp_path), 'AGENT_TRACE_ENABLED': '0', 'AGENT_TASK_WORKTREE': '0',
        'AGENT_TEMPORAL_ENGINE': engine, 'AGENT_EXECUTION_BACKEND': 'temporal',
        'AGENT_APPROVAL_DB': str(tmp_path/'approvals.db')}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv('AGENT_PROJECT_WORKSPACES', raising=False)
    with execution_scope(source='local', identity='local', project='default', session='step', task_id='step-task', workspace=tmp_path):
        store = Store()
        context = current_context()
        owner = task_owner(context)
        store.bind(owner, context.task_id, 'temporal')
        scope = {name: getattr(context, name) for name in ('source', 'identity', 'session', 'conversation', 'project')}
        scope.update(workspace=str(tmp_path), allowed_tools=sorted(STEP_TOOLS if engine in {'human', 'approval'} else READ_TOOLS))
        if engine == 'approval':
            from agent_execution.approvals import ApprovalStore
            scope['approval_db'] = str(ApprovalStore().path.resolve())
        row = store.submit(owner, context.task_id, [{'role':'user','content':'private step goal'}], scope, 'admin', Settings.load())
        steps = StepStore(store)
        row = steps.bootstrap_claim(row['id'], Settings.load())
        with execution_context(store, row) as ledger:
            cursor = steps.bootstrap(row, ledger)
            yield store, steps, row, ledger, cursor


def update(task, phase='step_ready', state='pending', next_kind='tool', messages=None):
    store, steps, row, ledger, cursor = task
    saved = {'messages': messages or [{'role':'user','content':'private step goal'}],
             'iteration':1, 'iteration_limit':4, 'eval_retries':0, 'next_kind':next_kind}
    with closing(store.tasks.connect()) as conn, conn:
        conn.execute('UPDATE tasks SET state=?,phase=?,checkpoint=? WHERE owner=? AND id=?',
                     (state,phase,canonical(saved),row['owner'],row['task_id']))
    return saved


def test_protocol_and_queue_are_pinned_not_selected_by_later_environment(task, monkeypatch):
    store,steps,row,ledger,cursor=task
    assert store.workflow_type(row)==STEP_WORKFLOW_NAME
    assert row['queue']==store.queue_for('steps') and row['queue']!=store.queue
    assert 'ask_user' not in current_context().allowed_tools
    monkeypatch.setenv('AGENT_TEMPORAL_ENGINE','bridge')
    old=store.submit(row['owner'],row['task_id'],[{'role':'user','content':'private step goal'}],{},'viewer',Settings.load())
    assert old['id']==row['id'] and store.workflow_type(old)==STEP_WORKFLOW_NAME
    with closing(store.tasks.connect()) as conn,conn:
        conn.execute('DELETE FROM temporal_protocols WHERE submission_id=?',(row['id'],))
    with pytest.raises(ValueError): store.workflow_type(row)
    with closing(store.tasks.connect()) as conn,conn:
        conn.execute('UPDATE temporal_submissions SET queue=? WHERE id=?',(store.queue,row['id']))
    assert store.workflow_type(store.get(row['id']))==WORKFLOW_NAME


def test_bootstrap_and_projection_are_idempotent_and_reference_only(task):
    store,steps,row,ledger,cursor=task
    assert steps.bootstrap(row,ledger)==cursor
    assert 'private step goal' not in json.dumps(cursor)
    fence,cached=steps.claim(cursor,ledger)
    assert cached is None
    update(task)
    following=steps.commit(cursor,fence,ledger,'tool')
    assert following['revision']==1 and following['next_kind']=='tool'
    assert steps.claim(cursor,ledger)==(None,following)
    assert steps.commit(cursor,fence,ledger,'tool')==following
    assert steps.recover(cursor,ledger)==following
    with closing(store.tasks.connect()) as conn:
        assert conn.execute('SELECT COUNT(*) FROM temporal_artifacts').fetchone()[0]==2


def test_running_step_cannot_be_claimed_twice_or_completed_with_stale_fence(task):
    store,steps,row,ledger,cursor=task
    steps.claim(cursor,ledger)
    with pytest.raises(OperationUncertain): steps.claim(cursor,ledger)
    with pytest.raises(PermissionError): steps.commit(cursor,'wrong-fence',ledger,'tool')


def test_projection_failure_rolls_back_artifact_and_recovers_without_executor(task):
    store,steps,row,ledger,cursor=task
    fence,_=steps.claim(cursor,ledger)
    update(task)
    with closing(store.tasks.connect()) as conn,conn:
        conn.execute("CREATE TRIGGER injected_failure BEFORE INSERT ON temporal_step_runs WHEN NEW.revision=1 BEGIN SELECT RAISE(ABORT,'projection failure'); END")
    with pytest.raises(sqlite3.IntegrityError): steps.commit(cursor,fence,ledger,'tool')
    with closing(store.tasks.connect()) as conn,conn:
        assert conn.execute('SELECT COUNT(*) FROM temporal_artifacts').fetchone()[0]==1
        conn.execute('DROP TRIGGER injected_failure')
    result=steps.recover(cursor,ledger)
    assert result['revision']==1 and result['next_kind']=='tool'
    assert not steps.current(cursor,fence)


@pytest.mark.parametrize('state',[None,'prepared','running','uncertain','completed'])
def test_recovery_only_releases_proven_pre_model_execution(task,state):
    store,steps,row,ledger,cursor=task
    fence,_=steps.claim(cursor,ledger)
    saved=update(task,phase='model_pending',state='running')
    saved.update(model_request={'model':'test','tools':[]},model_attempt=1)
    with closing(store.tasks.connect()) as conn,conn:
        conn.execute('UPDATE tasks SET checkpoint=? WHERE owner=? AND id=?',(canonical(saved),row['owner'],row['task_id']))
    if state:
        ledger.prepare('model:1:attempt:1','model',{})
        if state!='prepared':
            op=ledger.claim('model:1:attempt:1')
            if state=='uncertain': ledger.uncertain(op['step_id'],op['fence'])
            elif state=='completed': ledger.complete(op['step_id'],op['fence'],{}, {})
    if state in {None,'prepared'}:
        assert steps.recover(cursor,ledger)==cursor
        assert not steps.current(cursor,fence)
        assert store.tasks.get(row['owner'],row['task_id'])['state']=='pending'
    else:
        with pytest.raises(OperationUncertain): steps.recover(cursor,ledger)


@pytest.mark.parametrize('kind', ['model', 'tool'])
def test_unconfirmed_preparation_hooks_are_not_replayed(task, kind):
    store, steps, row, ledger, cursor = task
    steps.claim(cursor, ledger)
    update(task, phase='step_preparing', state='running', next_kind=kind)
    with pytest.raises(OperationUncertain, match='Preparation hooks'):
        steps.recover(cursor, ledger)
    assert store.tasks.get(row['owner'], row['task_id'])['state'] == 'running'


def test_tool_started_is_not_replayed_even_if_raw_result_is_saved(task):
    store,steps,row,ledger,cursor=task
    steps.claim(cursor,ledger)
    update(task,phase='tool_started',state='running')
    ledger.prepare('model:1:tool:read','tool',{})
    op=ledger.claim('model:1:tool:read')
    ledger.complete(op['step_id'],op['fence'],'raw result',{})
    with pytest.raises(OperationUncertain): steps.recover(cursor,ledger)


@pytest.mark.parametrize('phase,expected', [('step_ready','tool'),('model_received','evaluation'),
    ('tool_done','model'),('evaluation_done','evaluation')])
def test_complete_boundaries_advance_cursor_without_repeating_io(task,phase,expected):
    store,steps,row,ledger,cursor=task
    steps.claim(cursor,ledger)
    update(task,phase=phase,state='running',messages=[{'role':'user','content':'goal'},{'role':'assistant','content':'done'}])
    result=steps.recover(cursor,ledger)
    assert result['revision']==1 and result['next_kind']==expected


def test_terminal_reply_can_finalize_after_activity_delivery_was_lost(task):
    store,steps,row,ledger,cursor=task
    steps.claim(cursor,ledger)
    update(task,phase='evaluation_done',state='completed')
    with closing(store.tasks.connect()) as conn,conn:
        conn.execute("UPDATE tasks SET reply='done' WHERE owner=? AND id=?",(row['owner'],row['task_id']))
    result=steps.recover(cursor,ledger)
    assert result['next_kind']=='finalize'
    assert ledger.load(result['state_ref'])['reply']=='done'


def test_cancelled_parent_and_deleted_artifact_block_projection(task):
    store,steps,row,ledger,cursor=task
    fence,_=steps.claim(cursor,ledger)
    store.cancel(row['id'],row['owner'])
    with pytest.raises(PermissionError): steps.commit(cursor,fence,ledger,'tool')
    with pytest.raises(PermissionError): steps.recover(cursor,ledger)


def test_missing_snapshot_is_not_rebuilt_by_executing_a_step(task):
    store,steps,row,ledger,cursor=task
    with closing(store.tasks.connect()) as conn,conn:
        conn.execute('DELETE FROM temporal_artifacts WHERE id=?',(cursor['state_ref']['id'],))
    with pytest.raises(KeyError): steps.claim(cursor,ledger)


@pytest.mark.parametrize('field,value',[('revision',True),('revision',-1),('next_kind',{}),
    ('next_kind','run_command'),('schema_version',True)])
def test_malformed_wire_cursor_fails_closed(task,field,value):
    cursor=copy.deepcopy(task[-1]);cursor[field]=value
    with pytest.raises(ValueError): validate_cursor(cursor)


def test_extra_identity_or_changed_phase_cannot_override_persisted_cursor(task):
    store,steps,row,ledger,cursor=task
    bad={**cursor,'identity':'admin'}
    with pytest.raises(ValueError): steps.claim(bad,ledger)
    bad={**cursor,'next_kind':'tool'}
    with pytest.raises(PermissionError): steps.claim(bad,ledger)
    bad={**cursor,'submission_id':'a'*32}
    with pytest.raises(PermissionError): steps.claim(bad,ledger)
    with pytest.raises(PermissionError): steps.commit(bad,'fence',ledger,'tool')
    with pytest.raises(PermissionError): steps.recover(bad,ledger)


def test_unknown_schema_and_protocol_are_not_downgraded(task):
    store,steps,row,ledger,cursor=task
    with closing(store.tasks.connect()) as conn,conn:
        conn.execute('UPDATE temporal_step_schema SET version=99')
        conn.execute('UPDATE temporal_protocols SET version=99')
    with pytest.raises(ValueError): StepStore(store)
    with pytest.raises(ValueError): store.workflow_type(row)


def test_unsupported_protocol_is_not_dispatched_during_rollback(task):
    from agent_execution.temporal_backend.client import dispatch
    store,steps,row,ledger,cursor=task
    with closing(store.tasks.connect()) as conn,conn:
        conn.execute('UPDATE temporal_protocols SET version=99 WHERE submission_id=?',(row['id'],))
    client=Mock()
    with pytest.raises(ValueError,match='Unsupported Temporal execution protocol'):
        asyncio.run(dispatch(store,row,client))
    client.start_workflow.assert_not_called()
    client.get_workflow_handle.assert_not_called()


def test_snapshot_counters_are_not_boolean_or_negative(task):
    snapshot=task[-2].load(task[-1]['state_ref'])
    for value in [True,-1]:
        bad=copy.deepcopy(snapshot);bad['checkpoint']['iteration']=value
        with pytest.raises(ValueError):validate_snapshot(bad)


@pytest.mark.parametrize('name,value',[('provider_retry_limit',True),('provider_retry_limit',257),
    ('eval_retry_limit',-1),('eval_retry_limit','2')])
def test_snapshot_retry_limits_fail_closed(task,name,value):
    snapshot=task[-2].load(task[-1]['state_ref'])
    bad=copy.deepcopy(snapshot);bad['checkpoint'][name]=value
    with pytest.raises(ValueError):validate_snapshot(bad)
