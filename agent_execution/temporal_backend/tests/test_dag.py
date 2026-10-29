"""TP21 owner, backend, dependency, budget and process-fence contracts."""
import asyncio
from contextlib import closing
import json
import os
import subprocess
import sys
from types import SimpleNamespace as NS
from unittest.mock import patch,AsyncMock
import pytest

from agent_execution.coordinator import Coordinator,scope_owner,SUCCESS,STOPPED
from agent_execution.policy import execution_scope
from agent_execution.tasks import _active
from agent_execution.budgets import budget_scope,BudgetExhausted
from agent_execution.worktrees import git
from permissions import set_current_role
from agent_execution.temporal_backend.dag import Graphs,busy
from agent_execution.temporal_backend.dag_contracts import envelope,probe,should_continue_as_new


@pytest.mark.parametrize(('suggested','active','expected'),[
    (True,0,True),(True,1,False),(False,0,False),(True,-1,False),(True,False,False)])
def test_dag_continue_as_new_requires_suggestion_and_settled_children(suggested,active,expected):
    assert should_continue_as_new(suggested,active) is expected


def test_dag_contracts_do_not_require_temporal_sdk():
    script = (
        "import builtins\n"
        "original = builtins.__import__\n"
        "def blocked(name, *args, **kwargs):\n"
        "    if name == 'temporalio' or name.startswith('temporalio.'):\n"
        "        raise ModuleNotFoundError(name)\n"
        "    return original(name, *args, **kwargs)\n"
        "builtins.__import__ = blocked\n"
        "from agent_execution.temporal_backend.dag_contracts import should_continue_as_new\n"
        "assert should_continue_as_new(True, 0) is True\n"
    )
    result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_dag_workflow_rotates_run_without_stopping_graph(monkeypatch):
    pytest.importorskip('temporalio', reason='Temporal workflow SDK is an optional dependency')
    from types import SimpleNamespace
    from temporalio import workflow as sdk_workflow
    import agent_execution.temporal_backend.dag_workflow as dag_workflow

    class ContinueSignal(sdk_workflow.ContinueAsNewError):
        pass

    calls=[]
    async def execute(name,value,**kwargs):
        calls.append(name)
        return {'done':False,'stop':False,'ready':[],'concurrency':1}
    def continue_run(value):
        calls.append(('continue',value))
        raise ContinueSignal()
    fake=SimpleNamespace(ContinueAsNewError=sdk_workflow.ContinueAsNewError,
        info=lambda:SimpleNamespace(is_continue_as_new_suggested=lambda:True),
        execute_activity=execute,continue_as_new=continue_run,sleep=lambda *_:None)
    monkeypatch.setattr(dag_workflow,'workflow',fake)
    with pytest.raises(ContinueSignal):
        asyncio.run(dag_workflow.CoordinatorDAG().run({'graph_id':'a'*32}))
    assert calls==['pythonai_dag_snapshot_v1',('continue',{'graph_id':'a'*32})]


