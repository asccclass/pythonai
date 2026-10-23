"""Real filesystem writes bound to exact V3 approvals and immutable observations."""
from contextlib import closing
import json
import os
from pathlib import Path
from unittest.mock import Mock,patch

import pytest

from agent_execution import execution_scope
from agent_execution.policy import current_context
from agent_execution.tasks import task_owner
from agent_execution.approvals import ApprovalStore
from agent_execution.temporal_backend.config import Settings
from agent_execution.temporal_backend.contracts import STEP_TOOLS,FILE_WRITE_TOOLS
from agent_execution.temporal_backend.store import Store
from agent_execution.temporal_backend.step_store import StepStore
from agent_execution.temporal_backend.context import execution_context
from agent_execution.temporal_backend.admission import owner_key
from agent_execution.temporal_backend.operations import run_operation
from agent_execution.temporal_backend.ledger import Ledger,OperationUncertain
from agent_execution.temporal_backend.file_writes import relative,prepare,capabilities
from agent_execution.temporal_backend.write_resolution import record,entries
from agent_execution.temporal_backend.tests.test_approval_gate import wait,approve,execute


@pytest.fixture
def task(tmp_path,monkeypatch):
    runtime = tmp_path/'.system_generated'
    runtime.mkdir()
    roles = runtime/'roles.json'
    roles.write_text(json.dumps({'websocket:alice':'admin'}))
    for name,value in {'AGENT_TASK_DB':str(runtime/'tasks.db'),'AGENT_APPROVAL_DB':str(runtime/'approvals.db'),
        'AGENT_TENANT_ROOT':str(runtime/'tenants'),'AGENT_WORKSPACE_ROOT':str(tmp_path),
        'AGENT_TEMPORAL_ENGINE':'approval','AGENT_TEMPORAL_FILE_WRITES':'1','AGENT_EXECUTION_BACKEND':'temporal',
        'ROLES_CONFIG':str(roles),'AGENT_TASK_WORKTREE':'0','AGENT_TRACE_ENABLED':'0'}.items():
        monkeypatch.setenv(name,value)
    monkeypatch.delenv('AGENT_PROJECT_WORKSPACES',raising=False)
    with execution_scope(source='websocket',identity='websocket:alice',session='device',project='default',
                         task_id='write-task',workspace=tmp_path,repository_workspace=None):
        owner = task_owner(current_context())
        monkeypatch.setenv('AGENT_TEMPORAL_TRIAL_OWNERS',json.dumps([owner_key(owner)]))
        store,approvals = Store(),ApprovalStore()
        store.bind(owner,'write-task','temporal')
        scope = {k:getattr(current_context(),k) for k in ('source','identity','session','project','conversation')}
        scope.update(workspace=str(tmp_path),allowed_tools=sorted(STEP_TOOLS|FILE_WRITE_TOOLS),
                     approval_db=str(approvals.path.resolve()),file_writes=1)
        row = store.submit(owner,'write-task',[{'role':'user','content':'Write program notes'}],scope,'admin',Settings.load())
        steps = StepStore(store)
        row = steps.bootstrap_claim(row['id'],Settings.load())
        with execution_context(store,row) as ledger:
            cursor = steps.bootstrap(row,ledger)
            yield store,steps,row,ledger,cursor,approvals,roles


def approved(task,name='write_file',arguments=None):
    arguments = {'path':'program/note.md','content':'繁體中文\nfirst line\n'} if arguments is None else arguments
    gate,cursor,intent = wait(task,name,arguments)
    assert not (Path(json.loads(task[2]['context'])['workspace'])/arguments.get('path','program/missing')).exists()
    approve(task,intent)
    assert gate.probe(cursor,task[3],task[1])['ready']
    return cursor,intent,arguments


@pytest.mark.parametrize('value',['../out','/absolute','C:/file','a/../b','a//b','a:stream','a/.git/x','a/./b','a\\..\\b','program/CON/note.md','program/AUX.txt','program/NUL/data'])
def test_paths_are_strictly_relative(value):
    with pytest.raises((ValueError,PermissionError)):
        relative(value)


