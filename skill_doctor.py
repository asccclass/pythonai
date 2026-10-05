import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
import json
import os
from pathlib import Path
from collections import Counter
from datetime import datetime, timedelta
import argparse

from dotenv import load_dotenv
from openai import OpenAI

from memory import MemoryStore

load_dotenv()

SYSTEM_PROMPT = """You are the 'Skill Doctor' for an autonomous AI Agent.
Your job is to analyze the agent's recent execution histories (Episodes) to evaluate its efficiency, tool usage, procedural compliance, and Critic-Correction loop effectiveness.

Here is the data from the agent's recent episodes, including tool usages, critic feedback (when the Task Evaluator rejects the agent's work), and HITL (Human-in-the-Loop) handovers.

Your Output MUST be in JSON format matching this schema:
{
    "report": "A detailed markdown report (string) in Traditional Chinese (zh-TW) scoring the agent on Efficiency, Code Quality, Verbosity, and Procedure Compliance. List out specific anti-patterns observed.",
    "new_agent_personas": [
        {
            "subject": "agent",
            "predicate": "must_remember_to",
            "object": "do something specific to avoid the failures seen in the logs (write this in English)"
        }
    ]
}

The `new_agent_personas` will be injected into the agent's semantic memory (as 'agent_persona' memory_type) to permanently fix these anti-patterns and improve the macro self-correction loop. Be very specific in your directives.
"""

def gather_diagnostics(store: MemoryStore, limit: int = 20) -> str:
    with store.connect() as conn:
        rows = conn.execute(
            """
            SELECT id, started_at, ended_at, status, summary
            FROM episodes
            ORDER BY id DESC LIMIT ?
            """,
            (limit,)
        ).fetchall()
        
    episodes = [dict(r) for r in rows]
    if not episodes:
        return "No recent episodes found."
        
    dump = "## Recent Episodes\n\n"
    
    for ep in episodes:
        ep_id = ep["id"]
        dump += f"### Episode {ep_id} (Status: {ep['status']})\n"
        dump += f"Summary: {ep['summary']}\n"
        
        events = store.episode_events(ep_id)
        
        tool_counts = Counter()
        critic_feedbacks = []
        hitl_count = 0
        
        for ev in events:
            if ev["event_type"] == "tool_call":
                meta = ev.get("metadata", {})
                if isinstance(meta, dict):
                    tool_counts[meta.get("name", "unknown")] += 1
            elif ev["event_type"] == "agent_message" and ev["role"] == "user":
                content = ev.get("content", "")
                if "任務驗收未通過 (Critic Feedback)" in content:
                    critic_feedbacks.append(content)
                elif "HITL" in content or "人工審核" in content:
                    hitl_count += 1
                    
        dump += f"Tool Usage: {dict(tool_counts)}\n"
        dump += f"Critic Feedbacks Triggered: {len(critic_feedbacks)}\n"
        if critic_feedbacks:
            dump += "Sample Critic Feedbacks:\n"
            for fb in critic_feedbacks[:2]:
                dump += f"- {fb[:200]}...\n"
        dump += f"HITL Handovers: {hitl_count}\n\n"
        
    return dump


def run_doctor(db_path: str = "memory/memory.db", limit: int = 20, auto_apply: bool = False):
    store = MemoryStore(Path(db_path))
    diagnostics = gather_diagnostics(store, limit)
    
    if diagnostics == "No recent episodes found.":
        print("沒有找到最近的對話紀錄 (No recent episodes found)。")
        return
        
    client = OpenAI(
        base_url=os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434/v1"),
        api_key=os.environ.get("OLLAMA_API_KEY", "ollama")
    )
    
    model = os.environ.get("OLLAMA_MODEL", "llama3.2")
    
    print(f"[*] 正在針對最近的 {limit} 個 Episode 執行 Skill Doctor 效能分析...")
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": diagnostics}
            ],
            response_format={"type": "json_object"},
            temperature=0.2
        )
        
        result_json = response.choices[0].message.content
        result = json.loads(result_json)
    except Exception as e:
        print(f"[!] Doctor 分析失敗: {e}")
        return
        
    report = result.get("report", "")
    new_personas = result.get("new_agent_personas", [])
    
    print("\n" + "="*50)
    print("[Doctor] SKILL DOCTOR 效能診斷報告")
    print("="*50)
    print(report)
    print("="*50)
    
    if new_personas:
        print("\n[*] 建議的新增 Agent Personas (巨觀級別自癒準則):")
        for p in new_personas:
            print(f"  - {p['subject']} {p['predicate']} {p['object']}")
            
        if auto_apply:
            print("\n[*] 正在自動將新的 Personas 注入至記憶庫中...")
            doc_ep = store.start_episode()
            doc_ev = store.add_event(doc_ep, "doctor_report", "system", report)
            store.finish_episode(doc_ep, status="verified_completed", summary="Skill Doctor injected new personas.")
            for p in new_personas:
                store.add_semantic_memory(
                    subject=p.get("subject", "agent"),
                    predicate=p.get("predicate", "must"),
                    object_value=p.get("object", ""),
                    source_event_id=doc_ev,
                    memory_type="agent_persona",
                    scope="global",
                    confidence=0.95
                )
            print("[+] Personas 注入成功！Agent 往後將自動遵守這些全域規則。")
        else:
            print("\n請加入 --apply 參數來將這些準則自動注入至 Agent 的 Semantic Memory 中。")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Skill Doctor - Agent Performance Diagnostics")
    parser.add_argument("--limit", type=int, default=20, help="Number of recent episodes to analyze")
    parser.add_argument("--db", type=str, default="memory/memory.db", help="Path to memory DB")
    parser.add_argument("--apply", action="store_true", help="Auto-apply the generated agent personas")
    args = parser.parse_args()
    
    run_doctor(db_path=args.db, limit=args.limit, auto_apply=args.apply)
