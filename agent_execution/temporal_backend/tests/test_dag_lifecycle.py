"""TP22 exact worktree targets, merge fences and dead-owner recovery contracts."""
from contextlib import closing
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from unittest.mock import patch
import pytest

from agent_execution.temporal_backend.tests.test_dag import setup, group, submit, result
from agent_execution.coordinator import merge_node
from agent_execution.policy import execution_scope
from agent_execution.worktrees import WorktreeStore, git
from agent_execution.temporal_backend.dag import busy
from agent_execution.temporal_backend.dag_lifecycle import Lifecycle, reap, register_process
from agent_execution.temporal_backend.references import process_stamp
from permissions import set_current_role


def reviewed(s):
    key = submit(s, 'coder')
    graph = group(s)['id']
    node = s.graphs.claim(graph, key)
    tree = WorktreeStore(node['payload']['repository'], node['payload']['storage'])
    path = tree.create(s.owner, key, node['payload']['snapshot'])
    (path/'result.txt').write_text('result', encoding='utf-8')
    git(path, 'add', 'result.txt')
    git(path, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-m', 'result')
    s.store.publish(node, {'success': True, 'verified': True, 'reply': 'private',
        'base': node['payload']['snapshot'], 'commit': git(path, 'rev-parse', 'HEAD').strip()})
    s.graphs.process_done(graph, node)
    with closing(s.store.connect()) as conn:
        reviewer = conn.execute('SELECT id FROM agent_nodes WHERE review_of=?', (key,)).fetchone()[0]
    result(s, reviewer, review={'approve': True, 'findings': []})
    return key, path


def test_durable_merge_is_idempotent_and_preserves_parent_head_and_budget(setup):
    s = setup
    node, path = reviewed(s)
    life = Lifecycle(s.graphs)
    before = s.budget.snapshot()
    head = git(s.repo, 'rev-parse', 'HEAD')
    key = life.request(group(s)['id'], node, 'merge')
    assert life.request(group(s)['id'], node, 'merge') == key
    with pytest.raises(PermissionError): merge_node(s.store, s.owner, node)
    assert life.execute(key) == {'operation_id': key}
    assert life.get(key)['state'] == 'completed'
    stamp = (s.repo/'result.txt').stat().st_mtime_ns
    assert life.execute(key) == {'operation_id': key}
    assert (s.repo/'result.txt').stat().st_mtime_ns == stamp
    assert git(s.repo, 'rev-parse', 'HEAD') == head
    assert s.budget.snapshot() == before
    assert path.is_dir()


def test_cancel_before_lifecycle_claim_has_no_filesystem_effect(setup):
    s=setup;node,_=reviewed(s);life=Lifecycle(s.graphs)
    key=life.request(group(s)['id'],node,'merge')
    life.cancel_pending(key)
    life.execute(key)
    assert life.get(key)['state']=='cancelled'
    assert not (s.repo/'result.txt').exists()


def test_existing_human_entry_only_persists_an_authorized_intent(setup):
    from agent_execution.coordinator import human_agent_command
    s=setup;node,_=reviewed(s)
    response=json.loads(human_agent_command('/agents root merge '+node))
    assert response['state']=='pending'
    rows=json.loads(human_agent_command('/agents root operations '+node))
    assert rows==[{'id':response['operation_id'],'action':'merge','state':'pending'}]
    assert not (s.repo/'result.txt').exists()


@pytest.mark.parametrize('failure_first',[False,True])
def test_late_worker_failure_cannot_downgrade_native_cancellation_fence(setup,failure_first):
    s=setup;key=submit(s);graph=group(s)['id'];node=s.graphs.claim(graph,key)
    if failure_first:s.store.fail(node,'failed','RuntimeError')
    s.graphs.node_stopped(graph,key)
    s.store.fail(node,'failed','RuntimeError')
    assert s.store.get(s.owner,key)['state']=='needs_resolution'
    s.graphs.process_done(graph,node)
    assert s.store.get(s.owner,key)['state']=='needs_resolution'
    with closing(s.store.connect()) as conn:
        assert conn.execute('SELECT state FROM temporal_agent_runs WHERE node_id=?',(key,)).fetchone()[0]=='completed'


@pytest.mark.parametrize('change', ['parent', 'worktree', 'review', 'role', 'cancel', 'deadline'])
def test_changed_authority_or_target_never_performs_lifecycle_io(setup, change):
    s = setup
    node, path = reviewed(s)
    life = Lifecycle(s.graphs)
    key = life.request(group(s)['id'], node, 'merge')
    if change == 'parent': (s.repo/'result.txt').write_text('parent edit', encoding='utf-8')
    elif change == 'worktree': (path/'untracked.txt').write_text('new', encoding='utf-8')
    elif change == 'cancel': s.graphs.cancel(group(s)['id'])
    elif change == 'deadline':
        with closing(s.store.connect()) as conn, conn:
            conn.execute('UPDATE task_budgets SET deadline=?', (time.time()-1,))
    elif change == 'review':
        with closing(s.store.connect()) as conn, conn:
            conn.execute("UPDATE agent_nodes SET state='rejected' WHERE persona='reviewer'")
    elif change == 'role':
        with set_current_role('viewer'):
            with pytest.raises(PermissionError): life.request(group(s)['id'], node, 'merge')
        return
    try: life.execute(key)
    except (ValueError, PermissionError): pass
    assert life.get(key)['state'] in {'failed', 'uncertain'}
    assert not (s.repo/'result.txt').exists() or (s.repo/'result.txt').read_text() == 'parent edit'


def test_merge_cancel_during_apply_rolls_back_and_never_publishes_success(setup):
    s = setup
    node, _ = reviewed(s)
    life = Lifecycle(s.graphs)
    key = life.request(group(s)['id'], node, 'merge')
    original = os.replace
    def replace(source, destination):
        original(source, destination)
        if str(destination)==str(s.repo/'result.txt'):
            s.graphs.cancel(group(s)['id'])
    with patch('agent_execution.coordinator.os.replace', side_effect=replace):
        with pytest.raises((PermissionError, RuntimeError)): life.execute(key)
    assert life.get(key)['state'] == 'uncertain'
    assert s.store.get(s.owner, node)['state'] != 'merged'
    assert not (s.repo/'result.txt').exists()
    life.execute(key)
    assert not (s.repo/'result.txt').exists()


def test_explicit_archive_preserves_commits_dirty_ignored_and_untracked_files(setup):
    s = setup
    node, path = reviewed(s)
    s.store.cancel(s.owner, node)
    s.graphs.seal(group(s)['id']);s.graphs.snapshot(group(s)['id'])
    (path/'private.txt').write_text('untracked', encoding='utf-8')
    (path/'.gitignore').write_text('ignored.bin\n', encoding='utf-8')
    (path/'ignored.bin').write_bytes(b'ignored')
    life = Lifecycle(s.graphs)
    key = life.request(group(s)['id'], node, 'archive')
    life.execute(key)
    assert life.get(key)['state'] == 'completed'
    assert not path.exists()
    worktrees = WorktreeStore(s.repo, s.root/'worktrees')
    restored = worktrees.restore(s.owner, node)
    assert (restored/'private.txt').read_text() == 'untracked'
    assert (restored/'ignored.bin').read_bytes() == b'ignored'
    assert (restored/'result.txt').read_text() == 'result'
    life.execute(key)
    assert restored.exists(), 'Replaying a completed archive cannot remove a restored tree'


def test_started_unknown_archive_never_replays(setup):
    s = setup
    node, path = reviewed(s)
    s.store.cancel(s.owner, node)
    s.graphs.seal(group(s)['id']);s.graphs.snapshot(group(s)['id'])
    life = Lifecycle(s.graphs)
    key = life.request(group(s)['id'], node, 'archive')
    with closing(s.store.connect()) as conn, conn:
        conn.execute("UPDATE temporal_dag_operations SET state='started',pid=99999999,process='dead' WHERE id=?", (key,))
    reap(s.graphs)
    assert life.get(key)['state'] == 'uncertain'
    with patch.object(WorktreeStore, 'archive', side_effect=AssertionError('Never repeat unknown I/O')):
        life.execute(key)
    assert path.is_dir()


def test_live_child_identity_protects_expired_orphan_and_worktree(setup):
    s = setup
    node = submit(s)
    g = group(s)['id']
    claimed = s.graphs.claim(g, node)
    register_process(s.store, claimed)
    with closing(s.store.connect()) as conn, conn:
        conn.execute("UPDATE temporal_agent_runs SET pid=99999999,process='dead'")
        conn.execute('UPDATE agent_nodes SET expires=0')
        assert busy(conn, [s.owner])
    reap(s.graphs)
    assert s.store.get(s.owner, node)['state'] == 'cancel_requested'
    with closing(s.store.connect()) as conn, conn:
        conn.execute("UPDATE temporal_agent_processes SET process='reused-pid'")
    budget = s.budget.snapshot()
    reap(s.graphs)
    assert s.store.get(s.owner, node)['state'] == 'needs_resolution'
    assert s.store.get(s.owner, node)['attempts'] == 1
    assert s.budget.snapshot() == budget
    assert s.graphs.claim(g, node) is None


def test_frozen_erasure_snapshot_includes_lifecycle_intents(setup):
    from agent_execution.temporal_backend.dag_erasure import preview
    s = setup
    node, _ = reviewed(s)
    with closing(s.store.connect()) as conn: before = preview(conn, [s.owner])
    key = Lifecycle(s.graphs).request(group(s)['id'], node, 'merge')
    with closing(s.store.connect()) as conn: after = preview(conn, [s.owner])
    assert 'operations' not in before[0]
    assert after[0]['operations'] == [key]


def test_completed_graph_can_be_fenced_before_a_manual_merge(setup):
    s = setup
    node, _ = reviewed(s)
    s.graphs.seal(group(s)['id'])
    assert s.graphs.snapshot(group(s)['id'])['done']
    s.graphs.cancel(group(s)['id'])
    with pytest.raises(PermissionError): Lifecycle(s.graphs).request(group(s)['id'], node, 'merge')


def test_archive_rejects_dirty_content_changes_even_when_git_status_is_identical(setup):
    s=setup;node,path=reviewed(s)
    s.store.cancel(s.owner,node);s.graphs.seal(group(s)['id']);s.graphs.snapshot(group(s)['id'])
    (path/'.gitignore').write_text('ignored.bin\n',encoding='utf-8')
    (path/'ignored.bin').write_bytes(b'before')
    life=Lifecycle(s.graphs);key=life.request(group(s)['id'],node,'archive')
    (path/'ignored.bin').write_bytes(b'after')
    life.execute(key)
    assert life.get(key)['state']=='failed' and path.exists()


def test_archive_rechecks_authorized_bytes_before_git_removes_the_tree(setup):
    from agent_execution.worktrees import snapshot_files
    s=setup;node,path=reviewed(s)
    s.store.cancel(s.owner,node);s.graphs.seal(group(s)['id']);s.graphs.snapshot(group(s)['id'])
    (path/'.gitignore').write_text('ignored.bin\n',encoding='utf-8')
    (path/'ignored.bin').write_bytes(b'before')
    life=Lifecycle(s.graphs);key=life.request(group(s)['id'],node,'archive')
    def change(root,*args,**kwargs):
        (path/'ignored.bin').write_bytes(b'after')
        return snapshot_files(root,*args,**kwargs)
    with patch('agent_execution.worktrees.snapshot_files',side_effect=change):
        with pytest.raises(ValueError):life.execute(key)
    assert life.get(key)['state']=='uncertain' and path.exists()
    assert (path/'ignored.bin').read_bytes()==b'after'


def test_real_orphan_process_must_exit_before_dead_host_recovery(setup):
    s=setup;node=submit(s);graph=group(s)['id'];claimed=s.graphs.claim(graph,node)
    marker=s.root/'child-ready'
    code=('import sys,time; from pathlib import Path; '
          'from agent_execution.coordinator import Coordinator; '
          'from agent_execution.temporal_backend.dag_lifecycle import register_process; '
          's=Coordinator(sys.argv[1]); n=s.get(sys.argv[2],sys.argv[3]); '
          'register_process(s,n); Path(sys.argv[4]).touch(); time.sleep(60)')
    child=subprocess.Popen([sys.executable,'-c',code,str(s.store.path),s.owner,node,str(marker)],
        creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    try:
        deadline=time.monotonic()+10
        while not marker.exists():
            assert child.poll() is None and time.monotonic()<deadline
            time.sleep(.05)
        with closing(s.store.connect()) as conn,conn:
            conn.execute("UPDATE temporal_agent_runs SET pid=99999999,process='dead'")
            conn.execute('UPDATE agent_nodes SET expires=0')
        reap(s.graphs)
        assert s.store.get(s.owner,node)['state']=='cancel_requested'
        assert s.store.cancelled(claimed,claimed['lease'])
        child.terminate();child.wait(timeout=5)
        reap(s.graphs)
        assert s.store.get(s.owner,node)['state']=='needs_resolution'
        assert s.store.get(s.owner,node)['attempts']==1
    finally:
        if child.poll() is None:child.terminate();child.wait(timeout=5)


@pytest.mark.parametrize('action',['merge','archive'])
def test_real_process_exit_after_effect_keeps_unknown_outcome_without_replay(setup,action):
    s=setup;node,path=reviewed(s);life=Lifecycle(s.graphs)
    if action=='archive':
        s.store.cancel(s.owner,node);s.graphs.seal(group(s)['id']);s.graphs.snapshot(group(s)['id'])
    key=life.request(group(s)['id'],node,action)
    code='''import os,sys
from unittest.mock import patch
from agent_execution.temporal_backend.dag_lifecycle import Lifecycle
from agent_execution.worktrees import WorktreeStore
life=Lifecycle()
if sys.argv[2]=='merge':
    original=os.replace
    def replace(source,target):
        original(source,target)
        if str(target)==sys.argv[3]:os._exit(17)
    with patch('agent_execution.coordinator.os.replace',side_effect=replace):life.execute(sys.argv[1])
else:
    original=WorktreeStore.archive
    def archive(*args,**kwargs):
        original(*args,**kwargs)
        os._exit(17)
    with patch.object(WorktreeStore,'archive',archive):life.execute(sys.argv[1])
'''
    child=subprocess.run([sys.executable,'-c',code,key,action,str(s.repo/'result.txt')],timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    assert child.returncode==17
    reap(s.graphs)
    assert life.get(key)['state']=='uncertain'
    if action=='merge':
        assert (s.repo/'result.txt').read_text()=='result'
        assert s.store.get(s.owner,node)['state']=='needs_resolution'
        stamp=(s.repo/'result.txt').stat().st_mtime_ns
        life.execute(key)
        assert (s.repo/'result.txt').stat().st_mtime_ns==stamp
    else:
        tree=WorktreeStore(s.repo,s.root/'worktrees')
        assert tree.get(s.owner,node)['state']=='archived'
        tree.restore(s.owner,node)
        life.execute(key)
        assert (path/'result.txt').read_text()=='result'


@pytest.mark.parametrize('scope', [{'identity':'other'}, {'coordinator_owner':'delegated'}, {'coordinator_node':'0'*32}])
def test_lifecycle_request_requires_exact_non_delegated_admin_owner(setup, scope):
    s = setup
    node, _ = reviewed(s)
    with execution_scope(**scope):
        with pytest.raises(PermissionError): Lifecycle(s.graphs).request(group(s)['id'], node, 'merge')
