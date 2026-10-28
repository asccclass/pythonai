"""Dry-run-first operator cleanup for old, closed Temporal DAG History chains."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import time

from .dag_retention import candidates, collect


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--owner-hash',required=True)
    parser.add_argument('--days',type=int,default=30)
    parser.add_argument('--limit',type=int,default=100)
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args(argv)
    if not 1<=args.days<=3650:parser.error('--days must be 1..3650')
    if not 1<=args.limit<=100:parser.error('--limit must be 1..100')
    root=Path(os.environ.get('LOCALAPPDATA') or Path.home()/'.local'/'share')/'PythonAI'
    path=Path(os.environ.get('AGENT_TASK_DB') or root/'tasks.db').resolve()
    before=time.time()-args.days*86400
    if not path.is_file():parser.error('Task database does not exist')
    from agent_execution.coordinator import Coordinator
    from .dag import Graphs
    graphs=Graphs(Coordinator(path))
    keys=candidates(graphs,args.owner_hash,before,args.limit)
    if not args.apply:
        print(json.dumps({'dry_run':True,'candidates':keys,'days':args.days}))
        return
    from permissions import current_role
    if current_role()!='admin':raise PermissionError('DAG retention apply requires trusted admin operator')

    async def apply():
        outcomes=[]
        for key in keys:
            try:outcomes.append(await collect(graphs,key,args.owner_hash,before))
            except Exception as error:
                outcomes.append({'graph_ref':key,'state':'pending','error_type':type(error).__name__})
        return outcomes
    print(json.dumps({'dry_run':False,'results':asyncio.run(apply())}))


if __name__=='__main__':
    main()
