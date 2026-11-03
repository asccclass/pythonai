from contextlib import closing
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time
from unittest.mock import patch

import pytest

from memory import MemoryStore
from memory_worker import MemoryReviewWorker
from agent_execution.memory_jobs import inspect_jobs, recover, renew, finish, LeaseLost
from agent_execution.memory_jobs.processes import process_start, owner_state, assert_legacy_workers_stopped


@pytest.fixture
def store(tmp_path):
    return MemoryStore(tmp_path/'memory.db')


def job(store):
    return store.add_memory_job('memory_review',store.start_episode())


def expire(store, key):
    with closing(store.connect()) as conn, conn:
        conn.execute('UPDATE memory_jobs SET lease_until=? WHERE id=?',(time.time()-1,key))


def test_claim_and_finish_are_fenced_across_store_instances(store):
    key = job(store)
    other = MemoryStore(store.db_path)
    assert store.claim_memory_job(key)
    assert not other.claim_memory_job(key)
    with pytest.raises(LeaseLost):
        other.complete_memory_job(key)
    row = store.memory_jobs()[0]
    assert row['lease_token'] and row['owner_pid']==os.getpid()
    assert row['heartbeat_at'] < row['lease_until']
    store.complete_memory_job(key)
    assert store.memory_jobs()[0]['lease_token'] is None


def test_expiry_does_not_allow_takeover_or_recovery_of_a_live_owner(store):
    key = job(store); assert store.claim_memory_job(key)
    expire(store,key)
    assert not renew(store,key,store._memory_job_token(key))
    with pytest.raises(LeaseLost):store.complete_memory_job(key)
    assert recover(store.db_path)=={'recovered':[],'backup':None}
    assert not MemoryStore(store.db_path).claim_memory_job(key)
    assert inspect_jobs(store.db_path)[0]['owner_state']=='running'


def test_blocked_provider_has_heartbeat_and_cannot_be_claimed_twice(store, monkeypatch):
    monkeypatch.setenv('MEMORY_JOB_LEASE_SECONDS','1')
    started, release = threading.Event(), threading.Event()
    key = job(store)
    class SlowClassifier:
        def assess_episode(self, events):
            from memory_classifier import MemoryCandidateDecision
            started.set()
            assert release.wait(5)
            return MemoryCandidateDecision(should_extract=False)
    worker = MemoryReviewWorker(async_mode=False)
    thread = threading.Thread(target=worker._process_item,
        args=(('memory_review',key,store,store.memory_jobs()[0]['episode_id'],SlowClassifier(),None,None),))
    thread.start()
    try:
        assert started.wait(3)
        initial = inspect_jobs(store.db_path)[0]['lease_until']
        time.sleep(1.2)
        current = inspect_jobs(store.db_path)[0]
        assert current['lease_until']>initial
        assert current['lease_until']>time.time()
        assert not MemoryStore(store.db_path).claim_memory_job(key)
    finally:
        release.set(); thread.join(5)
    assert not thread.is_alive()
    assert store.memory_jobs()[0]['status']=='completed'


def test_setup_exception_fails_job_without_stranding_processing(store):
    key = job(store)
    def broken():raise ValueError('classifier setup failed')
    worker = MemoryReviewWorker(async_mode=False)
    worker._process_item(('memory_review',key,store,store.memory_jobs()[0]['episode_id'],broken,None,None))
    assert store.memory_jobs()[0]['status']=='failed'
    assert store.memory_jobs()[0]['lease_token'] is None


def test_worker_return_after_heartbeat_loss_fails_instead_of_stranding_job(store,monkeypatch):
    from agent_execution.memory_jobs import release_after_execution
    key = job(store); store.claim_memory_job(key)
    token = store._memory_job_token(key)
    expire(store,key)
    with pytest.raises(LeaseLost):store.complete_memory_job(key)
    release_after_execution(store,key,token)
    assert store.memory_jobs()[0]['status']=='failed'
    # A late owner's finalizer cannot overwrite a replacement job token.
    with closing(store.connect()) as conn, conn:
        conn.execute("UPDATE memory_jobs SET status='processing',lease_token='replacement' WHERE id=?",(key,))
    release_after_execution(store,key,token)
    assert store.memory_jobs()[0]['lease_token']=='replacement'


