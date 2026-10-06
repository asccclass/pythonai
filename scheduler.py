import sqlite3
import json
import os
import uuid
from pathlib import Path
from datetime import datetime, timezone
from contextlib import closing
from croniter import croniter
import traceback
import subprocess

DEFAULT_SCHEDULER_DB = Path(__file__).resolve().parent / "communication" / "scheduler.db"

def scheduler_db_path() -> Path:
    return Path(os.environ.get("SCHEDULER_DB_PATH", DEFAULT_SCHEDULER_DB))

def now_utc() -> datetime:
    return datetime.now(timezone.utc)

class SchedulerStore:
    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path) if db_path is not None else scheduler_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with closing(self.connect()) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS scheduled_jobs (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT,
                    cron_expression TEXT NOT NULL,
                    job_type TEXT NOT NULL,
                    job_payload TEXT NOT NULL,
                    is_enabled BOOLEAN DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    next_run_at TIMESTAMP
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS scheduled_job_runs (
                    id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    completed_at TIMESTAMP,
                    output TEXT,
                    error_message TEXT,
                    FOREIGN KEY (job_id) REFERENCES scheduled_jobs(id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS scheduled_job_locks (
                    job_id TEXT PRIMARY KEY,
                    locked_by TEXT NOT NULL,
                    locked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    expires_at TIMESTAMP NOT NULL,
                    FOREIGN KEY (job_id) REFERENCES scheduled_jobs(id)
                )
                """
            )
            connection.commit()

    def add_job(self, name: str, cron_expression: str, job_type: str, job_payload: dict, description: str = "") -> str:
        job_id = str(uuid.uuid4())
        
        base_time = now_utc()
        iter = croniter(cron_expression, base_time)
        next_run_at = iter.get_next(datetime).isoformat()
        
        with closing(self.connect()) as connection:
            connection.execute(
                """
                INSERT INTO scheduled_jobs (id, name, description, cron_expression, job_type, job_payload, next_run_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (job_id, name, description, cron_expression, job_type, json.dumps(job_payload), next_run_at)
            )
            connection.commit()
        return job_id

    def list_jobs(self) -> list[dict]:
        with closing(self.connect()) as connection:
            rows = connection.execute("SELECT * FROM scheduled_jobs").fetchall()
            return [dict(row) for row in rows]
            
    def get_due_jobs(self) -> list[dict]:
        now_str = now_utc().isoformat()
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM scheduled_jobs 
                WHERE is_enabled = 1 AND next_run_at <= ?
                """,
                (now_str,)
            ).fetchall()
            return [dict(row) for row in rows]

    def acquire_lock(self, job_id: str, locked_by: str, timeout_seconds: int = 300) -> bool:
        with closing(self.connect()) as connection:
            try:
                connection.execute(
                    "INSERT INTO scheduled_job_locks (job_id, locked_by, locked_at, expires_at) VALUES (?, ?, datetime('now'), datetime('now', '+{} seconds'))".format(timeout_seconds),
                    (job_id, locked_by)
                )
                connection.commit()
                return True
            except sqlite3.IntegrityError:
                row = connection.execute("SELECT 1 FROM scheduled_job_locks WHERE job_id = ? AND expires_at < datetime('now')", (job_id,)).fetchone()
                if row:
                    connection.execute(
                        "UPDATE scheduled_job_locks SET locked_by = ?, locked_at = datetime('now'), expires_at = datetime('now', '+{} seconds') WHERE job_id = ?".format(timeout_seconds),
                        (locked_by, job_id)
                    )
                    connection.commit()
                    return True
                return False

    def release_lock(self, job_id: str) -> None:
        with closing(self.connect()) as connection:
            connection.execute("DELETE FROM scheduled_job_locks WHERE job_id = ?", (job_id,))
            connection.commit()

    def record_run_start(self, job_id: str) -> str:
        run_id = str(uuid.uuid4())
        with closing(self.connect()) as connection:
            connection.execute(
                "INSERT INTO scheduled_job_runs (id, job_id, status) VALUES (?, ?, 'running')",
                (run_id, job_id)
            )
            connection.commit()
        return run_id

    def record_run_complete(self, run_id: str, status: str, output: str = "", error_message: str = "") -> None:
        with closing(self.connect()) as connection:
            connection.execute(
                "UPDATE scheduled_job_runs SET status = ?, completed_at = CURRENT_TIMESTAMP, output = ?, error_message = ? WHERE id = ?",
                (status, output, error_message, run_id)
            )
            connection.commit()

    def update_next_run(self, job_id: str, cron_expression: str) -> None:
        base_time = now_utc()
        iter = croniter(cron_expression, base_time)
        next_run_at = iter.get_next(datetime).isoformat()
        with closing(self.connect()) as connection:
            connection.execute("UPDATE scheduled_jobs SET next_run_at = ? WHERE id = ?", (next_run_at, job_id))
            connection.commit()


def execute_job(job: dict) -> tuple[str, str, str]:
    job_type = job["job_type"]
    payload = json.loads(job["job_payload"])
    
    try:
        if job_type == "command":
            cmd = payload.get("command")
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
            if result.returncode == 0:
                return "completed", result.stdout, ""
            else:
                return "failed", result.stdout, result.stderr
        elif job_type == "maintenance.summary" or (
            job_type == "maintenance" and payload.get("task") == "daily_summary"
        ):
            from summary import generate_daily_summary
            telegram_chat_id = payload.get("telegram_chat_id")
            output, error = generate_daily_summary(telegram_chat_id=telegram_chat_id)
            if error:
                return "failed", output, error
            return "completed", output, ""
        else:
            return "failed", "", f"Unsupported job_type: {job_type}"
    except Exception as e:
        return "failed", "", traceback.format_exc()

def process_due_jobs():
    store = SchedulerStore()
    due_jobs = store.get_due_jobs()
    worker_id = f"worker-{os.getpid()}"
    
    for job in due_jobs:
        job_id = job["id"]
        if store.acquire_lock(job_id, worker_id):
            try:
                run_id = store.record_run_start(job_id)
                status, output, error_message = execute_job(job)
                store.record_run_complete(run_id, status, output, error_message)
            finally:
                store.update_next_run(job_id, job["cron_expression"])
                store.release_lock(job_id)
import threading
import time
import traceback

class SchedulerWorker:
    def __init__(self, check_interval: int = 60):
        self.check_interval = check_interval
        self._stop_event = threading.Event()
        self._thread = None

    def start(self):
        if self._thread is None:
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._run, daemon=True, name="SchedulerWorker")
            self._thread.start()
            print("Scheduler worker started.")

    def stop(self):
        if self._thread is not None:
            self._stop_event.set()
            self._thread.join()
            self._thread = None
            print("Scheduler worker stopped.")

    def _run(self):
        # We need to import process_due_jobs inside to avoid circular imports if any, 
        # but process_due_jobs is in the same module so we can just call it
        from scheduler import process_due_jobs
        while not self._stop_event.is_set():
            try:
                process_due_jobs()
            except Exception as e:
                print(f"Error in scheduler worker loop: {e}")
                traceback.print_exc()
            
            # Wait for check_interval but check stop_event periodically
            self._stop_event.wait(self.check_interval)
