"""Bounded subprocess execution with cancellation and process-tree cleanup."""
from __future__ import annotations

import math
import os
import signal
import subprocess
from typing import Callable


def command_timeout() -> float:
    value = float(os.environ.get("AGENT_COMMAND_TIMEOUT_SECONDS", "60"))
    if not math.isfinite(value) or value <= 0:
        raise ValueError("AGENT_COMMAND_TIMEOUT_SECONDS must be finite and positive")
    return value


def execution_mode() -> str:
    legacy = "container" if os.environ.get("AGENT_USE_SANDBOX", "").lower() in {"1", "true"} else "host"
    mode = os.environ.get("AGENT_EXECUTION_MODE", legacy).strip().lower()
    if mode not in {"host", "container"}:
        raise ValueError("AGENT_EXECUTION_MODE must be host or container")
    return mode


def terminate_tree(process: subprocess.Popen) -> None:
    if os.name == "nt":
        # Only target the child created by this runner, never a discovered system PID.
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.kill()


class WindowsJob:
    """Kill all descendants when a runner finishes, including an exited parent's children."""
    def __init__(self, process):
        import ctypes
        from ctypes import wintypes
        class BasicLimit(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                        ("PerJobUserTimeLimit", ctypes.c_longlong), ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t), ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD), ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]
        class IOCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in
                        ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                         "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]
        class ExtendedLimit(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BasicLimit), ("IoInfo", IOCounters),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        information = ExtendedLimit()
        information.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        try:
            if not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(information), ctypes.sizeof(information)):
                raise ctypes.WinError(ctypes.get_last_error())
            if not self.kernel.AssignProcessToJobObject(self.handle, wintypes.HANDLE(int(process._handle))):
                raise ctypes.WinError(ctypes.get_last_error())
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def run_process(command, *, cwd=None, env=None, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=None,
                check_cancelled: Callable[[], bool] | None = None,
                max_output_bytes=2 * 1024 * 1024, check=False) -> subprocess.CompletedProcess:
    from .policy import current_context
    import threading
    import time
    timeout = command_timeout() if timeout is None else float(timeout)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Command timeout must be finite and positive")
    if max_output_bytes <= 0:
        raise ValueError("Output limit must be positive")
    check_cancelled = check_cancelled or current_context().check_cancelled
    empty = "" if text else b""
    if check_cancelled and check_cancelled():
        message = "Task was cancelled before execution"
        return subprocess.CompletedProcess(command, 130, empty, message if text else message.encode())
    options = {"cwd": cwd, "env": env}
    if capture_output:
        options.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    else:
        options["start_new_session"] = True
    process = subprocess.Popen(command, **options)
    job = None
    stdout_buffer, stderr_buffer = bytearray(), bytearray()
    overflow = threading.Event()
    readers = []

    def read(stream, buffer):
        while True:
            chunk = stream.read(4096)
            if not chunk:
                return
            available = max_output_bytes - len(buffer)
            buffer.extend(chunk[:max(0, available)])
            if len(chunk) > available:
                overflow.set()

    code = None
    reason = ""
    try:
        if os.name == "nt":
            job = WindowsJob(process)
        for stream, buffer in ((process.stdout, stdout_buffer), (process.stderr, stderr_buffer)):
            if stream is not None:
                reader = threading.Thread(target=read, args=(stream, buffer), daemon=True)
                reader.start()
                readers.append(reader)
        deadline = time.monotonic() + timeout
        while True:
            cancelled = bool(check_cancelled and check_cancelled())
            if cancelled or time.monotonic() >= deadline or overflow.is_set():
                code = 130 if cancelled else 125 if overflow.is_set() else 124
                reason = "Task was cancelled" if cancelled else "Command output exceeded limit" if overflow.is_set() else f"Command timed out after {timeout:g} seconds"
                terminate_tree(process)
                break
            try:
                process.wait(timeout=0.05)
                break
            except subprocess.TimeoutExpired:
                pass
    finally:
        if process.poll() is None:
            terminate_tree(process)
        # Closing the job kills surviving descendants even if the parent already exited.
        if job is not None:
            job.close()
        elif os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait(timeout=5)
        for reader in readers:
            reader.join(timeout=5)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()
    if overflow.is_set() and code is None:
        code, reason = 125, "Command output exceeded limit"
    stdout, stderr = bytes(stdout_buffer), bytes(stderr_buffer)
    if reason:
        stderr += b"\n" + reason.encode("utf-8")
    if text:
        stdout, stderr = stdout.decode(encoding, errors), stderr.decode(encoding, errors)
    result = subprocess.CompletedProcess(command, code if code is not None else process.returncode, stdout, stderr)
    if check:
        result.check_returncode()
    return result