@pytest.fixture
def setup(tmp_path,monkeypatch):
    repo=tmp_path/'repo'
    repo.mkdir()
    git(repo,'init')
    git(repo,'config','user.name','Test')
    git(repo,'config','user.email','test@example.invalid')
    (repo/'original.txt').write_text('original',encoding='utf-8')
    git(repo,'add','original.txt')
    git(repo,'commit','-m','base')
    for key,value in {
        'AGENT_TASK_DB':tmp_path/'tasks.db','AGENT_APPROVAL_DB':tmp_path/'approvals.db',
        'AGENT_WORKSPACE_ROOT':repo,'AGENT_PROJECT_WORKSPACES':'{}',
        'AGENT_WORKTREE_ROOT':tmp_path/'worktrees','AGENT_TASK_WORKTREE':'0',
        'AGENT_TASK_MAX_CHILDREN':'8','AGENT_TASK_MAX_DEPTH':'2','AGENT_TASK_MAX_REQUESTS':'40',
        'AGENT_TASK_MAX_TOKENS':'1000000','AGENT_TASK_DEADLINE_SECONDS':'600',
        'AGENT_SUBAGENT_TIMEOUT_SECONDS':'120','AGENT_SUBAGENT_CONCURRENCY':'2',
        'AGENT_COORDINATOR_BACKEND':'temporal','AGENT_TEMPORAL_FILE_WRITES':'0',
        'AGENT_TEMPORAL_ENGINE':'bridge','AGENT_TENANT_ROOT':tmp_path/'tenants'
    }.items():monkeypatch.setenv(key,str(value))
    monkeypatch.delenv('AGENT_TEMPORAL_TRIAL_OWNERS',raising=False)
    with execution_scope(source='local',identity='local',project='default',session='tp21',task_id='root',
            workspace=repo,repository_workspace=None,coordinator_node=None,coordinator_owner=None,lifecycle_owner=None),set_current_role('admin'):
        store=Coordinator()
        owner=scope_owner()
        criteria={'version':1,'checks':[{'kind':'file_contains','path':'result.txt','text':'result'}]}
        token=store.tasks.claim(owner,'root','delegate',[{'role':'user','content':'delegate'}],criteria=criteria)
        active=_active.set((store.tasks,owner,'root',token))
        try:
            with budget_scope() as budget:
                yield NS(store=store,graphs=Graphs(store),owner=owner,budget=budget,repo=repo,root=tmp_path)
        finally:_active.reset(active)


def group(s,key=None):
    with closing(s.store.connect()) as conn:
        return dict(conn.execute('SELECT * FROM temporal_agent_graphs').fetchone())


def submit(s,persona='planner',goal='private task',dependencies=None):
    return s.store.submit(persona,goal,dependencies)


def other_root(s,backend='temporal'):
    from agent_execution.budgets import _budget
    with execution_scope(task_id='other-root'),patch.dict(os.environ,{'AGENT_COORDINATOR_BACKEND':backend}):
        lease=s.store.tasks.claim(s.owner,'other-root','other',[{'role':'user','content':'other'}],criteria={'version':1,'checks':[]})
        active=_active.set((s.store.tasks,s.owner,'other-root',lease))
        inherited=_budget.set(None)
        try:
            with budget_scope():return s.store.submit('planner','other root')
        finally:
            _budget.reset(inherited)
            _active.reset(active)


def result(s,node_id,persona=None,review=None):
    g=group(s)
    node=s.graphs.claim(g['id'],node_id)
    value={'success':True,'verified':True,'reply':'private result','commit':node['payload']['snapshot']}
    if review is not None:value['review']=review
    s.store.publish(node,value)
    s.graphs.process_done(g['id'],node)
    return node


def test_default_coordinator_stays_sdk_free_sqlite(setup,monkeypatch):
    s=setup
    monkeypatch.setenv('AGENT_COORDINATOR_BACKEND','sqlite')
    import builtins
    original=builtins.__import__
    def guarded(name,*args,**kwargs):
        if name.startswith('temporalio'):raise AssertionError('legacy imported SDK')
        return original(name,*args,**kwargs)
    with patch('builtins.__import__',side_effect=guarded):
        key=submit(s)
        assert s.store.claim()['id']==key
    with closing(s.store.connect()) as conn:
        assert not conn.execute('SELECT 1 FROM temporal_agent_graphs').fetchone()


def test_existing_unpinned_legacy_root_never_migrates(setup,monkeypatch):
    s=setup
    monkeypatch.setenv('AGENT_COORDINATOR_BACKEND','sqlite')
    first=submit(s)
    with closing(s.store.connect()) as conn,conn:conn.execute('DELETE FROM agent_root_backends')
    monkeypatch.setenv('AGENT_COORDINATOR_BACKEND','temporal')
    submit(s,goal='followup')
    assert s.store.claim()['id']==first
    with closing(s.store.connect()) as conn:
        assert not conn.execute('SELECT 1 FROM temporal_agent_graphs').fetchone()