@pytest.mark.parametrize('engine,owners,value',[('steps','[]','1'),('approval',None,'1'),('approval','[]','true')])
def test_write_configuration_is_explicit(monkeypatch,engine,owners,value):
    monkeypatch.setenv('AGENT_TEMPORAL_ENGINE',engine)
    monkeypatch.setenv('AGENT_TEMPORAL_FILE_WRITES',value)
    if owners is None:
        monkeypatch.delenv('AGENT_TEMPORAL_TRIAL_OWNERS',raising=False)
    else:
        monkeypatch.setenv('AGENT_TEMPORAL_TRIAL_OWNERS',owners)
    with pytest.raises(ValueError):
        Settings.load()


def test_write_policy_does_not_expand_old_or_wrong_protocol():
    from agent_execution.temporal_backend.contracts import APPROVAL_WORKFLOW_NAME
    assert not FILE_WRITE_TOOLS & capabilities({},APPROVAL_WORKFLOW_NAME)
    for flag in [True,0,'1',2]:
        with pytest.raises(PermissionError):
            capabilities({'file_writes':flag},APPROVAL_WORKFLOW_NAME)
    with pytest.raises(PermissionError):
        capabilities({'file_writes':1},'AgentTaskWorkflowV1')


def test_approved_write_uses_atomic_adapter_and_cache_without_reexecution(task):
    cursor,intent,args = approved(task)
    function = Mock(side_effect=AssertionError('Legacy direct write bypassed protection'))
    result = execute(task,cursor,'write_file',args,function)
    target = Path(json.loads(task[2]['context'])['workspace'])/args['path']
    assert target.read_bytes()==args['content'].replace('\n',os.linesep).encode()
    assert result['evidence']['kind']=='temporal_file_write'
    function.assert_not_called()
    assert task[5].decision_state(intent['approval_id'],'websocket:alice','device')=='consumed'
    cached = run_operation('tool','model:1:tool:read-one',{'name':'write_file','arguments':args},function)
    assert cached==result
    function.assert_not_called()
    assert not list(target.parent.glob('.pythonai-tp17-*'))
    assert entries(task[0].tasks,task[2]['owner'],'write-task')[0]['effect_observed']


def test_persisted_write_policy_survives_new_submission_flag_off(task,monkeypatch):
    cursor,intent,args = approved(task)
    monkeypatch.setenv('AGENT_TEMPORAL_FILE_WRITES','0')
    with execution_context(task[0],task[2]):
        assert FILE_WRITE_TOOLS<=current_context().allowed_tools
    assert execute(task,cursor,'write_file',args,Mock())['success']


def test_directory_batch_and_edit_are_exact_and_registered(task):
    args = {'paths':['program','program/notes','program/templates']}
    cursor,intent,args = approved(task,'create_directory',args)
    assert execute(task,cursor,'create_directory',args,Mock())['success']
    root = Path(json.loads(task[2]['context'])['workspace'])
    assert all((root/p).is_dir() for p in args['paths'])
    from agent_execution.workspace_lifecycle import LifecycleStore
    assert LifecycleStore().containing_directory(root/'program/notes')
    # Another operation must get its own approval; no grant reuse across tools.
    with pytest.raises(PermissionError):
        run_operation('tool','other-edit',{'name':'edit_file','arguments':{'path':'program/note.md','old_str':'','new_str':'new'}},
                      lambda: __import__('agent_execution.policy',fromlist=['execute_tool']).execute_tool(
                          'edit_file',{'path':'program/note.md','old_str':'','new_str':'new'},Mock()))


