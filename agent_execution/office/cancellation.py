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
        conn.execute("UPDATE office_assignments SET state='cancelled' WHERE owner=? AND id=?", (owner,key))
        conn.execute("UPDATE office_executions SET state='draining' WHERE owner=? AND assignment_id=? AND state='running'", (owner,key))
        conn.execute("UPDATE office_operations SET state='cancelled' WHERE owner=? AND assignment_id=?", (owner,key))
        conn.execute("UPDATE office_outbox SET state=CASE WHEN attempts=0 THEN 'cancelled' ELSE 'cancel_pending' END,token=NULL,lease_until=NULL WHERE owner=? AND assignment_id=? AND state!='cancelled'", (owner,key))
    return len(rows)