def test_native_backend_is_pinned_and_legacy_never_claims(setup,monkeypatch):
    s=setup
    first=submit(s)
    monkeypatch.setenv('AGENT_COORDINATOR_BACKEND','sqlite')
    second=submit(s)
    assert s.store.claim() is None
    assert s.store.get(s.owner,first)['payload']['temporal_graph']==s.store.get(s.owner,second)['payload']['temporal_graph']
    assert s.budget.snapshot()['children']==2
    assert Graphs(Coordinator()).queue==s.graphs.queue


def test_legacy_recovery_never_replays_unknown_native_node(setup):
    s=setup
    key=submit(s)
    g=group(s)
    node=s.graphs.claim(g['id'],key)
    with closing(s.store.connect()) as conn,conn:conn.execute('UPDATE agent_nodes SET expires=0 WHERE id=?',(key,))
    s.store.recover()
    assert s.store.claim() is None
    assert s.store.get(s.owner,key)['state']=='running'
    assert s.graphs.claim(g['id'],key) is None
    assert s.store.get(s.owner,key)['attempts']==1
    assert s.budget.snapshot()['children']==1


def test_planner_dependency_and_independent_reviewer_mapping(setup):
    s=setup
    planner=submit(s)
    coder=submit(s,'coder','create result',[planner])
    follower=submit(s,'planner','after review',[coder])
    g=group(s)
    assert s.graphs.snapshot(g['id'])['ready']==[planner]
    with pytest.raises(PermissionError):s.graphs.claim(g['id'],coder)
    result(s,planner)
    assert s.graphs.snapshot(g['id'])['ready']==[coder]
    result(s,coder)
    assert s.store.get(s.owner,coder)['state']=='review_pending'
    status=s.graphs.snapshot(g['id'])
    with closing(s.store.connect()) as conn:
        reviewer=conn.execute('SELECT id FROM agent_nodes WHERE review_of=?',(coder,)).fetchone()[0]
    assert reviewer in status['ready'] and follower not in status['ready'] and not status['done']
    result(s,reviewer,review={'approve':True,'findings':[]})
    assert s.store.get(s.owner,coder)['state']=='reviewed'
    assert follower in s.graphs.snapshot(g['id'])['ready']
    assert s.budget.snapshot()['children']==4


@pytest.mark.parametrize('review',[
    {'approve':False,'findings':[]},
    {'approve':True,'findings':[{'path':'result.txt','line':1,'message':'fix'}]},
    {'approve':1,'findings':[]},
    {'approve':True,'findings':[],'extra':True}
])
def test_native_reviewer_cannot_approve_malformed_or_rejected_result(setup,review):
    s=setup
    coder=submit(s,'coder')
    follower=submit(s,dependencies=[coder])
    result(s,coder)
    with closing(s.store.connect()) as conn:
        reviewer=conn.execute('SELECT id FROM agent_nodes WHERE review_of=?',(coder,)).fetchone()[0]
    result(s,reviewer,review=review)
    assert s.store.get(s.owner,coder)['state']=='rejected'
    status=s.graphs.snapshot(group(s)['id'])
    assert follower not in status['ready']
    assert s.store.get(s.owner,follower)['state']=='failed'


def test_metadata_retry_never_counts_another_child_or_resets_budget(setup):
    s=setup
    key=submit(s)
    g=group(s)
    contract=g['contract']
    for _ in range(3):assert key in s.graphs.snapshot(g['id'])['ready']
    first=s.graphs.claim(g['id'],key)
    assert first
    assert s.graphs.claim(g['id'],key) is None
    assert s.budget.snapshot()['children']==1 and group(s)['contract']==contract


