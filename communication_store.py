from __future__ import annotations

from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import hashlib
import re
import time
from typing import Any

from communication_models import AgentCommand, InboundAttachment, InboundMessage


DEFAULT_COMMUNICATION_DB = Path(__file__).resolve().parent / "communication" / "communication.db"


def communication_db_path() -> Path:
    return Path(os.environ.get("COMMUNICATION_DB_PATH", DEFAULT_COMMUNICATION_DB))


class CommunicationStore:
    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path) if db_path is not None else communication_db_path()
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
                CREATE TABLE IF NOT EXISTS comm_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    platform TEXT NOT NULL,
                    platform_message_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    sender_id TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    text TEXT NOT NULL,
                    raw_payload TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'received',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(platform, platform_message_id, direction)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_command_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    command_id TEXT NOT NULL UNIQUE,
                    platform TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    sender_id TEXT NOT NULL,
                    text TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    requires_confirmation INTEGER NOT NULL DEFAULT 0,
                    source_message_id INTEGER,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    result_text TEXT,
                    last_error TEXT,
                    state TEXT,
                    suspended_tool_call_id TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (source_message_id) REFERENCES comm_messages(id)
                )
                """
            )
            self._ensure_agent_command_job_columns(connection)
            connection.executescript('''
                CREATE TABLE IF NOT EXISTS comm_attachments (
                    command_id TEXT NOT NULL, ordinal INTEGER NOT NULL, owner TEXT NOT NULL,
                    filename TEXT NOT NULL, mime TEXT NOT NULL, file_id TEXT, data BLOB NOT NULL,
                    digest TEXT NOT NULL, size INTEGER NOT NULL, expires REAL NOT NULL,
                    PRIMARY KEY(command_id,ordinal));
                CREATE TABLE IF NOT EXISTS comm_deliveries (
                    id TEXT PRIMARY KEY, platform TEXT NOT NULL, conversation TEXT NOT NULL,
                    sender TEXT NOT NULL, project TEXT NOT NULL, text TEXT NOT NULL,
                    reply_context TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
                    lease REAL, error TEXT, expires REAL NOT NULL);
            ''')
            connection.commit()

    @staticmethod
    def attachment_owner(owner):
        return json.dumps([owner.platform, owner.sender_id, owner.conversation_id, owner.project], separators=(',', ':'))

    def save_attachments(self, connection, message):
        from agent_execution.delivery import positive_limit, CapacityError
        attachments = message.attachments
        if len(attachments) > 16 or sum(a.size_bytes for a in attachments) > 32*1024*1024:
            raise CapacityError('Attachment batch exceeds count/size limit')
        for attachment in attachments:
            if not isinstance(attachment.data, bytes) or attachment.size_bytes > positive_limit('COMM_ATTACHMENT_MAX_BYTES',20*1024*1024):
                raise CapacityError('Attachment exceeds file size limit')
            if not re.fullmatch(r'[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+', attachment.content_type):
                raise ValueError('Attachment MIME type is invalid')
            signatures = {'image/png':b'\x89PNG\r\n\x1a\n','image/jpeg':b'\xff\xd8\xff','image/gif':b'GIF', 'application/pdf':b'%PDF-'}
            signature = signatures.get(attachment.content_type)
            if signature and not attachment.data.startswith(signature):
                raise ValueError('Attachment content does not match declared MIME type')
            if attachment.content_type == 'image/webp' and not (attachment.data.startswith(b'RIFF') and attachment.data[8:12] == b'WEBP'):
                raise ValueError('Attachment content does not match declared MIME type')
            if not attachment.filename or len(attachment.filename) > 255 or any(c in attachment.filename for c in '/\\\x00') or attachment.filename in {'.','..'}:
                raise ValueError('Attachment filename is invalid')
        if connection.execute('SELECT 1 FROM comm_attachments WHERE command_id=?', (message.idempotency_key,)).fetchone():
            return
        if connection.execute('SELECT COUNT(*) FROM comm_attachments').fetchone()[0]+len(attachments) > positive_limit('COMM_ATTACHMENT_COUNT',10000):
            raise CapacityError('Attachment metadata capacity reached')
        used = connection.execute('SELECT COALESCE(SUM(size),0) FROM comm_attachments').fetchone()[0]
        if used + sum(a.size_bytes for a in attachments) > positive_limit('COMM_ATTACHMENT_TOTAL_BYTES',128*1024*1024):
            raise CapacityError('Attachment storage capacity reached')
        for index, attachment in enumerate(attachments):
            connection.execute('INSERT INTO comm_attachments VALUES(?,?,?,?,?,?,?,?,?,?)',
                (message.idempotency_key,index,self.attachment_owner(message),attachment.filename,attachment.content_type,
                 attachment.platform_file_id,attachment.data,hashlib.sha256(attachment.data).hexdigest(),attachment.size_bytes,
                 time.time()+positive_limit('COMM_ATTACHMENT_TTL',7*86400)))

    def load_attachments(self, command_id, owner):
        with closing(self.connect()) as connection:
            job = connection.execute('SELECT platform,sender_id,conversation_id,project FROM agent_command_jobs WHERE command_id=?', (command_id,)).fetchone()
            if job is None or self.attachment_owner(owner) != json.dumps([job['platform'],job['sender_id'],job['conversation_id'],job['project']], separators=(',', ':')):
                raise PermissionError('Attachment belongs to another identity/session/project')
            rows = connection.execute('SELECT * FROM comm_attachments WHERE command_id=? AND owner=? ORDER BY ordinal',
                                      (command_id,self.attachment_owner(owner))).fetchall()
        result = []
        for row in rows:
            if row['expires'] <= time.time():
                raise ValueError('Attachment expired; upload it again')
            data = bytes(row['data'])
            if len(data) != row['size'] or hashlib.sha256(data).hexdigest() != row['digest']:
                raise ValueError('Attachment integrity check failed')
            result.append(InboundAttachment(row['filename'],row['mime'],data,row['file_id']))
        return tuple(result)

    def cleanup_expired(self):
        with closing(self.connect()) as connection, connection:
            # Preserve expired attachment metadata as a tombstone, never silently run without a file.
            connection.execute("UPDATE comm_attachments SET data=X'', size=0 WHERE expires<=? AND size<>0", (time.time(),))
            connection.execute("UPDATE comm_deliveries SET state='uncertain', error='Delivery lease expired; verify platform before retry' WHERE state='sending' AND lease<=?", (time.time(),))
            connection.execute('DELETE FROM comm_deliveries WHERE expires<=? AND state IN (\'sent\',\'pending\')', (time.time(),))

    def _ensure_agent_command_job_columns(self, connection: sqlite3.Connection) -> None:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(agent_command_jobs)").fetchall()
        }
        if "state" not in columns:
            connection.execute("ALTER TABLE agent_command_jobs ADD COLUMN state TEXT")
        if "suspended_tool_call_id" not in columns:
            connection.execute("ALTER TABLE agent_command_jobs ADD COLUMN suspended_tool_call_id TEXT")
        if "delivery_ready" not in columns:
            connection.execute("ALTER TABLE agent_command_jobs ADD COLUMN delivery_ready INTEGER NOT NULL DEFAULT 1")
        if "project" not in columns:
            connection.execute("ALTER TABLE agent_command_jobs ADD COLUMN project TEXT NOT NULL DEFAULT 'default'")

    @staticmethod
    def owner_filter(owner):
        if owner is None:
            return "1=1", ()
        return "platform=? AND conversation_id=? AND sender_id=? AND project=?", (owner.platform, owner.conversation_id, owner.sender_id, owner.project)

    def cancel_session(self, owner):
        clause, values = self.owner_filter(owner)
        with closing(self.connect()) as connection, connection:
            connection.execute(f"UPDATE agent_command_jobs SET status=CASE WHEN status IN ('running','cancel_requested') THEN 'cancel_requested' ELSE 'cancelled' END, state=NULL, suspended_tool_call_id=NULL WHERE {clause} AND command_id<>? AND status IN ('pending','running','cancel_requested','suspended')",
                               (*values, owner.command_id))

    def release_discord_interaction(self, interaction_id):
        with closing(self.connect()) as connection, connection:
            connection.execute("UPDATE agent_command_jobs SET delivery_ready=1 WHERE platform='discord' AND source_message_id IN (SELECT id FROM comm_messages WHERE platform='discord' AND platform_message_id=? AND direction='inbound')",
                               (str(interaction_id),))

    def reply_context(self, source_message_id: int | None) -> dict[str, Any]:
        with closing(self.connect()) as connection:
            row = connection.execute("SELECT raw_payload FROM comm_messages WHERE id=? AND platform='discord' AND direction='inbound'",
                                     (source_message_id,)).fetchone()
        if row is None:
            return {}
        payload = json.loads(row["raw_payload"])
        return {key: payload[key] for key in ("token", "application_id", "interaction_id", "received_at") if key in payload}

    def record_inbound_message(self, message: InboundMessage) -> tuple[int, bool]:
        with closing(self.connect()) as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO comm_messages (
                    platform, platform_message_id, conversation_id, sender_id,
                    direction, text, raw_payload
                )
                VALUES (?, ?, ?, ?, 'inbound', ?, ?)
                """,
                (
                    message.platform,
                    message.platform_message_id,
                    message.conversation_id,
                    message.sender_id,
                    message.text,
                    json.dumps(raw_payload_with_attachment_metadata(message), ensure_ascii=False),
                ),
            )
            inserted = cursor.rowcount > 0
            if inserted:
                message_id = int(cursor.lastrowid)
            else:
                row = connection.execute(
                    """
                    SELECT id FROM comm_messages
                    WHERE platform = ? AND platform_message_id = ? AND direction = 'inbound'
                    """,
                    (message.platform, message.platform_message_id),
                ).fetchone()
                message_id = int(row["id"])
            connection.commit()
            return message_id, inserted

    def enqueue_command(self, message: InboundMessage, source_message_id: int) -> AgentCommand:
        command_id = message.idempotency_key
        with closing(self.connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            self.save_attachments(connection, message)
            connection.execute(
                """
                INSERT OR IGNORE INTO agent_command_jobs (
                    command_id, platform, conversation_id, sender_id, text, source_message_id, delivery_ready, project
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    command_id,
                    message.platform,
                    message.conversation_id,
                    message.sender_id,
                    message.text,
                    source_message_id,
                    0 if message.platform == "discord" else 1,
                    message.project,
                ),
            )
            connection.commit()
        return self.command_by_id(command_id, include_attachments=True)

    def ingest_inbound_message(self, message: InboundMessage) -> tuple[AgentCommand, bool]:
        with closing(self.connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            old = connection.execute('SELECT * FROM agent_command_jobs WHERE command_id=?', (message.idempotency_key,)).fetchone()
            if old is not None:
                if self.attachment_owner(message) != self.attachment_owner(_command_from_row(old)):
                    raise PermissionError('Duplicate event belongs to another identity/session/project')
                return _command_from_row(old), False
            self.save_attachments(connection, message)
            connection.execute("INSERT OR IGNORE INTO comm_messages(platform,platform_message_id,conversation_id,sender_id,direction,text,raw_payload) VALUES(?,?,?,?,'inbound',?,?)",
                (message.platform,message.platform_message_id,message.conversation_id,message.sender_id,message.text,json.dumps(raw_payload_with_attachment_metadata(message),ensure_ascii=False)))
            message_id = connection.execute("SELECT id FROM comm_messages WHERE platform=? AND platform_message_id=? AND direction='inbound'", (message.platform,message.platform_message_id)).fetchone()[0]
            connection.execute('INSERT INTO agent_command_jobs(command_id,platform,conversation_id,sender_id,text,source_message_id,delivery_ready,project) VALUES(?,?,?,?,?,?,?,?)',
                (message.idempotency_key,message.platform,message.conversation_id,message.sender_id,message.text,message_id,0 if message.platform=='discord' else 1,message.project))
        return self.command_by_id(message.idempotency_key, include_attachments=True), True

    def pending_commands(self, limit: int = 10) -> list[AgentCommand]:
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """
                SELECT command_id, platform, conversation_id, sender_id, text, status,
                       requires_confirmation, source_message_id, project
                FROM agent_command_jobs
                WHERE status = 'pending' AND delivery_ready=1
                ORDER BY id ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [_command_from_row(row) for row in rows]

    def recoverable_commands(self):
        with closing(self.connect()) as connection:
            return [_command_from_row(row) for row in connection.execute("SELECT * FROM agent_command_jobs WHERE status IN ('running','recovery_required') ORDER BY id LIMIT 100")]

    def set_recovery_state(self, command_id, state):
        with closing(self.connect()) as connection, connection:
            connection.execute("UPDATE agent_command_jobs SET status=?,updated_at=CURRENT_TIMESTAMP WHERE command_id=? AND status IN ('running','recovery_required')", (state,command_id))

    def command_by_id(self, command_id: str, include_attachments=False) -> AgentCommand:
        with closing(self.connect()) as connection:
            row = connection.execute(
                """
                SELECT command_id, platform, conversation_id, sender_id, text, status,
                       requires_confirmation, source_message_id, project
                FROM agent_command_jobs
                WHERE command_id = ?
                """,
                (command_id,),
            ).fetchone()
        if row is None:
            raise KeyError(command_id)
        command = _command_from_row(row)
        if include_attachments:
            from dataclasses import replace
            command = replace(command, attachments=self.load_attachments(command_id, command))
        return command

    def mark_command_running(self, command_id: str) -> bool:
        with closing(self.connect()) as connection, connection:
            cursor = connection.execute("UPDATE agent_command_jobs SET status='running', attempts=attempts+1, updated_at=CURRENT_TIMESTAMP WHERE command_id=? AND status IN ('pending','suspended')",
                                        (command_id,))
            return cursor.rowcount == 1

    def complete_command(self, command_id: str, result_text: str) -> None:
        self._update_command(command_id, "completed", result_text=result_text)

    def fail_command(self, command_id: str, error: str) -> None:
        self._update_command(command_id, "failed", last_error=error)
        
    def cancel_command(self, command_id: str, owner=None) -> bool:
        clause, values = self.owner_filter(owner)
        with closing(self.connect()) as connection, connection:
            cursor = connection.execute(f"UPDATE agent_command_jobs SET status=CASE WHEN status='running' THEN 'cancel_requested' ELSE 'cancelled' END WHERE command_id=? AND {clause} AND status IN ('pending','running','suspended')",
                                        (command_id, *values))
            return cursor.rowcount == 1

    def query_jobs_status(self, limit: int = 5, owner=None) -> list[AgentCommand]:
        clause, values = self.owner_filter(owner)
        with closing(self.connect()) as connection:
            rows = connection.execute(
                f"""
                SELECT command_id, platform, conversation_id, sender_id, text, status,
                       requires_confirmation, source_message_id, project
                FROM agent_command_jobs
                WHERE {clause}
                ORDER BY id DESC
                LIMIT ?
                """,
                (*values, limit),
            ).fetchall()
        return [_command_from_row(row) for row in rows]
        
    def is_cancel_requested(self, command_id: str) -> bool:
        with closing(self.connect()) as connection:
            row = connection.execute("SELECT status FROM agent_command_jobs WHERE command_id = ?", (command_id,)).fetchone()
            return row is not None and row["status"] in {"cancel_requested", "cancelled"}
            
    def resolve_command_id(self, prefix: str, owner=None) -> str | None:
        if not prefix:
            return None
        escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        clause, values = self.owner_filter(owner)
        with closing(self.connect()) as connection:
            rows = connection.execute(f"SELECT command_id FROM agent_command_jobs WHERE command_id LIKE ? ESCAPE '\\' AND {clause} LIMIT 2", (f"{escaped}%", *values)).fetchall()
            return rows[0]["command_id"] if len(rows) == 1 else None

    def suspend_command(self, command_id: str, state_json: str, tool_call_id: str) -> None:
        with closing(self.connect()) as connection:
            connection.execute(
                "UPDATE agent_command_jobs SET status = 'suspended', state = ?, suspended_tool_call_id = ?, updated_at = CURRENT_TIMESTAMP WHERE command_id = ? AND status IN ('pending','running')",
                (state_json, tool_call_id, command_id)
            )
            connection.commit()

    def get_suspended_command(self, conversation_id: str, owner=None) -> tuple[str, str, str] | None:
        """Returns (command_id, state_json, suspended_tool_call_id) of the most recent suspended job."""
        with closing(self.connect()) as connection:
            clause, values = self.owner_filter(owner)
            row = connection.execute(
                f"SELECT command_id, state, suspended_tool_call_id FROM agent_command_jobs WHERE conversation_id = ? AND {clause} AND status = 'suspended' ORDER BY id DESC LIMIT 1",
                (conversation_id, *values)
            ).fetchone()
            if row and row["state"] and row["suspended_tool_call_id"]:
                return (row["command_id"], row["state"], row["suspended_tool_call_id"])
        return None

    def clear_suspended_command(self, conversation_id: str, owner=None) -> int:
        """Cancel suspended jobs so the next message cannot resume old context."""
        with closing(self.connect()) as connection:
            clause, values = self.owner_filter(owner)
            cursor = connection.execute(
                f"UPDATE agent_command_jobs SET status = 'cancelled', state = NULL, suspended_tool_call_id = NULL, updated_at = CURRENT_TIMESTAMP WHERE conversation_id = ? AND {clause} AND status = 'suspended'",
                (conversation_id, *values),
            )
            connection.commit()
            return cursor.rowcount

    def record_outbound_message(
        self,
        platform: str,
        conversation_id: str,
        text: str,
        platform_message_id: str,
        sender_id: str = "agent",
    ) -> int:
        with closing(self.connect()) as connection:
            cursor = connection.execute(
                """
                INSERT INTO comm_messages (
                    platform, platform_message_id, conversation_id, sender_id,
                    direction, text, raw_payload, status
                )
                VALUES (?, ?, ?, ?, 'outbound', ?, '{}', 'sent')
                """,
                (platform, platform_message_id, conversation_id, sender_id, text),
            )
            connection.commit()
            return int(cursor.lastrowid)

    def queue_delivery(self, command, text, reply_context, is_error=False):
        from agent_execution.delivery import CapacityError, positive_limit
        # Include content in the key: a suspended question and its final reply are separate deliveries.
        digest = hashlib.sha256(text.encode()).hexdigest()
        key = f"{command.command_id}:{'error' if is_error else 'outbound'}:{digest}"
        with closing(self.connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            if connection.execute('SELECT 1 FROM comm_deliveries WHERE id=?', (key,)).fetchone():
                return key
            if len(text.encode()) > 2*1024*1024 or connection.execute('SELECT COUNT(*) FROM comm_deliveries').fetchone()[0] >= positive_limit('COMM_DELIVERY_LIMIT',10000):
                raise CapacityError('Platform delivery capacity reached')
            used = connection.execute('SELECT COALESCE(SUM(length(CAST(text AS BLOB))+length(CAST(reply_context AS BLOB))),0) FROM comm_deliveries').fetchone()[0]
            if used+len(text.encode())+len(json.dumps(reply_context).encode()) > positive_limit('COMM_DELIVERY_BYTES',128*1024*1024):
                raise CapacityError('Platform delivery storage capacity reached')
            connection.execute('INSERT INTO comm_deliveries(id,platform,conversation,sender,project,text,reply_context,expires) VALUES(?,?,?,?,?,?,?,?)',
                (key,command.platform,command.conversation_id,command.sender_id,command.project,text,json.dumps(reply_context),time.time()+positive_limit('COMM_DELIVERY_TTL',7*86400)))
        return key

    def pending_deliveries(self, delivery_id=None):
        with closing(self.connect()) as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM comm_deliveries WHERE state='pending' AND expires>? AND (? IS NULL OR id=?) ORDER BY rowid LIMIT 20", (time.time(),delivery_id,delivery_id))]

    def claim_delivery(self, key):
        with closing(self.connect()) as connection, connection:
            return connection.execute("UPDATE comm_deliveries SET state='sending',lease=? WHERE id=? AND state='pending' AND expires>?", (time.time()+120,key,time.time())).rowcount == 1

    def finish_delivery(self, key, state, error=None):
        with closing(self.connect()) as connection, connection:
            row = connection.execute("SELECT * FROM comm_deliveries WHERE id=? AND state='sending'", (key,)).fetchone()
            if row is None:
                return
            connection.execute('UPDATE comm_deliveries SET state=?,error=?,lease=NULL WHERE id=?', (state,error,key))
            if state == 'sent':
                connection.execute("INSERT OR IGNORE INTO comm_messages(platform,platform_message_id,conversation_id,sender_id,direction,text,status) VALUES(?,?,?,'agent','outbound',?,'sent')", (row['platform'],key,row['conversation'],row['text']))

    def delivery_status(self, owner):
        with closing(self.connect()) as connection:
            return [dict(row) for row in connection.execute('SELECT id,state,error,expires FROM comm_deliveries WHERE platform=? AND conversation=? AND sender=? AND project=? ORDER BY rowid DESC LIMIT 20', (owner.platform,owner.conversation_id,owner.sender_id,owner.project))]

    def recent_messages(self, limit: int = 20) -> list[dict[str, Any]]:
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """
                SELECT id, platform, platform_message_id, conversation_id, sender_id,
                       direction, text, raw_payload, status, created_at, updated_at
                FROM comm_messages
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        messages = []
        for row in rows:
            item = dict(row)
            item["raw_payload"] = json.loads(item["raw_payload"] or "{}")
            messages.append(item)
        return messages

    def _update_command(
        self,
        command_id: str,
        status: str,
        *,
        increment_attempts: bool = False,
        result_text: str | None = None,
        last_error: str | None = None,
    ) -> None:
        attempts_sql = "attempts = attempts + 1," if increment_attempts else ""
        with closing(self.connect()) as connection:
            connection.execute(
                f"""
                UPDATE agent_command_jobs
                SET status = CASE WHEN status IN ('cancel_requested','cancelled') AND ? IN ('completed','failed','running') THEN 'cancelled' ELSE ? END,
                    {attempts_sql}
                    result_text = COALESCE(?, result_text),
                    last_error = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE command_id = ?
                """,
                (status, status, result_text, last_error, command_id),
            )
            connection.commit()


def _command_from_row(row: sqlite3.Row) -> AgentCommand:
    command_id = str(row["command_id"])
    return AgentCommand(
        command_id=command_id,
        platform=str(row["platform"]),
        conversation_id=str(row["conversation_id"]),
        sender_id=str(row["sender_id"]),
        text=str(row["text"]),
        status=str(row["status"]),
        requires_confirmation=bool(row["requires_confirmation"]),
        source_message_id=int(row["source_message_id"]) if row["source_message_id"] is not None else None,
        project=str(row["project"]),
    )


def raw_payload_with_attachment_metadata(message: InboundMessage) -> dict[str, Any]:
    payload = dict(message.raw_payload or {})
    if message.attachments:
        payload["attachments"] = [
            {
                "filename": attachment.filename,
                "content_type": attachment.content_type,
                "size_bytes": attachment.size_bytes,
                "platform_file_id": attachment.platform_file_id,
            }
            for attachment in message.attachments
        ]
    return payload
