import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
import json
from pathlib import Path
import os

from communication_adapters.telegram_adapter import TelegramAdapter
from memory import memory_db_path
from communication_store import DEFAULT_DB_PATH as communication_db_path
import server

def get_recent_jobs(hours: int = 24) -> list[dict]:
    with closing(sqlite3.connect(communication_db_path)) as conn:
        conn.row_factory = sqlite3.Row
        cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
        rows = conn.execute(
            "SELECT * FROM agent_command_jobs WHERE created_at >= ? ORDER BY created_at DESC", 
            (cutoff,)
        ).fetchall()
        return [dict(r) for r in rows]

def get_recent_semantic_memories(hours: int = 24) -> list[dict]:
    with closing(sqlite3.connect(memory_db_path())) as conn:
        conn.row_factory = sqlite3.Row
        cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
        rows = conn.execute(
            "SELECT * FROM semantic_memories WHERE updated_at >= ? ORDER BY updated_at DESC", 
            (cutoff,)
        ).fetchall()
        return [dict(r) for r in rows]

def get_recent_procedures(hours: int = 24) -> list[dict]:
    with closing(sqlite3.connect(memory_db_path())) as conn:
        conn.row_factory = sqlite3.Row
        cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
        rows = conn.execute(
            "SELECT * FROM procedures WHERE updated_at >= ? ORDER BY updated_at DESC", 
            (cutoff,)
        ).fetchall()
        return [dict(r) for r in rows]

def generate_daily_summary(telegram_chat_id: str | None = None) -> tuple[str, str]:
    try:
        jobs = get_recent_jobs(24)
        memories = get_recent_semantic_memories(24)
        procedures = get_recent_procedures(24)
        
        completed_jobs = [j for j in jobs if j['status'] == 'completed']
        failed_jobs = [j for j in jobs if j['status'] == 'failed']
        
        new_memories = [m for m in memories if m['created_at'] == m['updated_at'] and not m.get('archived_at')]
        updated_memories = [m for m in memories if m['created_at'] != m['updated_at'] and not m.get('archived_at')]
        low_confidence_memories = [m for m in memories if m['confidence'] < 0.3 and not m.get('archived_at')]
        
        new_procedures = [p for p in procedures if p['created_at'] == p['updated_at']]
        
        prompt = f'''Generate a daily summary report based on the following data from the last 24 hours. Format it nicely in Markdown. Be concise but informative. Include sections for tasks, semantic memory, and procedures.

Data:
- Completed Tasks: {len(completed_jobs)} (Snippets: {json.dumps([j.get('text', '')[:50] for j in completed_jobs[:5]])})
- Failed Tasks: {len(failed_jobs)} (Snippets: {json.dumps([j.get('error_message', '')[:50] for j in failed_jobs[:5]])})
- New Semantic Memories: {len(new_memories)} (Snippets: {json.dumps([f"{m['subject']} {m['predicate']} {m['object']}" for m in new_memories[:5]])})
- Updated Semantic Memories: {len(updated_memories)}
- Low Confidence Memories requiring review: {len(low_confidence_memories)}
- New Procedures: {len(new_procedures)} (Task Types: {json.dumps([p['task_type'] for p in new_procedures[:5]])})

Generate the markdown summary now:'''

        messages = [{"role": "system", "content": "You are a helpful assistant that generates daily summaries of agent activity. Output only the markdown summary, no conversational filler."},
                    {"role": "user", "content": prompt}]
        
        response = server.get_client().chat.completions.create(
            model=server.OLLAMA_MODEL,
            messages=messages
        )
        summary = response.choices[0].message.content
        if telegram_chat_id:
            try:
                adapter = TelegramAdapter()
                adapter.send_message(telegram_chat_id, summary)
            except Exception as e:
                pass
        return summary, ""
    except Exception as e:
        import traceback
        return "", traceback.format_exc()