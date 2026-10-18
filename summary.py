from agent_configuration import load_text
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
import json
from pathlib import Path
import os

from communication_adapters.telegram_adapter import TelegramAdapter
from memory import memory_db_path
from communication_store import communication_db_path
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
        
        prompt = load_text(
            'instructions.json', 'summary_request',
            completed_count=len(completed_jobs),
            completed_snippets=json.dumps([j.get('text', '')[:50] for j in completed_jobs[:5]]),
            failed_count=len(failed_jobs),
            failed_snippets=json.dumps([(j.get('error_message') or j.get('last_error') or '')[:50] for j in failed_jobs[:5]]),
            new_memory_count=len(new_memories),
            new_memory_snippets=json.dumps([f"{m['subject']} {m['predicate']} {m['object']}" for m in new_memories[:5]]),
            updated_memory_count=len(updated_memories),
            low_confidence_count=len(low_confidence_memories),
            new_procedure_count=len(new_procedures),
            procedure_types=json.dumps([p['task_type'] for p in new_procedures[:5]]),
        )

        messages = [{"role": "system", "content": load_text('instructions.json', 'summary_system')},
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
