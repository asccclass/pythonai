"""Trusted local operator retention entrypoint. Default is read-only dry run."""
import argparse
import asyncio
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import time

from .retention import candidates,tables


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--owner-hash',required=True)
    parser.add_argument('--days',type=int,default=30)
    parser.add_argument('--limit',type=int,default=100)
    parser.add_argument('--apply',action='store_true')
    args = parser.parse_args(argv)
    if not 1<=args.days<=3650:
        parser.error('--days must be 1..3650')
    root = Path(os.environ.get('LOCALAPPDATA') or Path.home()/'.local'/'share')/'PythonAI'
    path = Path(os.environ.get('AGENT_TASK_DB') or root/'tasks.db').resolve()
    before = time.time()-args.days*86400
    if not path.is_file():
        parser.error('Task database does not exist')
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        keys = candidates(conn,args.owner_hash,before,args.limit)
        pending = []
        if 'temporal_erasures' in tables(conn):
            from .admission import owner_key
            pending = [r[0] for r in conn.execute("""SELECT e.submission_id,s.owner FROM temporal_erasures e
                JOIN temporal_submissions s ON s.id=e.submission_id
                WHERE e.reason='retention' AND e.state!='local_done' ORDER BY e.updated""")
                if owner_key(r[1])==args.owner_hash][:args.limit]
    if not args.apply:
        print(json.dumps({'dry_run':True,'candidates':keys,'pending':pending}))
        return
    from permissions import current_role
    if current_role()!='admin':
        raise PermissionError('Retention apply requires trusted admin operator')
    from .store import Store
    from .retention import select
    from .erasure import remote,purge
    store = Store(path)
    # Re-select under a write transaction. The dry-run snapshot is not authority.
    remaining = args.limit-len(pending)
    fresh = select(store,args.owner_hash,before,remaining,True) if remaining else []
    keys = pending+fresh
    async def apply():
        result = []
        for key in keys:
            try:
                await remote(store,key)
                purge(store,key)
                result.append({'id':key,'state':'local_done'})
            except Exception as error:
                result.append({'id':key,'state':'pending','error_type':type(error).__name__})
        return result
    print(json.dumps({'dry_run':False,'results':asyncio.run(apply())}))


if __name__=='__main__':
    main()
