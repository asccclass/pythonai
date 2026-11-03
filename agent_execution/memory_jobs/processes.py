"""Process identity checks fail closed, including PID reuse and access denial."""
import os
import socket
import subprocess
import json
import re
from pathlib import Path


def process_start(pid):
    """Return a stable birth marker; None means identity cannot be established."""
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)]*4
        handle = kernel.OpenProcess(0x1000,False,pid)
        if not handle:
            if ctypes.get_last_error()==87:
                raise ProcessLookupError(pid)
            return None
        try:
            times = [wintypes.FILETIME() for _ in range(4)]
            if not kernel.GetProcessTimes(handle,*(ctypes.byref(t) for t in times)):
                return None
            code = wintypes.DWORD()
            kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE,ctypes.POINTER(wintypes.DWORD)]
            if not kernel.GetExitCodeProcess(handle,ctypes.byref(code)):
                return None
            if code.value!=259:
                raise ProcessLookupError(pid)
            return str((times[0].dwHighDateTime<<32)|times[0].dwLowDateTime)
        finally:
            kernel.CloseHandle(handle)
    try:
        stat = Path(f'/proc/{pid}/stat').read_text()
        boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        return boot+':'+stat[stat.rfind(')')+2:].split()[19]
    except FileNotFoundError:
        raise ProcessLookupError(pid) from None
    except OSError:
        return None


def owner_state(row):
    if row.get('owner_host') != socket.gethostname() or not row.get('owner_pid') or not row.get('owner_started'):
        return 'unknown'
    try:
        current = process_start(row['owner_pid'])
    except ProcessLookupError:
        return 'stopped'
    except (OSError,ValueError):
        return 'unknown'
    if current is None:
        return 'unknown'
    return 'running' if current==row['owner_started'] else 'stopped'


def assert_legacy_workers_stopped(pids):
    if not pids:
        raise ValueError('Legacy recovery requires stopped service PIDs')
    for pid in pids:
        if pid<=0:
            raise ValueError('Invalid service PID')
        try:
            process_start(pid)
        except ProcessLookupError:
            continue
        raise ValueError(f'Service PID {pid} is running or cannot be verified stopped')
    if os.name!='nt':
        raise ValueError('Legacy offline recovery currently requires Windows service inspection')
    script = "Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object { $_.Name -match '^python(w)?([0-9.]*)?\\.exe$' } | Select-Object ProcessId,ExecutablePath,CommandLine | ConvertTo-Json -Compress"
    result = subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',script],
        capture_output=True,text=True,timeout=20,check=True,
        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    records = json.loads(result.stdout or '[]')
    if isinstance(records,dict):
        records = [records]
    project = Path(__file__).resolve().parents[2]
    for record in records:
        if record['ProcessId']==os.getpid():
            continue
        command = record.get('CommandLine')
        executable = record.get('ExecutablePath')
        if not command or not executable:
            raise ValueError('Cannot verify all Python processes; legacy recovery refused')
        if record['ProcessId']==os.getppid() and '-m agent_execution.memory_jobs' in command:
            continue  # Windows venv's launcher for this maintenance process.
        if (str(project).lower() in executable.lower() or
                re.search(r'(?:communication_server\.py|(?:^|[\\/\s])server\.py|agent_execution\.temporal_backend)',command,re.I)):
            raise ValueError(f'PythonAI worker process {record["ProcessId"]} must stop before legacy recovery')