@pytest.mark.parametrize('field,value',[('deadline',9999999999),('limits','{}')])
def test_changed_root_budget_contract_blocks_native_io(setup,field,value):
    s=setup
    key=submit(s)
    with closing(s.store.connect()) as conn,conn:
        conn.execute('UPDATE task_budgets SET '+field+'=? WHERE owner=? AND id=?',(value,s.owner,'root'))
    with pytest.raises(PermissionError,match='contract'):s.graphs.claim(group(s)['id'],key)
    assert s.store.get(s.owner,key)['attempts']==0


def test_child_budget_including_reviewer_is_atomic(setup):
    s=setup
    with closing(s.store.connect()) as conn,conn:
        limits=json.loads(s.budget.snapshot()['limits']);limits['children']=1
        conn.execute('UPDATE task_budgets SET limits=? WHERE owner=? AND id=?',(json.dumps(limits),s.owner,'root'))
    from agent_execution.budgets import TaskBudget
    s.budget.max_children=1
    coder=submit(s,'coder')
    result(s,coder)
    assert s.store.get(s.owner,coder)['state']=='needs_input'
    assert s.budget.snapshot()['children']==1
    with closing(s.store.connect()) as conn:
        assert conn.execute('SELECT count(*) FROM agent_nodes').fetchone()[0]==1


def test_trial_denial_rolls_back_node_and_budget_admission(setup,monkeypatch):
    s=setup
    monkeypatch.setenv('AGENT_TEMPORAL_TRIAL_OWNERS','[]')
    with pytest.raises(PermissionError):submit(s)
    assert s.budget.snapshot()['children']==0
    with closing(s.store.connect()) as conn:
        assert not conn.execute('SELECT 1 FROM agent_nodes').fetchone()
        assert not conn.execute('SELECT 1 FROM agent_root_backends').fetchone()


@pytest.mark.parametrize('role',['viewer','operator'])
def test_non_admin_cannot_admit_native_dag(setup,role):
    with set_current_role(role),pytest.raises(PermissionError):submit(setup)
    assert setup.budget.snapshot()['children']==0


def test_operator_owner_scope_is_enforced(setup):
    s=setup
    submit(s)
    key=group(s)['id']
    with execution_scope(session='other'):
        with pytest.raises(PermissionError):s.graphs.cancel(key)
        with pytest.raises(PermissionError):s.graphs.seal(key)
        with pytest.raises(PermissionError):s.graphs.get(key,operator=True)
    with set_current_role('viewer'),pytest.raises(PermissionError):s.graphs.get(key,operator=True)


def test_seal_blocks_new_nodes_but_allows_required_reviewer(setup):
    s=setup
    coder=submit(s,'coder')
    key=group(s)['id']
    s.graphs.seal(key)
    with pytest.raises(PermissionError):submit(s)
    result(s,coder)
    assert s.store.get(s.owner,coder)['state']=='review_pending'
    assert not s.graphs.snapshot(key)['done']


def test_parent_cancel_fences_late_publication_and_waits_for_process(setup):
    s=setup
    key=submit(s)
    graph=group(s)['id']
    node=s.graphs.claim(graph,key)
    s.store.tasks.cancel(s.owner,'root')
    status=s.graphs.snapshot(graph)
    assert status['stop'] and not status['done'] and not status['ready']
    assert s.store.cancelled(node,node['lease'])
    with pytest.raises(RuntimeError):s.store.publish(node,{'success':True})
    with closing(s.store.connect()) as conn:assert busy(conn,[s.owner])
    s.graphs.process_done(graph,node)
    assert s.graphs.snapshot(graph)['done']
    with closing(s.store.connect()) as conn:assert not busy(conn,[s.owner])


def test_unknown_activity_is_fenced_without_replay(setup):
    s=setup
    key=submit(s)
    graph=group(s)['id']
    node=s.graphs.claim(graph,key)
    s.graphs.node_stopped(graph,key)
    assert s.store.get(s.owner,key)['state']=='needs_resolution'
    assert s.graphs.claim(graph,key) is None
    with closing(s.store.connect()) as conn:assert busy(conn,[s.owner])
    s.graphs.process_done(graph,node)
    with closing(s.store.connect()) as conn:assert not busy(conn,[s.owner])


