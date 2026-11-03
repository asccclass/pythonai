"""Leased memory jobs and conservative, backed-up crash recovery."""
from contextlib import closing, contextmanager
import json
import math
import os
from pathlib import Path
import socket
import sqlite3
import threading
import time
import uuid

from .processes import process_start, owner_state, assert_legacy_workers_stopped


class LeaseLost(RuntimeError):
    pass


def lease_seconds():
    value = float(os.environ.get('MEMORY_JOB_LEASE_SECONDS', '120'))
    if not math.isfinite(value) or value < 1:
        raise ValueError('MEMORY_JOB_LEASE_SECONDS must be finite and >= 1')
    return value


def migrate(conn):
    columns = {r[1] for r in conn.execute('PRAGMA table_info(memory_jobs)')}
    for name, kind in (
        ('lease_token', 'TEXT'), ('owner_pid', 'INTEGER'), ('owner_started', 'TEXT'),
        ('owner_host', 'TEXT'), ('heartbeat_at', 'REAL'), ('lease_until', 'REAL'),
    ):
        if name not in columns:
            conn.execute(f'ALTER TABLE memory_jobs ADD COLUMN {name} {kind}')
    conn.execute('''CREATE TABLE IF NOT EXISTS memory_job_recoveries (
        id TEXT PRIMARY KEY, job_id INTEGER NOT NULL, previous_token TEXT,
        recovered_at REAL NOT NULL, backup_path TEXT NOT NULL, reason TEXT NOT NULL)''')


def claim(store, job_id, token):
    seconds = lease_seconds()
    started = process_start(os.getpid())
    if started is None:
        raise RuntimeError('Cannot establish memory worker process identity')
    now = time.time()
    with closing(store.connect()) as conn, conn:
        return conn.execute('''UPDATE memory_jobs SET status='processing', attempts=attempts+1,
            lease_token=?,owner_pid=?,owner_started=?,owner_host=?,heartbeat_at=?,lease_until=?,
            updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='pending'
            AND (next_attempt_at IS NULL OR next_attempt_at<=CURRENT_TIMESTAMP)''',
            (token,os.getpid(),started,socket.gethostname(),now,now+seconds,job_id)).rowcount == 1


def renew(store, job_id, token):
    now = time.time()
    with closing(store.connect()) as conn, conn:
        return conn.execute('''UPDATE memory_jobs SET heartbeat_at=?,lease_until=?,updated_at=CURRENT_TIMESTAMP
            WHERE id=? AND status='processing' AND lease_token=? AND lease_until>?''',
            (now,now+lease_seconds(),job_id,token,now)).rowcount == 1


def finish(store, job_id, token, state, error=None, next_attempt=None):
    with closing(store.connect()) as conn, conn:
        changed = conn.execute('''UPDATE memory_jobs SET status=?,last_error=?,next_attempt_at=?,
            lease_token=NULL,owner_pid=NULL,owner_started=NULL,owner_host=NULL,
            heartbeat_at=NULL,lease_until=NULL,updated_at=CURRENT_TIMESTAMP
            WHERE id=? AND status='processing' AND lease_token=? AND lease_until>?''',
            (state,error,next_attempt,job_id,token,time.time())).rowcount
        if not changed:
            raise LeaseLost('Memory job lease expired or ownership changed')
    tokens = getattr(store._job_leases, 'tokens', {})
    if tokens.get(job_id)==token:
        tokens.pop(job_id,None)


def release_after_execution(store, job_id, token):
    """The original worker has returned: fail, never complete, an expired job.

    This is normal owner finalization, not takeover of a potentially live
    callback. A replaced token can never change the new owner's job.
    """
    with closing(store.connect()) as conn, conn:
        conn.execute('''UPDATE memory_jobs SET status='failed',
            last_error='Worker returned without a valid completion; partial memory work retained',
            next_attempt_at=NULL,lease_token=NULL,owner_pid=NULL,owner_started=NULL,owner_host=NULL,
            heartbeat_at=NULL,lease_until=NULL,updated_at=CURRENT_TIMESTAMP
            WHERE id=? AND status='processing' AND lease_token=?''',(job_id,token))
    tokens = getattr(store._job_leases,'tokens',{})
    if tokens.get(job_id)==token:
        tokens.pop(job_id,None)