def test_real_worker_crash_is_backed_up_then_failed_without_replay(store):
    key = job(store)
    code = '''import sys,time
from memory import MemoryStore
store=MemoryStore(sys.argv[1])
assert store.claim_memory_job(int(sys.argv[2]))
print('claimed',flush=True)
time.sleep(30)
'''
    child = subprocess.Popen([sys.executable,'-c',code,str(store.db_path),str(key)],
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,
        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    try:
        assert child.stdout.readline().strip()=='claimed'
        token = store.memory_jobs()[0]['lease_token']
        expire(store,key)
        assert recover(store.db_path)['recovered']==[]  # Still actually running.
    finally:
        child.terminate(); child.wait(timeout=10)
        child.stdout.close(); child.stderr.close()
    result = recover(store.db_path)
    assert result['recovered']==[key]
    assert store.memory_jobs()[0]['status']=='failed'
    assert store.pending_memory_jobs()==[]
    with closing(sqlite3.connect(result['backup'])) as conn:
        assert conn.execute('SELECT status FROM memory_jobs WHERE id=?',(key,)).fetchone()[0]=='processing'
        assert conn.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    with closing(store.connect()) as conn:
        assert conn.execute('SELECT backup_path FROM memory_job_recoveries WHERE job_id=?',(key,)).fetchone()[0]==result['backup']
    with pytest.raises(LeaseLost):finish(store,key,token,'completed')
    assert recover(store.db_path)['recovered']==[]


def test_process_identity_detects_pid_reuse_and_fails_closed(store):
    key = job(store); store.claim_memory_job(key)
    row = store.memory_jobs()[0]
    with patch('agent_execution.memory_jobs.processes.process_start',return_value='different-birth'):
        assert owner_state(row)=='stopped'
    expire(store,key)
    with patch('agent_execution.memory_jobs.processes.process_start',return_value=None):
        assert owner_state(row)=='unknown'
        assert recover(store.db_path)['recovered']==[]
    with patch('agent_execution.memory_jobs.processes.process_start',side_effect=PermissionError):
        assert owner_state(row)=='unknown'


def test_legacy_processing_requires_offline_proof_and_backup(store):
    key = job(store)
    with closing(store.connect()) as conn, conn:
        conn.execute("UPDATE memory_jobs SET status='processing' WHERE id=?",(key,))
    assert inspect_jobs(store.db_path)[0]['owner_state']=='legacy_unowned'
    assert recover(store.db_path)['recovered']==[]
    with pytest.raises(ValueError,match='stopped service PIDs'):
        recover(store.db_path,legacy=True)
    with pytest.raises(ValueError,match='running or cannot'):
        recover(store.db_path,legacy=True,stopped_pids=[os.getpid()])
    with patch('agent_execution.memory_jobs.assert_legacy_workers_stopped') as stopped:
        result = recover(store.db_path,legacy=True,stopped_pids=[123])
    assert stopped.call_count==2
    assert result['recovered']==[key]
    assert Path(result['backup']).exists()


def test_backup_failure_keeps_processing_and_has_no_recovery_receipt(store):
    key = job(store); store.claim_memory_job(key); expire(store,key)
    with patch('agent_execution.memory_jobs.owner_state',return_value='stopped'), \
         patch('agent_execution.memory_jobs.Path.mkdir',side_effect=OSError('backup disk unavailable')):
        with pytest.raises(OSError):recover(store.db_path)
    assert store.memory_jobs()[0]['status']=='processing'
    with closing(store.connect()) as conn:
        assert conn.execute('SELECT COUNT(*) FROM memory_job_recoveries').fetchone()[0]==0


def test_pending_review_poller_recovers_only_confirmed_dead_modern_jobs(store):
    key = job(store); store.claim_memory_job(key); expire(store,key)
    worker = MemoryReviewWorker(async_mode=False)
    with patch('agent_execution.memory_jobs.owner_state',return_value='stopped'):
        assert worker.enqueue_pending_reviews(store,None,None,None)==0
    assert store.memory_jobs()[0]['status']=='failed'
    assert list((store.db_path.parent/'memory-job-backups').glob('*.db'))


def test_old_schema_is_inspectable_and_migration_preserves_jobs(tmp_path):
    path = tmp_path/'legacy.db'
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute('''CREATE TABLE memory_jobs (id INTEGER PRIMARY KEY,job_type TEXT,episode_id INTEGER,
            status TEXT,payload TEXT,attempts INTEGER,last_error TEXT,next_attempt_at TEXT,
            created_at TEXT,updated_at TEXT)''')
        conn.execute("INSERT INTO memory_jobs VALUES(1,'memory_review',NULL,'processing','{}',1,NULL,NULL,'old','old')")
    assert inspect_jobs(path)[0]['owner_state']=='legacy_unowned'
    migrated = MemoryStore(path)
    assert migrated.memory_jobs()[0]['status']=='processing'
    assert migrated.memory_jobs()[0]['lease_token'] is None


def test_windows_legacy_inspection_rejects_any_remaining_service():
    if os.name!='nt':pytest.skip('Windows maintenance boundary')
    with patch('agent_execution.memory_jobs.processes.process_start',side_effect=ProcessLookupError), \
         patch('agent_execution.memory_jobs.processes.subprocess.run',return_value=type('Result',(),{
             'stdout':'[{"ProcessId":321,"ExecutablePath":"C:/Python/python.exe","CommandLine":"python communication_server.py"}]'})()):
        with pytest.raises(ValueError,match='321 must stop'):
            assert_legacy_workers_stopped([123])