def test_live_native_activity_blocks_session_cleanup_even_with_terminal_projection(setup):
    from agent_execution.workspace_lifecycle.erasure import external_blockers
    s=setup
    key=submit(s)
    graph=group(s)['id']
    node=s.graphs.claim(graph,key)
    s.graphs.node_stopped(graph,key)
    assert 'temporal-dag:local-process' in external_blockers([s.owner],s.store.path)
    s.graphs.process_done(graph,node)
    assert 'temporal-dag:local-process' not in external_blockers([s.owner],s.store.path)


@pytest.mark.parametrize('setting,value',[
    ('AGENT_TEMPORAL_ADDRESS','127.0.0.1:7999'),
    ('AGENT_TEMPORAL_NAMESPACE','other'),
    ('AGENT_WORKSPACE_ROOT','elsewhere')
])
def test_configuration_drift_blocks_node_claim(setup,monkeypatch,setting,value):
    s=setup
    key=submit(s)
    monkeypatch.setenv(setting,value)
    with pytest.raises(PermissionError):s.graphs.claim(group(s)['id'],key)
    assert s.store.get(s.owner,key)['attempts']==0


def test_parent_tool_ceiling_is_saved_for_children_and_reviewer(setup):
    s=setup
    with execution_scope(allowed_tools=frozenset({'read_file','invoke_subagent'})):
        key=submit(s,'coder')
    node=s.store.get(s.owner,key)
    assert node['payload']['parent_tools']==['invoke_subagent','read_file']
    result(s,key)
    with closing(s.store.connect()) as conn:
        reviewer=s.store.decode(conn.execute('SELECT * FROM agent_nodes WHERE review_of=?',(key,)).fetchone())
    assert reviewer['payload']['parent_tools']==node['payload']['parent_tools']


@pytest.mark.parametrize('value',[{}, {'graph_id':'prompt'}, {'graph_id':'a'*32,'path':'private'}, {'graph_id':'a'*32,'node_id':'b'*32}])
def test_parent_wire_envelope_rejects_non_reference_fields(value):
    with pytest.raises(ValueError):envelope(value)


def test_cli_status_has_no_prompt_or_result(setup,capsys):
    from agent_execution.temporal_backend.dag_cli import main
    s=setup
    submit(s)
    with patch('dotenv.load_dotenv'):
        assert main(['status','--id',group(s)['id']])==0
    output=capsys.readouterr().out
    assert 'private task' not in output and 'private result' not in output
    assert json.loads(output)['nodes'][0]['state']=='queued'


def test_root_completion_does_not_make_unreviewed_coder_success(setup):
    s=setup
    coder=submit(s,'coder')
    result(s,coder)
    with closing(s.store.connect()) as conn,conn:
        conn.execute("UPDATE tasks SET state='completed' WHERE owner=? AND id='root'",(s.owner,))
    status=s.graphs.snapshot(group(s)['id'])
    assert not status['done'] and s.store.get(s.owner,coder)['state']=='review_pending'


def test_native_worker_rejects_a_legacy_dispatch_lease_without_activity_permit(setup):
    import time
    from agent_execution.coordinator_worker import execute_node
    s=setup
    key=submit(s,'coder')
    with closing(s.store.connect()) as conn,conn:
        conn.execute("UPDATE agent_nodes SET state='running',lease='legacy',expires=? WHERE id=?",(time.time()+90,key))
    node=s.store.get(s.owner,key)
    with pytest.raises(PermissionError,match='Activity permit'):
        execute_node(s.store,node)
    assert not (s.root/'worktrees'/(key+'.effects.log')).exists()


def test_trial_closure_stops_existing_unstarted_nodes(setup,monkeypatch):
    s=setup
    key=submit(s)
    monkeypatch.setenv('AGENT_TEMPORAL_TRIAL_OWNERS','[]')
    status=s.graphs.snapshot(group(s)['id'])
    assert status['done'] and not status['ready']
    assert s.store.get(s.owner,key)['state']=='cancelled'


