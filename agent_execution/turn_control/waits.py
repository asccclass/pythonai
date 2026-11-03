"""Expired human waits cannot intercept or reset a new user's task budget."""
from contextlib import closing
import json
import time

from agent_execution.tasks import RecoveryRequired, TaskBusy, assert_owner_active


EXPIRED = '先前任務已超過等待回答的期限，不能續跑。請以新訊息重新提出要求；舊任務額度不會重設。'


def _deadline(conn,owner,key):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='task_budgets'").fetchone():
        return None
    row = conn.execute('SELECT deadline FROM task_budgets WHERE owner=? AND id=?',(owner,key)).fetchone()
    return row[0] if row else None


def reject_expired_resume(store,owner,key):
    with closing(store.connect()) as conn:
        row = conn.execute('SELECT state FROM tasks WHERE owner=? AND id=?',(owner,key)).fetchone()
        if row is not None and row[0]=='completed':
            return  # Reading a completed result executes no work and needs no new budget.
        deadline = _deadline(conn,owner,key)
        if deadline is not None and deadline<=time.time():
            raise RecoveryRequired(EXPIRED)


def retire_expired_wait(store,owner,key,incoming):
    """Only an explicitly new human request may retire an expired SQLite wait."""
    with closing(store.connect()) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        assert_owner_active(conn,owner)
        row = conn.execute("SELECT * FROM tasks WHERE owner=? AND id=? AND state='suspended'",(owner,key)).fetchone()
        deadline = _deadline(conn,owner,key)
        if row is None or deadline is None or deadline>time.time():
            return None
        if not incoming or incoming==key:
            raise RecoveryRequired(EXPIRED)
        from agent_execution.budgets import active_budget
        inherited = active_budget()
        if inherited is not None and (inherited.key!=incoming or inherited.owner!=owner):
            raise RecoveryRequired(EXPIRED)
        if row['phase']!='waiting_user' or (row['lease_until'] or 0)>time.time():
            raise TaskBusy('待答任務仍有執行程序或未確認狀態，請先確認停止。')
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='execution_bindings'").fetchone():
            binding = conn.execute('SELECT backend FROM execution_bindings WHERE owner=? AND task_id=?',(owner,key)).fetchone()
            if binding and binding[0]=='temporal':
                raise RecoveryRequired('原生工作流程的待答期限已過；請先取消原任務，再以新訊息提出要求。')
        # Existing request replay cannot mint another execution envelope.
        if conn.execute('SELECT 1 FROM tasks WHERE owner=? AND id=?',(owner,incoming)).fetchone():
            raise RecoveryRequired(EXPIRED)
        conn.execute("UPDATE tasks SET state='cancelled',reply=?,lease_owner=NULL,lease_until=NULL,updated=? WHERE owner=? AND id=?",
            ('Error: '+EXPIRED,time.time(),owner,key))
        result = dict(row)
        result['checkpoint'] = json.loads(row['checkpoint'])
        return result


def restore_dialogue(store,owner,runtime,fallback=None):
    """Restore scoped conversation data, never resume or execute old tool calls.

    /clear is a durable fence. New runtimes may use the latest completed
    checkpoint after that fence; system instructions are always the live ones.
    """
    from agent_execution.context import normalize_blocks
    if any(m.get('role') in {'user','assistant'} for m in runtime.messages):
        if fallback is not None:
            runtime.messages[:] = [message for block in normalize_blocks(runtime.messages) for message in block]
        return
    with closing(store.connect()) as conn:
        assert_owner_active(conn,owner)
        if fallback is None:
            reset = conn.execute('SELECT cleared_at FROM conversation_resets WHERE owner=?',(owner,)).fetchone()
            row = conn.execute("SELECT checkpoint FROM tasks WHERE owner=? AND state='completed' AND updated>? ORDER BY updated DESC LIMIT 1",
                (owner,reset[0] if reset else 0)).fetchone()
            if row is None:
                return
            fallback = json.loads(row['checkpoint']).get('messages',[])
    history = [message for block in normalize_blocks(fallback) for message in block
               if message.get('role') not in {'system','developer'}]
    runtime.messages.extend(history)
