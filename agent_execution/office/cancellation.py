"""Propagate core cancellation into Office in the caller's SQLite transaction."""


def cancel_tasks(conn, owner, task_id=None):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='office_executions'").fetchone():
        return 0
    query = "SELECT id FROM office_assignments WHERE owner=? AND state IN ('queued','active')"
    args = [owner]
    if task_id is not None:
        query += ' AND task_id=?'
        args.append(task_id)
    rows = conn.execute(query,args).fetchall()
    for row in rows:
        key = row[0]
        cancel_calls(conn,owner,key)
        conn.execute("UPDATE office_assignments SET state='cancelled' WHERE owner=? AND id=?", (owner,key))
        conn.execute("UPDATE office_executions SET state='draining' WHERE owner=? AND assignment_id=? AND state='running'", (owner,key))
        conn.execute("UPDATE office_operations SET state='cancelled' WHERE owner=? AND assignment_id=?", (owner,key))
        conn.execute("UPDATE office_outbox SET state=CASE WHEN attempts=0 THEN 'cancelled' ELSE 'cancel_pending' END,token=NULL,lease_until=NULL WHERE owner=? AND assignment_id=? AND state!='cancelled'", (owner,key))
    return len(rows)


def cancel_calls(conn,owner,assignment):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='office_continuations'").fetchone():
        return
    row = conn.execute('''SELECT c.*,t.phase,t.checkpoint FROM office_continuations c
        JOIN office_executions e ON e.owner=c.owner AND e.assignment_id=c.assignment_id
        JOIN tasks t ON t.owner=e.owner AND t.id=e.task_id
        WHERE c.owner=? AND c.assignment_id=?''',(owner,assignment)).fetchone()
    from .persistence import digest
    safe = bool(row and row['state'] in {'waiting','ready'} and row['phase']=='office_wait'
        and row['checkpoint_digest']==digest(row['checkpoint']) and not conn.execute(
        "SELECT 1 FROM office_calls WHERE owner=? AND assignment_id=? AND state IN ('started','consuming','uncertain')",(owner,assignment)).fetchone())
    conn.execute("UPDATE office_continuations SET state='cancelled' WHERE owner=? AND assignment_id=?",(owner,assignment))
    conn.execute("UPDATE office_calls SET state=CASE WHEN state IN ('started','consuming') THEN 'uncertain' ELSE 'cancelled' END WHERE owner=? AND assignment_id=? AND state NOT IN ('completed','uncertain')",(owner,assignment))
    if safe:
        conn.execute("UPDATE office_executions SET state='cancelled' WHERE owner=? AND assignment_id=?",(owner,assignment))