def test_depth_exhaustion_does_not_allocate_another_child(setup):
    from agent_execution.budgets import _depth
    s=setup
    submit(s)
    token=_depth.set(2)
    try:
        with pytest.raises(BudgetExhausted):submit(s)
    finally:_depth.reset(token)
    assert s.budget.snapshot()['children']==1


def test_local_probe_keeps_strict_booleans_through_sqlite_seal_and_completion(setup):
    s=setup
    key=submit(s)
    graph=group(s)['id']
    probe(s.graphs.snapshot(graph))
    s.graphs.seal(graph)
    result(s,key)
    assert probe(s.graphs.snapshot(graph))['done'] is True
    assert probe(s.graphs.snapshot(graph))['stop'] is False


def test_node_task_binding_rejects_legacy_task_claim(setup):
    from agent_execution.coordinator import node_task_owner
    s=setup
    key=submit(s)
    node=s.graphs.claim(group(s)['id'],key)
    with pytest.raises(PermissionError,match='legacy executor'):
        s.store.tasks.claim(node_task_owner(node['payload']),'subagent:'+key,'bad',[{'role':'user','content':'bad'}])


def test_session_tombstone_stops_new_node_execution(setup):
    from agent_execution.workspace_lifecycle import LifecycleStore
    s=setup
    key=submit(s)
    life=LifecycleStore()
    session=life.ensure_session()
    with closing(life.connect()) as conn,conn:
        conn.execute("UPDATE wl_sessions SET state='deleting' WHERE id=?",(session,))
    assert s.graphs.snapshot(group(s)['id'])['done']
    with pytest.raises(PermissionError):s.graphs.claim(group(s)['id'],key)


def test_even_finished_dag_keeps_root_budget_out_of_ordinary_retention(setup):
    import time
    from agent_execution.temporal_backend.retention import eligible
    s=setup
    key=submit(s)
    result(s,key)
    s.graphs.seal(group(s)['id'])
    assert s.graphs.snapshot(group(s)['id'])['done']
    with closing(s.store.connect()) as conn:
        assert not eligible(conn,{'owner':s.owner,'task_id':'root'},time.time())