@pytest.mark.parametrize('change',['target','parent','role','cancel'])
def test_changed_preconditions_or_authority_never_overwrite(task,change):
    cursor,intent,args = approved(task)
    root = Path(json.loads(task[2]['context'])['workspace'])
    if change=='target':
        (root/'program').mkdir()
        (root/args['path']).write_text('external change')
    elif change=='parent':
        (root/'program').mkdir()
    elif change=='role':
        task[6].write_text(json.dumps({'websocket:alice':'viewer'}))
    else:
        task[0].tasks.cancel(task[2]['owner'],'write-task')
    with pytest.raises((PermissionError,OperationUncertain)):
        execute(task,cursor,'write_file',args,Mock())
    if change=='target':
        assert (root/args['path']).read_text()=='external change'
    else:
        assert not (root/args['path']).exists()


def test_hardlinked_existing_file_cannot_be_approved(task):
    root = Path(json.loads(task[2]['context'])['workspace'])
    (root/'program').mkdir()
    (root/'program/first').write_text('linked')
    os.link(root/'program/first',root/'program/second')
    with pytest.raises(PermissionError,match='unlinked'):
        wait(task,'write_file',{'path':'program/first','content':'replace'})


@pytest.mark.parametrize('fault',['consume','effect'])
def test_unknown_write_has_human_observation_without_resume_or_budget_reset(task,fault):
    cursor,intent,args = approved(task)
    original = Ledger.complete
    def lose(self,step,*rest):
        if step=='model:1:tool:read-one':
            raise RuntimeError('lost result after filesystem effect')
        return original(self,step,*rest)
    if fault=='consume':
        context = patch('agent_execution.temporal_backend.file_writes.execute_bound',side_effect=RuntimeError('lost after consume'))
    else:
        context = patch.object(Ledger,'complete',lose)
    with context,pytest.raises(RuntimeError):
        execute(task,cursor,'write_file',args,Mock())
    assert task[5].decision_state(intent['approval_id'],'websocket:alice','device')=='consumed'
    task[0].workflow_failed(task[2]['id'],task[2]['owner'],'FAILED')
    with pytest.raises((PermissionError,OperationUncertain)):
        execute(task,cursor,'write_file',args,Mock())
    from agent_execution.budgets import TaskBudget
    before = TaskBudget(task[2]['owner'],'write-task').snapshot()
    value = json.dumps({'status':'matches_expected' if fault=='effect' else 'matches_before','note':'Human inspected the isolated filesystem'})
    assert record(task[0].tasks,task[2]['owner'],'write-task','read-one',value)
    assert record(task[0].tasks,task[2]['owner'],'write-task','read-one',value)
    assert task[0].get(task[2]['id'])['state']=='uncertain'
    assert task[0].tasks.get(task[2]['owner'],'write-task')['state']=='uncertain'
    after = TaskBudget(task[2]['owner'],'write-task').snapshot()
    assert (before['requests'],before['deadline'])==(after['requests'],after['deadline'])
    assert entries(task[0].tasks,task[2]['owner'],'write-task')[0]['human_observation']
    with pytest.raises(PermissionError,match='immutable'):
        record(task[0].tasks,task[2]['owner'],'write-task','read-one',json.dumps({'status':json.loads(value)['status'],'note':'Changed attestation'}))


def test_oversized_content_and_missing_edit_rejected_before_approval(task):
    with pytest.raises(ValueError,match='2 MiB'):
        prepare(task[3],'oversized','oversized','write_file',{'path':'program/note.md','content':'x'*(2*1024*1024+1)})




def test_existing_utf8_edit_preserves_expected_windows_newlines(task):
    root = Path(json.loads(task[2]['context'])['workspace'])
    (root/'program').mkdir()
    (root/'program/note.md').write_text('first\n繁體中文\nfirst\n',encoding='utf-8')
    args = {'path':'program/note.md','old_str':'first','new_str':'updated'}
    gate,cursor,intent = wait(task,'edit_file',args)
    approve(task,intent)
    assert gate.probe(cursor,task[3],task[1])['ready']
    assert execute(task,cursor,'edit_file',args,Mock())['success']
    assert (root/'program/note.md').read_text(encoding='utf-8')=='updated\n繁體中文\nfirst\n'


