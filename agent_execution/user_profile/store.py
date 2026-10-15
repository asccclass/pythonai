"""Identity-wide profile storage; deliberately independent of conversation memory."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import time
import uuid

from agent_execution.policy import current_context
from agent_execution.sessions import tenant_memory_path


class ProfileConflict(RuntimeError):
    pass


class ProfileStore:
    def __init__(self, path=None):
        self.path = Path(path or tenant_memory_path(current_context().identity, "__profile__", "").with_name("profile.db"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as conn, conn:
            schema = conn.execute('PRAGMA user_version').fetchone()[0]
            if schema not in (0, 1):
                raise ValueError('Unsupported profile schema')
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS profile_meta (id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL);
                INSERT OR IGNORE INTO profile_meta VALUES(1,0);
                CREATE TABLE IF NOT EXISTS profile_entries (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, text TEXT NOT NULL,
                    evidence TEXT NOT NULL, updated REAL NOT NULL);
                PRAGMA user_version=1;
            ''')

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def snapshot(self):
        with closing(self.connect()) as conn, conn:
            conn.execute('BEGIN')
            version = conn.execute('SELECT version FROM profile_meta WHERE id=1').fetchone()[0]
            entries = [dict(row) for row in conn.execute('SELECT * FROM profile_entries ORDER BY updated,id')]
            return version, entries

    def apply(self, version, changes):
        with closing(self.connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            if conn.execute('SELECT version FROM profile_meta WHERE id=1').fetchone()[0] != version:
                raise ProfileConflict('Profile changed concurrently; please repeat the update.')
            for change in changes:
                if change['action'] == 'delete':
                    conn.execute('DELETE FROM profile_entries WHERE id=?', (change['id'],))
                elif change['action'] == 'update':
                    conn.execute('UPDATE profile_entries SET kind=?,text=?,evidence=?,updated=? WHERE id=?',
                                 (change['kind'], change['text'], change['evidence'], time.time(), change['id']))
                else:
                    exists = conn.execute('SELECT 1 FROM profile_entries WHERE kind=? AND text=?',
                                          (change['kind'], change['text'])).fetchone()
                    if not exists:
                        conn.execute('INSERT INTO profile_entries VALUES(?,?,?,?,?)',
                                     (uuid.uuid4().hex, change['kind'], change['text'], change['evidence'], time.time()))
            rows = conn.execute('SELECT text,evidence FROM profile_entries').fetchall()
            if len(rows) > 100 or sum(len(row[0]) + len(row[1]) for row in rows) > 20000:
                raise ValueError('Profile capacity reached; remove obsolete entries with /profile.')
            conn.execute('UPDATE profile_meta SET version=version+1 WHERE id=1')

    def command(self, text):
        parts = text.strip().split()
        if not parts or parts[0] != '/profile':
            return None
        version, entries = self.snapshot()
        if len(parts) == 1:
            return 'Profile（此登入身分，跨對話／專案共用）\n' + (json.dumps(entries, ensure_ascii=False, indent=2) if entries else '尚無自動記錄。')
        if parts[1:] == ['clear']:
            self.apply(version, [{'action': 'delete', 'id': entry['id']} for entry in entries])
            return '已清除自動 profile；本機手動 profile.md 及其他記憶系統須另行管理。'
        if len(parts) == 3 and parts[1] == 'forget':
            if parts[2] not in {entry['id'] for entry in entries}:
                return 'Profile 項目不存在。'
            self.apply(version, [{'action': 'delete', 'id': parts[2]}])
            return '已移除此 profile 項目。'
        return '用法：/profile | /profile forget <完整 id> | /profile clear'