def test_dag_retention_deletes_closed_history_and_fences_new_lifecycle(setup,monkeypatch):
    import time
    from agent_execution.temporal_backend.admission import owner_key
    from agent_execution.temporal_backend.dag_retention import candidates,collect
    from agent_execution.temporal_backend.dag_lifecycle import Lifecycle
    from temporalio.service import RPCError,RPCStatusCode

    s=setup
    node=submit(s)
    result(s,node)
    s.graphs.seal(group(s)['id'])
    assert s.graphs.snapshot(group(s)['id'])['done']
    graph_id=group(s)['id']
    before=time.time()-30*86400
    with closing(s.store.connect()) as conn,conn:
        conn.execute("UPDATE tasks SET state='completed',lease_owner=NULL,lease_until=NULL WHERE owner=? AND id='root'",(s.owner,))
        conn.execute('UPDATE temporal_agent_graphs SET updated=? WHERE id=?',(before-10,graph_id))
        from agent_execution.coordinator import node_task_owner
        conn.execute('''INSERT INTO tasks(owner,id,goal,criteria,state,phase,checkpoint,episode,reply,
            lease_owner,lease_until,attempts,updated) VALUES(?,?,?,'{"version":1,"checks":[]}',
            'completed','done','{}',0,'done',NULL,NULL,1,?)''',
            (node_task_owner(s.store.get(s.owner,node)['payload']),'subagent:'+node,'goal',time.time()))
    digest=owner_key(s.owner)
    assert candidates(s.graphs,digest,before)==[graph_id]

    class Client:
        def __init__(self,open_run=False):self.open_run=open_run
        async def list_workflows(self,**kwargs):
            if self.open_run:yield NS(run_id='still-open')
        namespace='pythonai-dev'
        def get_workflow_handle(self,*args,**kwargs):return Handle(self.open_run)
    class Handle:
        def __init__(self,open_run):self.open_run=open_run
        async def describe(self,**kwargs):
            if self.open_run:return NS(run_id='still-open',status=WorkflowExecutionStatus.RUNNING)
            raise RPCError('not found',RPCStatusCode.NOT_FOUND,b'')
    async def connect_open(_settings):return Client(open_run=True)
    async def connect_closed(_settings):return Client()
    from temporalio.client import WorkflowExecutionStatus
    from agent_execution.temporal_backend.erasure import ErasurePending
    monkeypatch.setattr('agent_execution.temporal_backend.client.connect',connect_open)
    with pytest.raises(ErasurePending,match='still open'):
        asyncio.run(collect(s.graphs,graph_id,digest,before))
    with closing(s.store.connect()) as conn:
        assert conn.execute('SELECT state FROM temporal_dag_gc WHERE graph_id=?',(graph_id,)).fetchone()[0]=='pending'
    with closing(s.store.connect()) as conn,conn:
        conn.execute('UPDATE temporal_agent_graphs SET updated=? WHERE id=?',(time.time(),graph_id))
    assert candidates(s.graphs,digest,before)==[graph_id], 'pending GC must remain resumable after the original age cutoff'
    with pytest.raises(PermissionError,match='History retention'):
        Lifecycle(s.graphs).request(graph_id,node,'archive')
    monkeypatch.setattr('agent_execution.temporal_backend.client.connect',connect_closed)
    receipt=asyncio.run(collect(s.graphs,graph_id,digest,before))
    assert receipt['state']=='history_collected'
    with closing(s.store.connect()) as conn:
        row=conn.execute('SELECT remote_deleted FROM temporal_agent_graphs WHERE id=?',(graph_id,)).fetchone()
        gc=conn.execute('SELECT state,receipt FROM temporal_dag_gc WHERE graph_id=?',(graph_id,)).fetchone()
        assert row['remote_deleted']==1 and gc['state']=='completed'
        assert json.loads(gc['receipt'])['history']=='not_found_verified'
    with pytest.raises(PermissionError,match='History retention'):
        Lifecycle(s.graphs).request(graph_id,node,'archive')


def test_dag_erasure_requires_remote_proof_and_keeps_hashed_receipt(setup):
    from agent_execution.workspace_lifecycle import LifecycleStore
    from agent_execution.temporal_backend.dag_erasure import scrub,receipts
    from agent_execution.temporal_backend.erasure import ErasurePending
    s=setup
    submit(s)
    key=group(s)['id']
    life=LifecycleStore()
    session=life.ensure_session()
    with closing(life.connect()) as conn,conn:
        conn.execute("UPDATE wl_sessions SET state='deleting' WHERE id=?",(session,))
    with pytest.raises(ErasurePending):scrub(life,session)
    with closing(life.connect()) as conn,conn:
        conn.execute('UPDATE temporal_agent_graphs SET remote_deleted=1 WHERE id=?',(key,))
    scrub(life,session)
    with closing(life.connect()) as conn:
        proof=receipts(conn,session)
        assert proof and key not in json.dumps(proof)
        assert not conn.execute('SELECT 1 FROM temporal_agent_graphs').fetchone()
    with pytest.raises(PermissionError):submit(s)


