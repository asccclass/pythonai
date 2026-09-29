from __future__ import annotations

from dataclasses import asdict, is_dataclass
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
from typing import Any


DEFAULT_MEMORY_DB = Path(__file__).resolve().parent / "memory" / "memory.db"


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
                    subject_entity_id INTEGER,
                    object_entity_id INTEGER,
                    memory_type TEXT NOT NULL DEFAULT 'fact',
                    scope TEXT NOT NULL DEFAULT 'global',
                    confidence REAL NOT NULL DEFAULT 0.5,
                    source_event_id INTEGER NOT NULL,
                    embedding TEXT,
                    embedding_updated_at TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    expires_at TEXT,
                    superseded_by INTEGER,
                    archived_at TEXT,
                    archive_reason TEXT,
                    FOREIGN KEY (source_event_id) REFERENCES episode_events(id),
                    FOREIGN KEY (subject_entity_id) REFERENCES entities(id),
                    FOREIGN KEY (object_entity_id) REFERENCES entities(id),
                    FOREIGN KEY (superseded_by) REFERENCES semantic_memories(id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS entities (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    normalized_name TEXT NOT NULL,
                    entity_type TEXT NOT NULL DEFAULT 'entity',
                    source_event_id INTEGER,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(normalized_name, entity_type),
                    FOREIGN KEY (source_event_id) REFERENCES episode_events(id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS entity_aliases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_id INTEGER NOT NULL,
                    alias TEXT NOT NULL,
                    normalized_alias TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(normalized_alias, entity_id),
                    FOREIGN KEY (entity_id) REFERENCES entities(id)
                )
                """
            )
            try:
                connection.execute("ALTER TABLE semantic_memories ADD COLUMN embedding TEXT")
            except sqlite3.OperationalError:
                pass
            try:
                connection.execute("ALTER TABLE semantic_memories ADD COLUMN embedding_updated_at TEXT")
            except sqlite3.OperationalError:
                pass
            try:
                connection.execute("ALTER TABLE semantic_memories ADD COLUMN memory_type TEXT NOT NULL DEFAULT 'fact'")
            except sqlite3.OperationalError:
                pass
            try:
                connection.execute("ALTER TABLE semantic_memories ADD COLUMN scope TEXT NOT NULL DEFAULT 'global'")
            except sqlite3.OperationalError:
                pass
            try:
                connection.execute("ALTER TABLE semantic_memories ADD COLUMN subject_entity_id INTEGER")
            except sqlite3.OperationalError:
                pass
            try:
                connection.execute("ALTER TABLE semantic_memories ADD COLUMN object_entity_id INTEGER")
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

    def message_events(self, limit: int = 50, episode_id: int | None = None) -> list[dict[str, Any]]:
        query = """
            SELECT id, episode_id, event_type, role, content, metadata, created_at
            FROM episode_events
            WHERE event_type = 'message'
        """
        params: list[Any] = []
        if episode_id is not None:
            query += " AND episode_id = ?"
            params.append(episode_id)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)

        with closing(self.connect()) as connection:
            rows = connection.execute(query, params).fetchall()
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
        memory_type: str = "fact",
        scope: str = "global",
        subject_entity_id: int | None = None,
        object_entity_id: int | None = None,
    ) -> int:
        embedding_json = json.dumps(embedding) if isinstance(embedding, list) else embedding
        with closing(self.connect()) as connection:
            if subject_entity_id is None:
                subject_entity_id = self._get_or_create_entity(connection, subject, infer_entity_type(subject, memory_type), source_event_id)
            if object_entity_id is None and should_link_object_entity(object_value, memory_type):
                object_entity_id = self._get_or_create_entity(connection, object_value, infer_entity_type(object_value, memory_type), source_event_id)
            cursor = connection.execute(
                """
                INSERT INTO semantic_memories (
                    subject, predicate, object, subject_entity_id, object_entity_id, memory_type, scope,
                    confidence, source_event_id, expires_at, embedding, embedding_updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CASE WHEN ? IS NULL THEN NULL ELSE CURRENT_TIMESTAMP END)
                """,
                (
                    subject,
                    predicate,
                    object_value,
                    subject_entity_id,
                    object_entity_id,
                    memory_type,
                    scope,
                    confidence,
                    source_event_id,
                    expires_at,
                    embedding_json,
                    embedding_json,
                ),
            )
            connection.commit()
            return int(cursor.lastrowid)

    def get_or_create_entity(
        self,
        name: str,
        entity_type: str = "entity",
        source_event_id: int | None = None,
        aliases: list[str] | None = None,
    ) -> int:
        with closing(self.connect()) as connection:
            entity_id = self._get_or_create_entity(connection, name, entity_type, source_event_id)
            for alias in aliases or []:
                self._add_entity_alias(connection, entity_id, alias)
            connection.commit()
            return entity_id

    def find_entity(self, name_or_alias: str, entity_type: str | None = None) -> dict[str, Any] | None:
        normalized = normalize_entity_name(name_or_alias)
        query = """
            SELECT e.id, e.name, e.normalized_name, e.entity_type, e.source_event_id, e.created_at, e.updated_at
            FROM entities e
            LEFT JOIN entity_aliases a ON a.entity_id = e.id
            WHERE (e.normalized_name = ? OR a.normalized_alias = ?)
        """
        params: list[Any] = [normalized, normalized]
        if entity_type is not None:
            query += " AND e.entity_type = ?"
            params.append(entity_type)
        query += " ORDER BY e.updated_at DESC, e.id DESC LIMIT 1"
        with closing(self.connect()) as connection:
            row = connection.execute(query, params).fetchone()
        return dict(row) if row is not None else None

    def entity_aliases(self, entity_id: int) -> list[dict[str, Any]]:
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """
                SELECT id, entity_id, alias, normalized_alias, created_at
                FROM entity_aliases
                WHERE entity_id = ?
                ORDER BY id ASC
                """,
                (entity_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def _get_or_create_entity(
        self,
        connection: sqlite3.Connection,
        name: str,
        entity_type: str = "entity",
        source_event_id: int | None = None,
    ) -> int:
        normalized_name = normalize_entity_name(name)
        row = connection.execute(
            """
            SELECT id
            FROM entities
            WHERE normalized_name = ? AND entity_type = ?
            """,
            (normalized_name, entity_type),
        ).fetchone()
        if row is not None:
            self._add_entity_alias(connection, int(row["id"]), name)
            return int(row["id"])
        cursor = connection.execute(
            """
            INSERT INTO entities (name, normalized_name, entity_type, source_event_id)
            VALUES (?, ?, ?, ?)
            """,
            (name, normalized_name, entity_type, source_event_id),
        )
        entity_id = int(cursor.lastrowid)
        self._add_entity_alias(connection, entity_id, name)
        return entity_id

    def _add_entity_alias(self, connection: sqlite3.Connection, entity_id: int, alias: str) -> None:
        normalized_alias = normalize_entity_name(alias)
        connection.execute(
            """
            INSERT OR IGNORE INTO entity_aliases (entity_id, alias, normalized_alias)
            VALUES (?, ?, ?)
            """,
            (entity_id, alias, normalized_alias),
        )

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
                    embedding_updated_at = CURRENT_TIMESTAMP,
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
        memory_type: str | None = None,
        scope: str | None = None,
    ) -> list[dict[str, Any]]:
        query = """
            SELECT id, subject, predicate, object, subject_entity_id, object_entity_id,
                   memory_type, scope, confidence, source_event_id, embedding, embedding_updated_at,
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
        if memory_type is not None:
            query += " AND memory_type = ?"
            params.append(memory_type)
        if scope is not None:
            query += " AND scope = ?"
            params.append(scope)
        query += " ORDER BY updated_at DESC, id DESC"

        with closing(self.connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def archived_semantic_memories(self) -> list[dict[str, Any]]:
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """
                SELECT id, subject, predicate, object, subject_entity_id, object_entity_id,
                       memory_type, scope, confidence, source_event_id, embedding, embedding_updated_at,
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
                SELECT id, subject, predicate, object, subject_entity_id, object_entity_id,
                       memory_type, scope, confidence, source_event_id, embedding, embedding_updated_at,
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
        memory_type: str = "fact",
        scope: str = "global",
        subject_entity_id: int | None = None,
        object_entity_id: int | None = None,
    ) -> int:
        embedding_json = json.dumps(embedding) if isinstance(embedding, list) else embedding
        with closing(self.connect()) as connection:
            if subject_entity_id is None:
                subject_entity_id = self._get_or_create_entity(connection, subject, infer_entity_type(subject, memory_type), source_event_id)
            if object_entity_id is None and should_link_object_entity(object_value, memory_type):
                object_entity_id = self._get_or_create_entity(connection, object_value, infer_entity_type(object_value, memory_type), source_event_id)
            cursor = connection.execute(
                """
                INSERT INTO semantic_memories (
                    subject, predicate, object, subject_entity_id, object_entity_id,
                    memory_type, scope, confidence, source_event_id, embedding, embedding_updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CASE WHEN ? IS NULL THEN NULL ELSE CURRENT_TIMESTAMP END)
                """,
                (
                    subject,
                    predicate,
                    object_value,
                    subject_entity_id,
                    object_entity_id,
                    memory_type,
                    scope,
                    confidence,
                    source_event_id,
                    embedding_json,
                    embedding_json,
                ),
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

    def skill_run_events(self, limit: int = 50) -> list[dict[str, Any]]:
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """
                SELECT id, episode_id, event_type, role, content, metadata, created_at
                FROM episode_events
                WHERE event_type IN (
                    'skill_candidates',
                    'skill_selected',
                    'skill_step',
                    'skill_step_result',
                    'skill_result'
                )
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


def normalize_entity_name(name: str) -> str:
    return " ".join(str(name).strip().casefold().split())


def infer_entity_type(name: str, memory_type: str = "fact") -> str:
    normalized = normalize_entity_name(name)
    if normalized == "user":
        return "user"
    if normalized in {"project", "repo", "repository"} or memory_type == "project_fact":
        return "project"
    if memory_type == "agent_persona":
        return "agent"
    if memory_type == "task_fact":
        return "task"
    return "entity"


def should_link_object_entity(object_value: str, memory_type: str = "fact") -> bool:
    normalized = normalize_entity_name(object_value)
    if not normalized:
        return False
    if memory_type in {"entity_fact", "project_fact", "agent_persona"}:
        return True
    return len(normalized.split()) <= 4 and not any(char.isdigit() for char in normalized)


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