def test_missing_edit_and_invalid_argument_shapes_do_not_create_plan(task):
    for arguments in [{'path':'program/missing.md','old_str':'old','new_str':'new'},
                      {'path':'program/note.md','content':'x','approval_id':'forged'}]:
        with pytest.raises(ValueError):
            prepare(task[3],'invalid','invalid','edit_file' if 'old_str' in arguments else 'write_file',arguments)
    root = Path(json.loads(task[2]['context'])['workspace'])
    assert not (root/'program').exists()


def test_linked_parent_cannot_be_approved(task):
    root = Path(json.loads(task[2]['context'])['workspace'])
    outside = root.parent/('outside-'+root.name)
    outside.mkdir()
    if os.name=='nt':
        import subprocess
        result = subprocess.run(['cmd','/c','mklink','/J',str(root/'linked'),str(outside)],
                                capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW)
        assert result.returncode==0
    else:
        (root/'linked').symlink_to(outside,target_is_directory=True)
    with pytest.raises(PermissionError,match='Linked'):
        wait(task,'write_file',{'path':'linked/note.md','content':'must not escape'})
    assert not (outside/'note.md').exists()


@pytest.mark.parametrize('status',['matches_expected','matches_before'])
def test_false_resolution_attestation_does_not_change_task(task,status):
    cursor,intent,args = approved(task)
    original = Ledger.complete
    def lost(self,step,*rest):
        if step=='model:1:tool:read-one':
            raise RuntimeError('lost')
        return original(self,step,*rest)
    with patch.object(Ledger,'complete',lost),pytest.raises(RuntimeError):
        execute(task,cursor,'write_file',args,Mock())
    task[0].workflow_failed(task[2]['id'],task[2]['owner'],'FAILED')
    root = Path(json.loads(task[2]['context'])['workspace'])
    (root/args['path']).write_text('unapproved external text')
    with pytest.raises(PermissionError,match='claimed observation'):
        record(task[0].tasks,task[2]['owner'],'write-task','read-one',json.dumps({'status':status,'note':'Incorrect claim'}))
    assert task[0].get(task[2]['id'])['state']=='uncertain'


def test_new_write_submission_does_not_expand_scope_to_commands(task):
    assert 'run_command' not in current_context().allowed_tools
    assert 'run_test_suite' not in current_context().allowed_tools
    assert 'delete_file' not in current_context().allowed_tools
    cursor,intent,args = approved(task,'write_file',{'path':'program/code.py','content':'def implemented(): return 1'})
    execute(task,cursor,'write_file',args,Mock())
    from agent_execution.acceptance import verify_contract
    checks,errors,_ = verify_contract({'version':1,'checks':[{'kind':'file_exists','path':'program/code.py'}]},
                                     Path(json.loads(task[2]['context'])['workspace']),Mock())
    assert not checks['acceptance:tests_required'] and errors




def test_direct_dispatch_cannot_skip_a_write_ledger_operation(task):
    from agent_execution.policy import execute_tool
    function = Mock()
    with pytest.raises(PermissionError,match='approved ledger'):
        execute_tool('write_file',{'path':'program/note.md','content':'bypass'},function)
    function.assert_not_called()


