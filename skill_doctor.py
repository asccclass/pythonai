from agent_configuration import load_text
import sys
import io
if __name__=='__main__' and hasattr(sys.stdout,'reconfigure'):sys.stdout.reconfigure(encoding='utf-8')
import json
import os
from pathlib import Path
from collections import Counter
from datetime import datetime, timedelta
import argparse
from contextlib import closing

from dotenv import load_dotenv
from openai import OpenAI

from memory import MemoryStore

load_dotenv()


def gather_diagnostics(store: MemoryStore, limit: int = 20) -> str:
    with closing(store.connect()) as conn:
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
        api_key=os.environ.get("OLLAMA_API_KEY", "ollama"),timeout=10,max_retries=0
    )
    
    model = os.environ.get("OLLAMA_MODEL", "llama3.2")
    
    print(f"[*] 正在針對最近的 {limit} 個 Episode 執行 Skill Doctor 效能分析...")
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": load_text('skill_doctor.md')},
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
        
    from agent_execution.extensions.schema import validate
    validate(result,{'type':'object','required':['report'],'properties':{'report':{'type':'string','maxLength':32768},
        'new_agent_personas':{'type':'array','maxItems':20,'items':{'type':'object','required':['subject','predicate','object'],
            'properties':{k:{'type':'string','minLength':1,'maxLength':1000} for k in ('subject','predicate','object')},'additionalProperties':False}},
        'new_skill_proposals':{'type':'array','maxItems':5,'items':{'type':'object','required':['name','files']}}}})
    report = result.get("report", "")
    new_personas = result.get("new_agent_personas", [])
    
    print("\n" + "="*50)
    print("[Doctor] SKILL DOCTOR 效能診斷報告")
    print("="*50)
    print(report)
    print("="*50)
    
    key=None
    if new_personas:
        print("\n[*] 建議的新增 Agent Personas (巨觀級別自癒準則):")
        for p in new_personas:
            print(f"  - {p['subject']} {p['predicate']} {p['object']}")
            
        from agent_execution.improvement.store import ImprovementStore,digest
        key=ImprovementStore().propose('doctor_persona',new_personas,'doctor-diagnostics:'+digest(diagnostics))
        print(f'\n[Doctor] 提案 {key} 已保存；須通過 A/B 評測與人工批准，尚未啟用。')
        if auto_apply:print('--apply 已停用自動注入；僅保存待審查提案。')
    for package in result.get('new_skill_proposals',[]):
        from agent_execution.extensions.store import ExtensionStore
        from agent_execution.improvement.store import ImprovementStore,digest
        version=ExtensionStore().stage(package['name'],'skill',package['files'],'doctor-diagnostics:'+digest(diagnostics))
        proposal=ImprovementStore().propose(package['name'],{'extension_version':version},'doctor skill suggestion',kind='skill')
        print(f'[Doctor] Skill 提案 {proposal}，未測試／未啟用；須依 T18 與 A/B gate 審查。')
    return key

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Skill Doctor - Agent Performance Diagnostics")
    parser.add_argument("--limit", type=int, default=20, help="Number of recent episodes to analyze")
    parser.add_argument("--db", type=str, default="memory/memory.db", help="Path to memory DB")
    parser.add_argument("--apply", action="store_true", help="Auto-apply the generated agent personas")
    args = parser.parse_args()
    
    run_doctor(db_path=args.db, limit=args.limit, auto_apply=args.apply)
