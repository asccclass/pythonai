"""Trusted owner-scoped DAG status, seal, cancel and dispatch reconciliation."""
import argparse
import asyncio
from contextlib import closing
from pathlib import Path
from .dag import Graphs
from .store import canonical


def main(argv=None):
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[2]/'.env')
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['status','seal','cancel','reconcile'])
    parser.add_argument('--id',required=True)
    args=parser.parse_args(argv)
    graphs=Graphs()
    graph=graphs.get(args.id,operator=True)
    if args.action=='seal':graphs.seal(args.id)
    elif args.action=='cancel':graphs.cancel(args.id)
    if args.action=='reconcile':
        try:asyncio.run(graphs.dispatch(args.id))
        except Exception as error:
            print(canonical({'id':args.id,'state':'pending','error_type':type(error).__name__}))
            return 2
    graph=graphs.get(args.id,operator=True)
    with closing(graphs.coordinator.connect()) as conn:
        nodes=[{'id':n['id'],'persona':n['persona'],'state':n['state'],'reviewed_by':n['reviewed_by']}
            for n in graphs.nodes(conn,graph)]
    print(canonical({'id':graph['id'],'state':graph['state'],'sealed':bool(graph['sealed']),
        'dispatched':bool(graph['dispatched']),'nodes':nodes}))
    return 0


if __name__=='__main__':raise SystemExit(main())
