"""Human decisions persisted outside model-controlled tool arguments."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid


class ApprovalRequired(PermissionError):
    def __init__(self, approval_id):
        self.approval_id = approval_id
        super().__init__(f"approval_required: {approval_id}; user must /approve {approval_id}, then retry the identical operation with approval_id")


def canonical(arguments):
    return json.dumps({k: v for k, v in arguments.items()
                       if k not in {"confirm", "approval_id", "memory", "episode_id"}},
                      sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


class ApprovalStore:
    def __init__(self, path=None):
        storage = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".local" / "share")
        self.path = Path(path or os.environ.get("AGENT_APPROVAL_DB") or storage / "PythonAI" / "approvals.db")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as conn, conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS approvals (
                    id TEXT PRIMARY KEY, identity TEXT NOT NULL, session TEXT NOT NULL,
                    action TEXT NOT NULL, arguments TEXT NOT NULL, digest TEXT NOT NULL,
                    expires REAL NOT NULL, state TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS approval_events (
                    id INTEGER PRIMARY KEY, approval_id TEXT, identity TEXT, session TEXT,
                    event TEXT NOT NULL, created REAL NOT NULL);
            """)

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def audit(conn, approval_id, identity, session, event):
        conn.execute("INSERT INTO approval_events(approval_id,identity,session,event,created) VALUES(?,?,?,?,?)",
                     (approval_id, identity, session, event, time.time()))

    def request(self, identity, session, action, arguments, ttl=300):
        data = canonical(arguments)
        digest = hashlib.sha256(data.encode()).hexdigest()
        with closing(self.connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT id FROM approvals WHERE identity=? AND session=? AND action=? AND digest=? AND state='pending' AND expires>?",
                               (identity, session, action, digest, time.time())).fetchone()
            if row:
                return row["id"]
            key = uuid.uuid4().hex
            conn.execute("INSERT INTO approvals VALUES(?,?,?,?,?,?,?,?)",
                         (key, identity, session, action, data, digest, time.time() + ttl, "pending"))
            self.audit(conn, key, identity, session, "requested")
            return key

    def decide(self, approval_id, identity, session, approve):
        with closing(self.connect()) as conn, conn:
            updated = conn.execute("UPDATE approvals SET state=? WHERE id=? AND identity=? AND session=? AND state='pending' AND expires>?",
                                   ("approved" if approve else "denied", approval_id, identity, session, time.time()))
            self.audit(conn, approval_id, identity, session,
                       ("approved" if approve else "denied") if updated.rowcount else "decision_rejected")
            accepted = updated.rowcount == 1
        if not accepted:
            raise PermissionError("Approval is missing, expired, decided, or belongs to another session")

    def consume(self, approval_id, identity, session, action, arguments):
        digest = hashlib.sha256(canonical(arguments).encode()).hexdigest()
        with closing(self.connect()) as conn, conn:
            updated = conn.execute("UPDATE approvals SET state='consumed' WHERE id=? AND identity=? AND session=? AND action=? AND digest=? AND state='approved' AND expires>?",
                                   (approval_id, identity, session, action, digest, time.time()))
            self.audit(conn, approval_id, identity, session, "consumed" if updated.rowcount else "consume_rejected")
            accepted = updated.rowcount == 1
        if not accepted:
            raise PermissionError("Approval does not authorize this identity, session, action or arguments; it may be expired or already used")

    def pending(self, identity, session):
        with closing(self.connect()) as conn:
            return [dict(row) for row in conn.execute("SELECT id,action,arguments,expires,state FROM approvals WHERE identity=? AND session=? AND state='pending' AND expires>?",
                                                      (identity, session, time.time()))]


def human_command(text, identity, session):
    """Only call from a human input transport, never register as a model tool."""
    parts = text.strip().split()
    if not parts or parts[0] not in {"/approve", "/deny", "/approvals"}:
        return None
    from permissions import current_role
    if current_role() != "admin":
        return "Permission denied: approval decisions require admin role."
    store = ApprovalStore()
    if parts[0] == "/approvals":
        return json.dumps(store.pending(identity, session), ensure_ascii=False)
    if len(parts) != 2:
        return "Usage: /approve <approval_id> or /deny <approval_id>"
    try:
        store.decide(parts[1], identity, session, parts[0] == "/approve")
        return "Approval decision saved. Retry the identical operation with this approval_id."
    except PermissionError as error:
        return str(error)
