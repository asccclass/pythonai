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
    parser.add_argument('action',choices=['status','seal','cancel','reconcile','merge','archive','operation-status'])
    parser.add_argument('--id',required=True)
    parser.add_argument('--node')
    args=parser.parse_args(argv)
    graphs=Graphs()
    from .dag_lifecycle import Lifecycle
    life=Lifecycle(graphs)
    if args.action=='operation-status':
        operation=life.get(args.id)
        graphs.get(operation['graph_id'],operator=True)
        print(canonical({key:operation[key] for key in ('id','graph_id','node_id','action','state')}))
        return 0
    graph=graphs.get(args.id,operator=True)
    if args.action in {'merge','archive'}:
        if not args.node:parser.error('--node is required')
        key=life.request(args.id,args.node,args.action)
        print(canonical({'operation_id':key,'state':life.get(key)['state']}))
        return 0
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
