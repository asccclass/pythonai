"""Lost liveness stops cooperating subprocesses and cannot publish success."""
import os
from pathlib import Path
import sys
import threading
import time
from unittest.mock import patch

import pytest
pytest.importorskip('temporalio')

from agent_execution import execution_scope
from agent_execution.process import run_process
from agent_execution.temporal_backend.activity_guard import ActivityLeaseLost, activity_guard, cancelled


def test_heartbeat_failure_latches_and_cleans_monitor(monkeypatch):
    monkeypatch.setattr('agent_execution.temporal_backend.activity_guard.HEARTBEAT_SECONDS', .01)
    with patch('temporalio.activity.heartbeat', side_effect=RuntimeError('lease lost')), \
         patch('temporalio.activity.is_cancelled', return_value=False):
        with pytest.raises(ActivityLeaseLost):
            with activity_guard():
                deadline = time.monotonic() + 3
                while not cancelled() and time.monotonic() < deadline:
                    time.sleep(.01)
                assert cancelled()
        assert not cancelled()  # ContextVar reset, no poisoning the next Activity.
    assert not any(t.name == 'temporal-activity-heartbeat' for t in threading.enumerate())


def test_guard_checks_sdk_cancel_before_publishing_result():
    with patch('temporalio.activity.is_cancelled', return_value=True), pytest.raises(InterruptedError):
        with activity_guard():
            pass


@pytest.mark.parametrize('reason', ['cancel', 'heartbeat'])
def test_liveness_loss_kills_actual_process_tree(tmp_path, monkeypatch, reason):
    marker = tmp_path / 'descendant.pid'
    code = ('import subprocess,sys,time; from pathlib import Path; '
            'p=subprocess.Popen([sys.executable,"-c","import time; time.sleep(30)"]); '
            'Path(sys.argv[1]).write_text(str(p.pid)); time.sleep(30)')
    monkeypatch.setattr('agent_execution.temporal_backend.activity_guard.HEARTBEAT_SECONDS', .01)
    def ready():
        return marker.exists() and marker.stat().st_size > 0
    def beat():
        if reason == 'heartbeat' and ready():
            raise RuntimeError('lease lost')
    def sdk_cancelled():
        return reason == 'cancel' and ready()
    result = None
    with patch('temporalio.activity.heartbeat', side_effect=beat), \
         patch('temporalio.activity.is_cancelled', side_effect=sdk_cancelled):
        with pytest.raises(ActivityLeaseLost if reason == 'heartbeat' else InterruptedError):
            with activity_guard(), execution_scope(check_cancelled=cancelled):
                result = run_process([sys.executable, '-c', code, str(marker)], timeout=10)
    if reason == 'heartbeat':
        assert result is None  # Callback raises uncertain, after process finally cleanup.
    else:
        assert result is not None and result.returncode == 130
    pid = int(marker.read_text())
    if os.name == 'nt':
        from memory_worker import windows_pid_exists
        assert not windows_pid_exists(pid)
    else:
        # Descendants may remain zombies until the host reaps them.
        status = Path('/proc') / str(pid) / 'status'
        assert not status.exists() or 'State:\tZ' in status.read_text()


@pytest.mark.skipif(os.name != 'nt', reason='Windows Job Object ownership on process exit')
def test_worker_process_exit_reaps_owned_process_tree(tmp_path):
    import subprocess
    from memory_worker import windows_pid_exists
    marker = tmp_path / 'crash-descendant.pid'
    child_code = ('import subprocess,sys,time; from pathlib import Path; '
                  'p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(30)"]); '
                  'Path(sys.argv[1]).write_text(str(p.pid)); time.sleep(30)')
    worker_code = ('from agent_execution.process import run_process; import sys; '
                   'run_process([sys.executable,"-c",sys.argv[1],sys.argv[2]],timeout=30)')
    process = subprocess.Popen([sys.executable, '-c', worker_code, child_code, str(marker)],
                               cwd=Path(__file__).resolve().parents[3],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        deadline = time.monotonic() + 10
        while not (marker.exists() and marker.stat().st_size):
            assert process.poll() is None
            assert time.monotonic() < deadline
            time.sleep(.05)
        pid = int(marker.read_text())
        assert windows_pid_exists(pid)
        # Terminate only our Worker process: OS handle closure must reap its Job.
        process.terminate()
        process.wait(timeout=5)
        deadline = time.monotonic() + 5
        while windows_pid_exists(pid) and time.monotonic() < deadline:
            time.sleep(.05)
        assert not windows_pid_exists(pid)
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=5)
