import argparse
import json
from pathlib import Path
from .redaction import redact

def main():
    parser=argparse.ArgumentParser(description='Local operations; stop workers before database migration')
    commands=parser.add_subparsers(dest='action',required=True)
    for name in ('readiness','doctor','traces','syntax','benchmark'):commands.add_parser(name)
    alerts=commands.add_parser('alerts');alerts.add_argument('--deliver',action='store_true')
    metrics=commands.add_parser('metrics');metrics.add_argument('--format',choices=('json','prometheus'),default='json');metrics.add_argument('--output')
    backup=commands.add_parser('backup');backup.add_argument('--database',action='append',required=True);backup.add_argument('--destination',required=True)
    restore=commands.add_parser('restore');restore.add_argument('--source',required=True);restore.add_argument('--destination',required=True)
    migrate=commands.add_parser('migrate');migrate.add_argument('--kind',required=True);migrate.add_argument('--database',required=True);migrate.add_argument('--destination',required=True);migrate.add_argument('--offline',action='store_true')
    args=parser.parse_args()
    if args.action=='metrics' and args.output and args.format!='prometheus':
        parser.error('--output requires --format prometheus')
    if args.action in {'doctor','readiness'}:
        from .doctor import readiness,dependency_doctor
        result=readiness() if args.action=='readiness' else dependency_doctor()
    elif args.action=='alerts':
        from .alerts import run
        result=run(args.deliver)
    elif args.action=='traces':
        from .tracing import TraceStore
        store=TraceStore();result={'summary':store.summary(),'spans':store.list()}
    elif args.action=='metrics':
        from .metrics import prometheus,snapshot,write_prometheus
        if args.output:result=write_prometheus(args.output)
        else:result=prometheus() if args.format=='prometheus' else snapshot()
    elif args.action=='syntax':
        from .syntax import check_syntax
        result=check_syntax(Path.cwd())
    elif args.action=='benchmark':
        from .benchmark import run_benchmark
        result=run_benchmark()
    else:
        from .backup import backup,restore,migrate
        if args.action=='backup':result=backup(args.database,args.destination)
        elif args.action=='restore':result=restore(args.source,args.destination)
        else:result=migrate(args.kind,args.database,args.destination,args.offline)
    print(result if isinstance(result,str) else json.dumps(redact(result),ensure_ascii=False,indent=2))
    if isinstance(result,dict) and (result.get('passed') is False or result.get('ready') is False):raise SystemExit(1)

if __name__=='__main__':main()
