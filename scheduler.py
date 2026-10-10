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
import shlex
from agent_execution import execute_tool, execution_scope
from agent_execution.policy import current_context, policy_tool
from permissions import current_role, set_current_role

from agent_execution.scheduling.store import SchedulerStore, scheduler_db_path, now_utc
from agent_execution.scheduling.engine import process_due_jobs

def execute_job(job: dict) -> tuple[str, str, str]:
    try:
        return execute_tool("schedule_job", {}, lambda: _execute_job_as_owner(job))
    except Exception as error:
        return "failed", "", str(error)


def _execute_job_as_owner(job):
    from agent_execution.scheduling.engine import execute_payload
    return execute_payload(job)

def _execute_job(job: dict) -> tuple[str, str, str]:
    try:
        job_type = job["job_type"]
        payload = json.loads(job["job_payload"])
        if job_type == "command":
            from base import auto_approve_command_runs, run_command
            cmd = payload.get("command")
            if isinstance(cmd, str):
                # Legacy CLI strings remain supported as argv, not shell programs.
                if any(character in cmd for character in "&|;<>\r\n`$"):
                    raise ValueError("Scheduled commands must use argv; shell operators are not allowed")
                cmd = shlex.split(cmd)
            if not isinstance(cmd, list) or not cmd or not all(isinstance(item, str) for item in cmd):
                raise ValueError("command must be a non-empty argv list")
            if os.name == "nt" and cmd[0].lower() == "echo":
                cmd = ["cmd", "/c", *cmd]
            arguments = {"command": cmd, "cwd": payload.get("cwd"), "env_file": payload.get("env_file"),
                         "trusted_asset_roots":list(current_context().trusted_assets)}
            with auto_approve_command_runs():
                result = execute_tool("run_command", arguments, run_command)
            if result.returncode == 0:
                return "completed", result.stdout, ""
            else:
                return "failed", result.stdout, result.stderr
        elif job_type == "maintenance.summary" or (
            job_type == "maintenance" and payload.get("task") == "daily_summary"
        ):
            from summary import generate_daily_summary
            telegram_chat_id = payload.get("telegram_chat_id")
            output, error = execute_tool("maintenance.summary", {"telegram_chat_id": telegram_chat_id}, generate_daily_summary)
            if error:
                return "failed", output, error
            return "completed", output, ""
        else:
            return "failed", "", f"Unsupported job_type: {job_type}"
    except Exception as e:
        return "failed", "", type(e).__name__

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
                process_due_jobs(check_cancelled=self._stop_event.is_set)
            except Exception as e:
                print(f"Error in scheduler worker loop: {e}")
                traceback.print_exc()
            
            # Wait for check_interval but check stop_event periodically
            self._stop_event.wait(self.check_interval)