@contextmanager
def heartbeat(store, job_id):
    """Keep a claimed job alive even while its provider call blocks."""
    token = store._memory_job_token(job_id)
    stopped = threading.Event()
    def beat():
        while not stopped.wait(lease_seconds()/3):
            try:
                if not renew(store, job_id, token):
                    return
            except Exception:
                # Never resurrect an expired lease. A live/unknown process still
                # blocks recovery and deletion until it actually terminates.
                return
    thread = threading.Thread(target=beat, name=f'memory-heartbeat-{job_id}', daemon=True)
    thread.start()
    try:
        yield
    finally:
        stopped.set()
        thread.join()


def inspect_jobs(path):
    """Read old and new databases without migrating or changing their contents."""
    path = Path(path).resolve()
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='memory_jobs'").fetchone():
            return []
        rows = [dict(r) for r in conn.execute("SELECT * FROM memory_jobs WHERE status='processing'")]
    result = []
    now = time.time()
    for row in rows:
        legacy = not row.get('lease_token')
        state = 'legacy_unowned' if legacy else owner_state(row)
        expired = (row.get('lease_until') or 0) <= now
        result.append({
            'kind':'memory-worker', 'job_id':row['id'], 'job_type':row['job_type'],
            'episode_id':row['episode_id'], 'status':row['status'], 'owner_state':state,
            'owner_pid':row.get('owner_pid'), 'heartbeat_at':row.get('heartbeat_at'),
            'lease_until':row.get('lease_until'), 'lease_expired':expired,
            'retryable':state=='running',
            'recovery_required':legacy or state in {'stopped','unknown'},
        })
    return result


def recover(path, *, legacy=False, stopped_pids=()):
    """Fail abandoned work; never replay possibly partially committed mutations.

    Expiry alone is insufficient. Legacy recovery additionally requires an
    offline maintenance window and explicitly verified stopped service PIDs.
    """
    path = Path(path)
    if path.is_symlink():
        raise ValueError('Recovery requires a regular database path')
    path = path.resolve(strict=True)
    if legacy:
        assert_legacy_workers_stopped(stopped_pids)
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('BEGIN IMMEDIATE')
        try:
            rows = [dict(r) for r in conn.execute("SELECT * FROM memory_jobs WHERE status='processing'")]
            def eligible(row):
                if not row.get('lease_token'):
                    return legacy
                return (row.get('lease_until') or 0)<=time.time() and owner_state(row)=='stopped'
            candidates = [row for row in rows if eligible(row)]
            if not candidates:
                conn.rollback()
                return {'recovered':[], 'backup':None}
            backup_dir = path.parent / 'memory-job-backups'
            backup_dir.mkdir(exist_ok=True)
            backup = backup_dir / (path.stem+'-'+uuid.uuid4().hex+'.db')
            # A separate read connection sees the exact committed pre-change
            # snapshot while BEGIN IMMEDIATE prevents another writer claiming.
            with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as source:
                with closing(sqlite3.connect(backup)) as target:
                    source.backup(target)
                    if target.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
                        raise RuntimeError('Memory recovery backup verification failed')
            if legacy:
                assert_legacy_workers_stopped(stopped_pids)
            migrate(conn)
            recovered = []
            for row in candidates:
                if not eligible(row):
                    continue
                reason = 'Owner confirmed stopped; partial memory work retained, automatic replay disabled'
                conn.execute('''UPDATE memory_jobs SET status='failed',last_error=?,next_attempt_at=NULL,
                    lease_token=NULL,owner_pid=NULL,owner_started=NULL,owner_host=NULL,
                    heartbeat_at=NULL,lease_until=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='processing' ''',
                    (reason,row['id']))
                conn.execute('INSERT INTO memory_job_recoveries VALUES(?,?,?,?,?,?)',
                    (uuid.uuid4().hex,row['id'],row.get('lease_token'),time.time(),str(backup),reason))
                recovered.append(row['id'])
            conn.commit()
            return {'recovered':recovered,'backup':str(backup)}
        except BaseException:
            conn.rollback()
            raise
