"""Persistent same-host references: erasure waits for real owners, never lease expiry."""
from contextlib import closing, contextmanager
import os
import uuid


def process_stamp(pid):
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes as w
        kernel = ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.OpenProcess.argtypes = [w.DWORD,w.BOOL,w.DWORD]
        kernel.OpenProcess.restype = w.HANDLE
        kernel.CloseHandle.argtypes = [w.HANDLE]
        handle = kernel.OpenProcess(0x1000,False,pid)
        if not handle:
            if ctypes.get_last_error()==87:
                return None
            raise PermissionError('Cannot establish reference process liveness')
        try:
            code = w.DWORD()
            kernel.GetExitCodeProcess.argtypes = [w.HANDLE,ctypes.POINTER(w.DWORD)]
            if not kernel.GetExitCodeProcess(handle,ctypes.byref(code)):
                raise OSError('Cannot read reference process status')
            if code.value!=259:
                return None
            values = [w.FILETIME() for _ in range(4)]
            kernel.GetProcessTimes.argtypes = [w.HANDLE]+[ctypes.POINTER(w.FILETIME)]*4
            if not kernel.GetProcessTimes(handle,*(ctypes.byref(v) for v in values)):
                raise OSError('Cannot read reference process identity')
            return str((values[0].dwHighDateTime<<32)|values[0].dwLowDateTime)
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid,0)  # Unix only; never use os.kill(pid,0) on Windows.
    except ProcessLookupError:
        return None
    return str(pid)  # Reused Unix PIDs conservatively keep the reference blocked.


def install(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS temporal_local_references(
        id TEXT PRIMARY KEY,submission_id TEXT NOT NULL,pid INTEGER NOT NULL,process TEXT NOT NULL)""")


@contextmanager
def reference(store,row):
    token = uuid.uuid4().hex
    stamp = process_stamp(os.getpid())
    with closing(store.tasks.connect()) as conn,conn:
        conn.execute('BEGIN IMMEDIATE')
        from .retention import assert_not_erased
        assert_not_erased(conn,row['owner'],row['task_id'])
        current = conn.execute('SELECT state FROM temporal_submissions WHERE id=?',(row['id'],)).fetchone()
        if not current or current[0]=='erasing':
            raise PermissionError('Temporal reference was erased or fenced')
        install(conn)
        from agent_execution.delivery import positive_limit,CapacityError
        if conn.execute('SELECT count(*) FROM temporal_local_references').fetchone()[0]>=positive_limit('AGENT_TEMPORAL_OPERATION_LIMIT',100000):
            raise CapacityError('Local reference capacity reached')
        conn.execute('INSERT INTO temporal_local_references VALUES(?,?,?,?)',(token,row['id'],os.getpid(),stamp))
    try:
        yield
    finally:
        with closing(store.tasks.connect()) as conn,conn:
            conn.execute('DELETE FROM temporal_local_references WHERE id=?',(token,))


def blockers(conn,key):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='temporal_local_references'").fetchone():
        return False
    active = False
    for token,pid,stamp in conn.execute('SELECT id,pid,process FROM temporal_local_references WHERE submission_id=?',(key,)).fetchall():
        if process_stamp(pid)==stamp:
            active = True
        else:
            conn.execute('DELETE FROM temporal_local_references WHERE id=?',(token,))
    return active
