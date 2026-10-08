from __future__ import annotations

from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
from typing import Any

from communication_models import AgentCommand, InboundAttachment, InboundMessage


DEFAULT_COMMUNICATION_DB = Path(__file__).resolve().parent / "communication" / "communication.db"
_PENDING_ATTACHMENTS: dict[str, tuple[InboundAttachment, ...]] = {}


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
            connection.commit()

    def _ensure_agent_command_job_columns(self, connection: sqlite3.Connection) -> None:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(agent_command_jobs)").fetchall()
        }
        if "state" not in columns:
            connection.execute("ALTER TABLE agent_command_jobs ADD COLUMN state TEXT")
        if "suspended_tool_call_id" not in columns:
            connection.execute("ALTER TABLE agent_command_jobs ADD COLUMN suspended_tool_call_id TEXT")

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
        if message.attachments:
            _PENDING_ATTACHMENTS[command_id] = tuple(message.attachments)
        with closing(self.connect()) as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO agent_command_jobs (
                    command_id, platform, conversation_id, sender_id, text, source_message_id
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    command_id,
                    message.platform,
                    message.conversation_id,
                    message.sender_id,
                    message.text,
                    source_message_id,
                ),
            )
            connection.commit()
        return self.command_by_id(command_id)

    def ingest_inbound_message(self, message: InboundMessage) -> tuple[AgentCommand, bool]:
        message_id, inserted = self.record_inbound_message(message)
        command = self.enqueue_command(message, message_id)
        return command, inserted

    def pending_commands(self, limit: int = 10) -> list[AgentCommand]:
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """
                SELECT command_id, platform, conversation_id, sender_id, text, status,
                       requires_confirmation, source_message_id
                FROM agent_command_jobs
                WHERE status = 'pending'
                ORDER BY id ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [_command_from_row(row) for row in rows]

    def command_by_id(self, command_id: str) -> AgentCommand:
        with closing(self.connect()) as connection:
            row = connection.execute(
                """
                SELECT command_id, platform, conversation_id, sender_id, text, status,
                       requires_confirmation, source_message_id
                FROM agent_command_jobs
                WHERE command_id = ?
                """,
                (command_id,),
            ).fetchone()
        if row is None:
            raise KeyError(command_id)
        return _command_from_row(row)

    def mark_command_running(self, command_id: str) -> None:
        self._update_command(command_id, "running", increment_attempts=True)

    def complete_command(self, command_id: str, result_text: str) -> None:
        self._update_command(command_id, "completed", result_text=result_text)

    def fail_command(self, command_id: str, error: str) -> None:
        self._update_command(command_id, "failed", last_error=error)
        
    def cancel_command(self, command_id: str) -> bool:
        # returns True if it was cancelled/requested successfully
        with closing(self.connect()) as connection:
            row = connection.execute("SELECT status FROM agent_command_jobs WHERE command_id = ?", (command_id,)).fetchone()
            if not row:
                return False
            status = row["status"]
            if status == "pending":
                self._update_command(command_id, "cancelled")
                return True
            elif status == "running":
                self._update_command(command_id, "cancel_requested")
                return True
            return False

    def query_jobs_status(self, limit: int = 5) -> list[AgentCommand]:
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """
                SELECT command_id, platform, conversation_id, sender_id, text, status,
                       requires_confirmation, source_message_id
                FROM agent_command_jobs
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [_command_from_row(row) for row in rows]
        
    def is_cancel_requested(self, command_id: str) -> bool:
        with closing(self.connect()) as connection:
            row = connection.execute("SELECT status FROM agent_command_jobs WHERE command_id = ?", (command_id,)).fetchone()
            return row is not None and row["status"] == "cancel_requested"
            
    def resolve_command_id(self, prefix: str) -> str | None:
        with closing(self.connect()) as connection:
            row = connection.execute("SELECT command_id FROM agent_command_jobs WHERE command_id LIKE ?", (f"{prefix}%",)).fetchone()
            return row["command_id"] if row else None

    def suspend_command(self, command_id: str, state_json: str, tool_call_id: str) -> None:
        with closing(self.connect()) as connection:
            connection.execute(
                "UPDATE agent_command_jobs SET status = 'suspended', state = ?, suspended_tool_call_id = ?, updated_at = CURRENT_TIMESTAMP WHERE command_id = ?",
                (state_json, tool_call_id, command_id)
            )
            connection.commit()

    def get_suspended_command(self, conversation_id: str) -> tuple[str, str, str] | None:
        """Returns (command_id, state_json, suspended_tool_call_id) of the most recent suspended job."""
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT command_id, state, suspended_tool_call_id FROM agent_command_jobs WHERE conversation_id = ? AND status = 'suspended' ORDER BY id DESC LIMIT 1",
                (conversation_id,)
            ).fetchone()
            if row and row["state"] and row["suspended_tool_call_id"]:
                return (row["command_id"], row["state"], row["suspended_tool_call_id"])
        return None

    def clear_suspended_command(self, conversation_id: str) -> int:
        """Cancel suspended jobs so the next message cannot resume old context."""
        with closing(self.connect()) as connection:
            cursor = connection.execute(
                "UPDATE agent_command_jobs SET status = 'cancelled', state = NULL, suspended_tool_call_id = NULL, updated_at = CURRENT_TIMESTAMP WHERE conversation_id = ? AND status = 'suspended'",
                (conversation_id,),
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
                SET status = ?,
                    {attempts_sql}
                    result_text = COALESCE(?, result_text),
                    last_error = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE command_id = ?
                """,
                (status, result_text, last_error, command_id),
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
        attachments=_PENDING_ATTACHMENTS.get(command_id, ()),
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
