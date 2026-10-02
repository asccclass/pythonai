import sqlite3
from contextlib import closing
from memory import memory_db_path
from communication_store import DEFAULT_DB_PATH as communication_db_path

def get_dashboard_stats():
    stats = {}
    # Get memory stats
    with closing(sqlite3.connect(memory_db_path())) as conn:
        conn.row_factory = sqlite3.Row
        stats['semantic_memory_count'] = conn.execute("SELECT COUNT(*) as c FROM semantic_memories WHERE archived_at IS NULL").fetchone()['c']
        stats['low_confidence_memory_count'] = conn.execute("SELECT COUNT(*) as c FROM semantic_memories WHERE archived_at IS NULL AND confidence < 0.3").fetchone()['c']
        stats['procedure_count'] = conn.execute("SELECT COUNT(*) as c FROM procedures").fetchone()['c']
    
    # Get task stats
    with closing(sqlite3.connect(communication_db_path())) as conn:
        conn.row_factory = sqlite3.Row
        stats['pending_tasks'] = conn.execute("SELECT COUNT(*) as c FROM agent_command_jobs WHERE status = 'pending'").fetchone()['c']
        stats['running_tasks'] = conn.execute("SELECT COUNT(*) as c FROM agent_command_jobs WHERE status = 'running'").fetchone()['c']
        stats['failed_tasks'] = conn.execute("SELECT COUNT(*) as c FROM agent_command_jobs WHERE status = 'failed'").fetchone()['c']
    
    return stats

def get_recent_memories(limit=50):
    with closing(sqlite3.connect(memory_db_path())) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT id, subject, predicate, object, confidence, created_at, memory_type FROM semantic_memories WHERE archived_at IS NULL ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

def get_recent_tasks(limit=50):
    with closing(sqlite3.connect(communication_db_path())) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT id, text, status, created_at, error_message FROM agent_command_jobs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

def handle_api_request(path: str):
    if path == "/api/stats":
        return 200, get_dashboard_stats()
    elif path == "/api/memories":
        return 200, get_recent_memories()
    elif path == "/api/tasks":
        return 200, get_recent_tasks()
    return 404, {"error": "not found"}