import pytest
import os
import tempfile
import sqlite3
from scheduler import SchedulerStore, process_due_jobs, execute_job
from datetime import datetime, timezone
import json

@pytest.fixture
def temp_db():
    fd, path = tempfile.mkstemp()
    os.close(fd)
    os.environ["SCHEDULER_DB_PATH"] = path
    yield path
    try:
        os.remove(path)
    except PermissionError:
        pass

def test_scheduler_store_initialization(temp_db):
    store = SchedulerStore()
    assert os.path.exists(temp_db)
    
    # check tables exist
    with store.connect() as conn:
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        table_names = [t["name"] for t in tables]
        assert "scheduled_jobs" in table_names
        assert "scheduled_job_runs" in table_names
        assert "scheduled_job_locks" in table_names

def test_add_job(temp_db):
    store = SchedulerStore()
    job_id = store.add_job(
        name="test_job",
        cron_expression="* * * * *",
        job_type="command",
        job_payload={"command": "echo test"}
    )
    assert job_id is not None
    
    jobs = store.list_jobs()
    assert len(jobs) == 1
    assert jobs[0]["name"] == "test_job"
    assert jobs[0]["cron_expression"] == "* * * * *"
    
def test_get_due_jobs(temp_db):
    store = SchedulerStore()
    
    # Add job that is past due
    job_id = store.add_job("test", "* * * * *", "command", {})
    with store.connect() as conn:
        conn.execute("UPDATE scheduled_jobs SET next_run_at = '2000-01-01T00:00:00+00:00' WHERE id = ?", (job_id,))
        conn.commit()
        
    due_jobs = store.get_due_jobs()
    assert len(due_jobs) == 1
    assert due_jobs[0]["id"] == job_id

def test_acquire_lock(temp_db):
    store = SchedulerStore()
    job_id = store.add_job("test", "* * * * *", "command", {})
    
    # Lock succeeds initially
    assert store.acquire_lock(job_id, "worker-1") == True
    
    # Lock fails if already locked
    assert store.acquire_lock(job_id, "worker-2") == False
    
    store.release_lock(job_id)
    assert store.acquire_lock(job_id, "worker-3") == True

def test_execute_job_command():
    job = {
        "job_type": "command",
        "job_payload": json.dumps({"command": "echo hello"})
    }
    status, output, error = execute_job(job)
    assert status == "completed"
    assert "hello" in output

def test_execute_job_maintenance(monkeypatch):
    import summary

    monkeypatch.setattr(summary, "generate_daily_summary", lambda telegram_chat_id=None: ("daily report", ""))
    job = {
        "job_type": "maintenance",
        "job_payload": json.dumps({"task": "daily_summary"})
    }
    status, output, error = execute_job(job)
    assert status == "completed"
    assert output == "daily report"

def test_execute_job_maintenance_summary(monkeypatch):
    import summary

    monkeypatch.setattr(summary, "generate_daily_summary", lambda telegram_chat_id=None: ("daily report", ""))
    job = {
        "job_type": "maintenance.summary",
        "job_payload": json.dumps({})
    }
    status, output, error = execute_job(job)
    assert status == "completed"
    assert output == "daily report"
