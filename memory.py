from __future__ import annotations

from dataclasses import asdict, is_dataclass
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
from typing import Any


DEFAULT_MEMORY_DB = Path(__file__).resolve().parent / "memory.db"


def memory_db_path() -> Path:
    return Path(os.environ.get("MEMORY_DB_PATH", DEFAULT_MEMORY_DB))


class MemoryStore:
    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path) if db_path is not None else memory_db_path()
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
                CREATE TABLE IF NOT EXISTS episodes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    ended_at TEXT,
                    status TEXT NOT NULL DEFAULT 'running',
                    summary TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS episode_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    episode_id INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    role TEXT,
                    content TEXT,
                    metadata TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (episode_id) REFERENCES episodes(id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS semantic_memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    subject TEXT NOT NULL,
                    predicate TEXT NOT NULL,
                    object TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 0.5,
                    source_event_id INTEGER NOT NULL,
                    embedding TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    expires_at TEXT,
                    superseded_by INTEGER,
                    archived_at TEXT,
                    archive_reason TEXT,
                    FOREIGN KEY (source_event_id) REFERENCES episode_events(id),
                    FOREIGN KEY (superseded_by) REFERENCES semantic_memories(id)
                )
                """
            )
            try:
                connection.execute("ALTER TABLE semantic_memories ADD COLUMN embedding TEXT")
            except sqlite3.OperationalError:
                pass
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS procedures (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_type TEXT NOT NULL,
                    context_pattern TEXT NOT NULL,
                    steps TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 0.5,
                    success_count INTEGER NOT NULL DEFAULT 0,
                    failure_count INTEGER NOT NULL DEFAULT 0,
                    source_episode_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    last_success_at TEXT,
                    archived_at TEXT,
                    archive_reason TEXT,
                    FOREIGN KEY (source_episode_id) REFERENCES episodes(id)
                )
                """
            )
            connection.commit()

    def start_episode(self) -> int:
        with closing(self.connect()) as connection:
            cursor = connection.execute("INSERT INTO episodes DEFAULT VALUES")
            connection.commit()
            return int(cursor.lastrowid)

    def finish_episode(self, episode_id: int, status: str = "completed", summary: str | None = None) -> None:
        with closing(self.connect()) as connection:
            connection.execute(
                """
                UPDATE episodes
                SET ended_at = CURRENT_TIMESTAMP, status = ?, summary = ?
                WHERE id = ?
                """,
                (status, summary, episode_id),
            )
            connection.commit()

    def add_event(
        self,
        episode_id: int,
        event_type: str,
        role: str | None = None,
        content: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        metadata_json = json.dumps(_jsonable(metadata or {}), ensure_ascii=False)
        with closing(self.connect()) as connection:
            cursor = connection.execute(
                """
                INSERT INTO episode_events (episode_id, event_type, role, content, metadata)
                VALUES (?, ?, ?, ?, ?)
                """,
                (episode_id, event_type, role, content, metadata_json),
            )
            connection.commit()
            return int(cursor.lastrowid)

    def recent_events(self, limit: int = 20) -> list[dict[str, Any]]:
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """
                SELECT id, episode_id, event_type, role, content, metadata, created_at
                FROM episode_events
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [_event_from_row(row) for row in rows]

    def episode_events(self, episode_id: int) -> list[dict[str, Any]]:
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """
                SELECT id, episode_id, event_type, role, content, metadata, created_at
                FROM episode_events
                WHERE episode_id = ?
                ORDER BY id ASC
                """,
                (episode_id,),
            ).fetchall()
        return [_event_from_row(row) for row in rows]

    def add_semantic_memory(
        self,
        subject: str,
        predicate: str,
        object_value: str,
        source_event_id: int,
        confidence: float = 0.5,
        expires_at: str | None = None,
        embedding: list[float] | str | None = None,
    ) -> int:
        embedding_json = json.dumps(embedding) if isinstance(embedding, list) else embedding
        with closing(self.connect()) as connection:
            cursor = connection.execute(
                """
                INSERT INTO semantic_memories (
                    subject, predicate, object, confidence, source_event_id, expires_at, embedding
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (subject, predicate, object_value, confidence, source_event_id, expires_at, embedding_json),
            )
            connection.commit()
            return int(cursor.lastrowid)

    def update_semantic_confidence(self, memory_id: int, confidence: float) -> None:
        with closing(self.connect()) as connection:
            connection.execute(
                """
                UPDATE semantic_memories
                SET confidence = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (confidence, memory_id),
            )
            connection.commit()

    def update_semantic_embedding(self, memory_id: int, embedding: list[float] | str) -> None:
        embedding_json = json.dumps(embedding) if isinstance(embedding, list) else embedding
        with closing(self.connect()) as connection:
            connection.execute(
                """
                UPDATE semantic_memories
                SET embedding = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (embedding_json, memory_id),
            )
            connection.commit()

    def active_semantic_memories(
        self,
        subject: str | None = None,
        predicate: str | None = None,
    ) -> list[dict[str, Any]]:
        query = """
            SELECT id, subject, predicate, object, confidence, source_event_id, embedding,
                   created_at, updated_at, expires_at, superseded_by, archived_at, archive_reason
            FROM semantic_memories
            WHERE superseded_by IS NULL
              AND archived_at IS NULL
              AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)
        """
        params: list[Any] = []
        if subject is not None:
            query += " AND subject = ?"
            params.append(subject)
        if predicate is not None:
            query += " AND predicate = ?"
            params.append(predicate)
        query += " ORDER BY updated_at DESC, id DESC"

        with closing(self.connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def archived_semantic_memories(self) -> list[dict[str, Any]]:
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """
                SELECT id, subject, predicate, object, confidence, source_event_id, embedding,
                       created_at, updated_at, expires_at, superseded_by, archived_at, archive_reason
                FROM semantic_memories
                WHERE archived_at IS NOT NULL
                ORDER BY archived_at DESC, id DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def expired_semantic_memories(self) -> list[dict[str, Any]]:
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """
                SELECT id, subject, predicate, object, confidence, source_event_id, embedding,
                       created_at, updated_at, expires_at, superseded_by, archived_at, archive_reason
                FROM semantic_memories
                WHERE superseded_by IS NULL
                  AND archived_at IS NULL
                  AND expires_at IS NOT NULL
                  AND expires_at <= CURRENT_TIMESTAMP
                ORDER BY expires_at ASC, id ASC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def archive_semantic_memory(self, memory_id: int, reason: str = "archived") -> None:
        with closing(self.connect()) as connection:
            connection.execute(
                """
                UPDATE semantic_memories
                SET archived_at = CURRENT_TIMESTAMP,
                    archive_reason = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (reason, memory_id),
            )
            connection.commit()

    def supersede_semantic_memory(
        self,
        old_memory_id: int,
        subject: str,
        predicate: str,
        object_value: str,
        source_event_id: int,
        confidence: float = 0.5,
        reason: str = "superseded",
        embedding: list[float] | str | None = None,
    ) -> int:
        embedding_json = json.dumps(embedding) if isinstance(embedding, list) else embedding
        with closing(self.connect()) as connection:
            cursor = connection.execute(
                """
                INSERT INTO semantic_memories (subject, predicate, object, confidence, source_event_id, embedding)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (subject, predicate, object_value, confidence, source_event_id, embedding_json),
            )
            new_memory_id = int(cursor.lastrowid)
            connection.execute(
                """
                UPDATE semantic_memories
                SET superseded_by = ?,
                    archived_at = CURRENT_TIMESTAMP,
                    archive_reason = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (new_memory_id, reason, old_memory_id),
            )
            connection.commit()
            return new_memory_id

    def add_procedure(
        self,
        task_type: str,
        context_pattern: str,
        steps: list[str],
        source_episode_id: int,
        confidence: float = 0.5,
        success_count: int = 1,
        failure_count: int = 0,
    ) -> int:
        with closing(self.connect()) as connection:
            cursor = connection.execute(
                """
                INSERT INTO procedures (
                    task_type, context_pattern, steps, confidence,
                    success_count, failure_count, source_episode_id,
                    last_success_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, CASE WHEN ? > 0 THEN CURRENT_TIMESTAMP ELSE NULL END)
                """,
                (
                    task_type,
                    context_pattern,
                    json.dumps(steps, ensure_ascii=False),
                    confidence,
                    success_count,
                    failure_count,
                    source_episode_id,
                    success_count,
                ),
            )
            connection.commit()
            return int(cursor.lastrowid)

    def find_procedure(self, task_type: str, context_pattern: str) -> dict[str, Any] | None:
        with closing(self.connect()) as connection:
            row = connection.execute(
                """
                SELECT id, task_type, context_pattern, steps, confidence, success_count,
                       failure_count, source_episode_id, created_at, updated_at,
                       last_success_at, archived_at, archive_reason
                FROM procedures
                WHERE task_type = ? AND context_pattern = ? AND archived_at IS NULL
                ORDER BY confidence DESC, success_count DESC, id DESC
                LIMIT 1
                """,
                (task_type, context_pattern),
            ).fetchone()
        return _procedure_from_row(row) if row is not None else None

    def active_procedures(self, task_type: str | None = None) -> list[dict[str, Any]]:
        query = """
            SELECT id, task_type, context_pattern, steps, confidence, success_count,
                   failure_count, source_episode_id, created_at, updated_at,
                   last_success_at, archived_at, archive_reason
            FROM procedures
            WHERE archived_at IS NULL
        """
        params: list[Any] = []
        if task_type is not None:
            query += " AND task_type = ?"
            params.append(task_type)
        query += " ORDER BY confidence DESC, success_count DESC, updated_at DESC, id DESC"

        with closing(self.connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return [_procedure_from_row(row) for row in rows]

    def record_procedure_result(self, procedure_id: int, succeeded: bool) -> None:
        success_increment = 1 if succeeded else 0
        failure_increment = 0 if succeeded else 1
        success_timestamp = ", last_success_at = CURRENT_TIMESTAMP" if succeeded else ""
        with closing(self.connect()) as connection:
            connection.execute(
                f"""
                UPDATE procedures
                SET success_count = success_count + ?,
                    failure_count = failure_count + ?,
                    updated_at = CURRENT_TIMESTAMP
                    {success_timestamp}
                WHERE id = ?
                """,
                (success_increment, failure_increment, procedure_id),
            )
            connection.commit()

    def archive_procedure(self, procedure_id: int, reason: str = "archived") -> None:
        with closing(self.connect()) as connection:
            connection.execute(
                """
                UPDATE procedures
                SET archived_at = CURRENT_TIMESTAMP,
                    archive_reason = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (reason, procedure_id),
            )
            connection.commit()

    def archived_procedures(self) -> list[dict[str, Any]]:
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """
                SELECT id, task_type, context_pattern, steps, confidence, success_count,
                       failure_count, source_episode_id, created_at, updated_at,
                       last_success_at, archived_at, archive_reason
                FROM procedures
                WHERE archived_at IS NOT NULL
                ORDER BY archived_at DESC, id DESC
                """
            ).fetchall()
        return [_procedure_from_row(row) for row in rows]

    def review_candidates(self, limit: int = 50) -> list[dict[str, Any]]:
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """
                SELECT id, episode_id, event_type, role, content, metadata, created_at
                FROM episode_events
                WHERE event_type IN ('memory_review_candidate', 'memory_review_result')
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [_event_from_row(row) for row in rows]


def _event_from_row(row: sqlite3.Row) -> dict[str, Any]:
    event = dict(row)
    event["metadata"] = json.loads(event["metadata"] or "{}")
    return event


def _procedure_from_row(row: sqlite3.Row) -> dict[str, Any]:
    procedure = dict(row)
    procedure["steps"] = json.loads(procedure["steps"] or "[]")
    return procedure


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value
