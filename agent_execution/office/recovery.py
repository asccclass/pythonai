"""Restore only native checkpoints that prove a completed I/O boundary."""
import json
import time

from .persistence import digest


def recover_boundary(conn,owner,execution):
    assignment = execution['assignment_id']
    turn = conn.execute('SELECT * FROM office_turns WHERE owner=? AND assignment_id=?',(owner,assignment)).fetchone()
    task = conn.execute('SELECT * FROM tasks WHERE owner=? AND id=?',(owner,execution['task_id'])).fetchone()
    if (not turn or not task or task['state'] not in {'running','pending'}
            or turn['session_id']!=execution['session_id'] or turn['generation']!=execution['generation']):
        return False
    if conn.execute("SELECT 1 FROM office_calls WHERE owner=? AND assignment_id=? AND state IN ('consuming','started','uncertain')",(owner,assignment)).fetchone():
        return False
    phase,call_id = task['phase'],None
    if phase == 'tool_started':
        from agent_execution.turn_control.continuation import assistant_frontier
        frontier,finished = assistant_frontier(json.loads(task['checkpoint'])['messages'])
        outstanding = [c['id'] for c in frontier.get('tool_calls',[]) if c['id'] not in finished]
        calls = conn.execute("SELECT * FROM office_calls WHERE owner=? AND assignment_id=? AND parent_id IS NULL AND state='completed'",(owner,assignment)).fetchall()
        for call in calls:
            binding = json.loads(call['binding'])
            if binding['provider_tool_call_id'] in outstanding and binding['session_id']==turn['session_id'] and binding['turn_id']==turn['turn_id']:
                call_id = call['id'];break
        if call_id is None: return False
        phase = 'model_received'
    elif phase not in {'model_received','tool_done','evaluation_done','step_ready'}:
        return False
    conn.execute('''INSERT INTO office_continuations
        (owner,assignment_id,call_id,state,checkpoint_digest,session_id,turn_id,workspace,tools,role,resume_phase,updated)
        VALUES(?,?,?,'ready',?,?,?,?,?,?,?,?) ON CONFLICT(owner,assignment_id) DO UPDATE SET
        call_id=excluded.call_id,state='ready',checkpoint_digest=excluded.checkpoint_digest,
        resume_phase=excluded.resume_phase,updated=excluded.updated''',
        (owner,assignment,call_id,digest(task['checkpoint']),turn['session_id'],turn['turn_id'],turn['workspace'],turn['tools'],turn['role'],phase,time.time()))
    conn.execute("UPDATE tasks SET state='office_wait',phase='office_wait',lease_owner=NULL,lease_until=NULL WHERE owner=? AND id=?",(owner,execution['task_id']))
    return True
