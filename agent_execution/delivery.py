"""Durable, owner-scoped WebSocket event history and input deduplication."""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import time


class CapacityError(RuntimeError):
    pass


def positive_limit(name, default):
    value = int(os.environ.get(name, default))
    if value <= 0:
        raise ValueError(f'{name} must be positive')
    return value


class EventStore:
    def __init__(self, path=None):
        root = Path(os.environ.get('LOCALAPPDATA') or Path.home()/'.local'/'share')/'PythonAI'
        self.path = Path(path or os.environ.get('COMPANION_EVENT_DB') or root/'companion.db')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as conn, conn:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS ws_sessions (
                    owner TEXT PRIMARY KEY, seq INTEGER NOT NULL DEFAULT 0,
                    floor INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS ws_events (
                    owner TEXT NOT NULL, seq INTEGER NOT NULL, message TEXT NOT NULL,
                    size INTEGER NOT NULL, expires REAL NOT NULL, PRIMARY KEY(owner,seq));
                CREATE TABLE IF NOT EXISTS ws_inputs (
                    owner TEXT NOT NULL, id TEXT NOT NULL, digest TEXT NOT NULL,
                    status TEXT NOT NULL, expires REAL NOT NULL, PRIMARY KEY(owner,id));
            ''')

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def owner(user, project, device):
        return json.dumps([user, project, device], ensure_ascii=False, separators=(',', ':'))

    def cleanup(self, conn):
        # Keep the floor even after expiry so clients detect lost history.
        conn.execute('''UPDATE ws_sessions SET floor=MAX(floor,COALESCE(
            (SELECT MAX(seq) FROM ws_events WHERE owner=ws_sessions.owner AND expires<=?),0))''', (time.time(),))
        conn.execute('DELETE FROM ws_events WHERE expires<=?', (time.time(),))
        conn.execute('DELETE FROM ws_inputs WHERE expires<=?', (time.time(),))

    def snapshot(self, owner):
        with closing(self.connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            self.cleanup(conn)
            row = conn.execute('SELECT seq,floor FROM ws_sessions WHERE owner=?', (owner,)).fetchone()
            messages = [json.loads(r['message']) for r in conn.execute(
                'SELECT message FROM ws_events WHERE owner=? ORDER BY seq', (owner,))]
            return (row['seq'], row['floor'], messages) if row else (0, 0, [])

    def append(self, owner, message):
        with closing(self.connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            self.cleanup(conn)
            if not conn.execute('SELECT 1 FROM ws_sessions WHERE owner=?', (owner,)).fetchone():
                if conn.execute('SELECT COUNT(*) FROM ws_sessions').fetchone()[0] >= positive_limit('COMPANION_MAX_SESSIONS', 10000):
                    raise CapacityError('Companion session capacity reached')
                conn.execute('INSERT INTO ws_sessions(owner) VALUES(?)', (owner,))
            seq = conn.execute('SELECT seq FROM ws_sessions WHERE owner=?', (owner,)).fetchone()[0] + 1
            result = {**message, 'seq':seq}
            data = json.dumps(result, ensure_ascii=False)
            size = len(data.encode())
            count, used = conn.execute('SELECT COUNT(*),COALESCE(SUM(size),0) FROM ws_events WHERE owner=?', (owner,)).fetchone()
            total = conn.execute('SELECT COALESCE(SUM(size),0) FROM ws_events').fetchone()[0]
            if count >= positive_limit('COMPANION_OUTBOX_LIMIT', 200) or size > 2*1024*1024 or used+size > 20*1024*1024 or total+size > positive_limit('COMPANION_EVENT_BYTES', 128*1024*1024):
                raise CapacityError('Reliable outbox full; acknowledge events before continuing')
            conn.execute('INSERT INTO ws_events VALUES(?,?,?,?,?)', (owner,seq,data,size,time.time()+positive_limit('COMPANION_EVENT_TTL',86400)))
            conn.execute('UPDATE ws_sessions SET seq=? WHERE owner=?', (seq,owner))
            return result

    def ack(self, owner, seq):
        with closing(self.connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT seq FROM ws_sessions WHERE owner=?', (owner,)).fetchone()
            if seq < 0 or seq > (row['seq'] if row else 0):
                raise ValueError('ACK sequence is outside this session history')
            conn.execute('DELETE FROM ws_events WHERE owner=? AND seq<=?', (owner,seq))
            conn.execute('UPDATE ws_sessions SET floor=MAX(floor,?) WHERE owner=?', (seq,owner))

    def accept_input(self, owner, key, text):
        import hashlib
        digest = hashlib.sha256(text.encode()).hexdigest()
        with closing(self.connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            self.cleanup(conn)
            existing = conn.execute('SELECT digest,status FROM ws_inputs WHERE owner=? AND id=?', (owner,key)).fetchone()
            if existing:
                if existing['digest'] != digest:
                    raise ValueError('Input ID cannot be reused with different content')
                return existing['status']
            if conn.execute('SELECT COUNT(*) FROM ws_inputs').fetchone()[0] >= positive_limit('COMPANION_INPUT_LIMIT', 10000):
                raise CapacityError('Companion input deduplication capacity reached')
            # Persist before any execution. A crash leaves an uncertain request, never silently replayed.
            conn.execute('INSERT INTO ws_inputs VALUES(?,?,?,?,?)', (owner,key,digest,'uncertain',time.time()+positive_limit('COMPANION_INPUT_TTL',7*86400)))
            return None

    def finish_input(self, owner, key, status):
        with closing(self.connect()) as conn, conn:
            conn.execute('UPDATE ws_inputs SET status=? WHERE owner=? AND id=?', (status,owner,key))
