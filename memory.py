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


def _event_from_row(row: sqlite3.Row) -> dict[str, Any]:
    event = dict(row)
    event["metadata"] = json.loads(event["metadata"] or "{}")
    return event


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