def test_dag_history_erasure_deletes_every_continue_as_new_run():
    from datetime import timedelta
    from types import SimpleNamespace as NS
    from temporalio.client import WorkflowExecutionStatus
    from temporalio.service import RPCError,RPCStatusCode
    from agent_execution.temporal_backend.dag_erasure import delete_history
    from agent_execution.temporal_backend.erasure import ErasurePending

    class Client:
        def __init__(self):
            self.runs={'old-run':WorkflowExecutionStatus.COMPLETED,
                       'current-run':WorkflowExecutionStatus.RUNNING}
            self.deleted=[]
            self.workflow_service=NS(delete_workflow_execution=self.delete)
        async def list_workflows(self,query,**kwargs):
            for run_id in list(self.runs):yield NS(run_id=run_id)
        def get_workflow_handle(self,workflow_id,run_id=None):
            return Handle(self,run_id or 'current-run')
        async def delete(self,request,timeout):
            run_id=request.workflow_execution.run_id
            self.deleted.append(run_id)
            self.runs.pop(run_id,None)

    class Handle:
        def __init__(self,client,run_id):self.client,self.run_id=client,run_id
        async def describe(self,**kwargs):
            if self.run_id not in self.client.runs:
                raise RPCError('not found',RPCStatusCode.NOT_FOUND,b'')
            return NS(run_id=self.run_id,status=self.client.runs[self.run_id])
        async def terminate(self,*args,**kwargs):
            self.client.runs[self.run_id]=WorkflowExecutionStatus.TERMINATED

    client=Client()
    with pytest.raises(ErasurePending,match='still open'):
        asyncio.run(delete_history(client,'test-namespace','pythonai/dag/example',timedelta(seconds=1)))
    asyncio.run(delete_history(client,'test-namespace','pythonai/dag/example',timedelta(seconds=1),
                               terminate_running=True))
    assert client.deleted==['current-run','old-run']
    assert client.runs=={}


def test_frozen_session_plan_includes_exact_dag_targets(setup):
    from agent_execution.workspace_lifecycle import LifecycleStore
    from agent_execution.workspace_lifecycle.operations import check_items
    s=setup
    node=submit(s)
    result(s,node)
    s.graphs.seal(group(s)['id'])
    s.graphs.snapshot(group(s)['id'])
    with closing(s.store.connect()) as conn,conn:
        conn.execute("UPDATE tasks SET state='completed',lease_owner=NULL,lease_until=NULL WHERE owner=? AND id='root'",(s.owner,))
    life=LifecycleStore()
    life.ensure_session()
    value=life.preview('delete_session')
    plan=life.plan(value['plan_id'])
    assert plan['payload']['dags'][0]['nodes']==[node]
    with closing(s.store.connect()) as conn,conn:
        conn.execute("UPDATE temporal_agent_graphs SET namespace='changed'")
        with pytest.raises(ValueError,match='DAG targets changed'):check_items(life,plan,conn)


def test_cancelled_dag_rejects_a_new_guarded_merge(setup):
    from agent_execution.coordinator import merge_node
    s=setup
    coder=submit(s,'coder')
    result(s,coder)
    with closing(s.store.connect()) as conn:
        reviewer=conn.execute('SELECT id FROM agent_nodes WHERE review_of=?',(coder,)).fetchone()[0]
    result(s,reviewer,review={'approve':True,'findings':[]})
    s.graphs.cancel(group(s)['id'])
    with pytest.raises(ValueError,match='DAG stopped'):merge_node(s.store,s.owner,coder)


def test_startup_does_not_start_legacy_service_for_native_only_nodes(setup):
    from agent_execution.coordinator import resume_coordinator
    submit(setup)
    assert resume_coordinator() is None


def test_native_claim_retains_global_cross_root_concurrency_limit(setup):
    from agent_execution.temporal_backend.dag import ConcurrencyFull
    s=setup
    first=submit(s)
    second=submit(s)
    root=group(s)['id']
    a=s.graphs.claim(root,first)
    s.graphs.claim(root,second)
    other=other_root(s)
    target=s.store.get(s.owner,other)['payload']['temporal_graph']
    with pytest.raises(ConcurrencyFull):s.graphs.claim(target,other)
    assert s.store.get(s.owner,other)['attempts']==0
    s.graphs.process_done(root,a)
    assert s.graphs.claim(target,other)['attempts']==1