def test_process_exit_after_file_effect_retains_unknown_operation_without_replay(task):
    import subprocess
    import sys
    cursor,intent,args = approved(task)
    code = '''
import json,os
from pathlib import Path
from agent_execution import execution_scope
from agent_execution.policy import execute_tool
from agent_execution.temporal_backend.store import Store
from agent_execution.temporal_backend.step_store import StepStore
from agent_execution.temporal_backend.context import execution_context
from agent_execution.temporal_backend.operations import run_operation
from agent_execution.temporal_backend.ledger import Ledger
cursor=json.loads(CURSOR_JSON)
arguments=json.loads(ARGUMENTS_JSON)
with execution_scope(source='websocket',identity='websocket:alice',session='device',project='default',task_id='write-task',workspace=Path(os.environ['AGENT_WORKSPACE_ROOT']),repository_workspace=None):
    store=Store()
    row=store.get(cursor['submission_id'])
    with execution_context(store,row) as ledger:
        StepStore(store).claim(cursor,ledger)
        with store.tasks.connect() as conn:
            conn.execute("UPDATE tasks SET phase='tool_started' WHERE owner=? AND id=?",(row['owner'],row['task_id']))
        def crash(*args,**kwargs):
            os._exit(73)
        Ledger.complete=crash
        run_operation('tool','model:1:tool:read-one',{'name':'write_file','arguments':arguments},
            lambda:execute_tool('write_file',arguments,lambda **kw:None))
'''.replace('CURSOR_JSON',repr(json.dumps(cursor))).replace('ARGUMENTS_JSON',repr(json.dumps(args)))
    result = subprocess.run([sys.executable,'-c',code],capture_output=True,timeout=30,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    assert result.returncode==73,result.stderr.decode(errors='replace')
    root = Path(json.loads(task[2]['context'])['workspace'])
    target = root/args['path']
    assert target.read_text(encoding='utf-8')==args['content']
    assert task[3].get('model:1:tool:read-one')['state']=='running'
    assert task[5].decision_state(intent['approval_id'],'websocket:alice','device')=='consumed'
    task[0].workflow_failed(task[2]['id'],task[2]['owner'],'FAILED')
    stamp = target.stat().st_mtime_ns
    with pytest.raises(PermissionError):
        run_operation('tool','model:1:tool:read-one',{'name':'write_file','arguments':args},Mock())
    from agent_execution.tasks import dispatch_task_input
    payload = json.dumps({'status':'matches_expected','note':'Human inspected output after terminated process'})
    response = dispatch_task_input('/resolve write-task read-one '+payload,Mock())
    assert 'remains fenced' in response.reply,response.reply
    response = dispatch_task_input('/steps write-task',Mock())
    assert json.loads(response.reply)['file_writes'][0]['human_observation']
    assert target.stat().st_mtime_ns==stamp
    assert task[0].get(task[2]['id'])['state']=='uncertain'




def test_adapter_requires_consumption_even_when_called_without_policy_dispatch(task):
    from agent_execution.temporal_backend.file_writes import execute_bound
    cursor,intent,args = approved(task)
    with pytest.raises(PermissionError,match='consumed approval'):
        run_operation('tool','model:1:tool:read-one',{'name':'write_file','arguments':args},
                      lambda:execute_bound('write_file',args,Mock()))
    root = Path(json.loads(task[2]['context'])['workspace'])
    assert not (root/'program').exists()
    assert task[5].decision_state(intent['approval_id'],'websocket:alice','device')=='approved'




def test_revoked_role_cannot_prepare_or_read_a_write_target(task):
    task[6].write_text(json.dumps({'websocket:alice':'viewer'}))
    with pytest.raises(PermissionError,match='before planning'):
        wait(task,'write_file',{'path':'program/note.md','content':'denied'})
    with closing(task[0].tasks.connect()) as conn:
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='temporal_file_writes'").fetchone()




def test_preflight_reports_the_new_write_policy(task):
    from agent_execution.temporal_backend.preflight import inspect
    value = inspect()
    assert value['file_writes'] is True and value['mode']=='approval_file_steps_v1'
    assert value['new_submission_engine']=='approval'




@pytest.mark.parametrize('operation,call_id',[('x'*513,'call'),('valid',''),('valid','x'*257)],ids=['step-limit','empty-call','call-limit'])
def test_write_operation_identifiers_are_bounded(task,operation,call_id):
    with pytest.raises(ValueError):
        prepare(task[3],operation,call_id,'write_file',{'path':'program/note.md','content':'data'})


def test_write_plan_capacity_rejects_before_filesystem_effect(task,monkeypatch):
    monkeypatch.setenv('AGENT_TEMPORAL_LEDGER_STORAGE_BYTES','1')
    from agent_execution.delivery import CapacityError
    with pytest.raises(CapacityError):
        wait(task,'write_file',{'path':'program/note.md','content':'text'})
    root = Path(json.loads(task[2]['context'])['workspace'])
    assert not (root/'program').exists()
