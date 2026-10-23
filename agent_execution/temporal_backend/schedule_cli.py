"""Trusted owner-scoped operator CLI; not registered as a model tool."""
import argparse
import asyncio
from pathlib import Path
from .schedules import Schedules
from .store import canonical

def main(argv=None):
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[2]/'.env')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['create','list','update','pause','resume','cancel','sync','notifications'])
    parser.add_argument('--id')
    parser.add_argument('--name')
    parser.add_argument('--cron')
    parser.add_argument('--timezone')
    parser.add_argument('--kind',choices=['agent','watch'],default='watch')
    parser.add_argument('--prompt')
    parser.add_argument('--max-runs',type=int)
    parser.add_argument('--timeout',type=float)
    parser.add_argument('--overlap',choices=['SKIP','BUFFER_ONE'])
    args = parser.parse_args(argv)
    schedules = Schedules()
    if args.action=='list':
        print(canonical(schedules.list()))
        return 0
    if args.action=='notifications':
        print(canonical(schedules.scheduler.notifications()))
        return 0
    if args.action=='create':
        if not args.name or not args.cron or not args.prompt:
            parser.error('create requires --name, --cron and --prompt')
        key = schedules.create(args.name,args.cron,args.kind,{'prompt':args.prompt},
            timezone=args.timezone or 'Asia/Taipei',max_runs=1000 if args.max_runs is None else args.max_runs,
            timeout=60 if args.timeout is None else args.timeout,overlap=args.overlap or 'SKIP')
    else:
        if not args.id:
            parser.error('--id is required')
        key = args.id
        schedules.get(key)  # Enforce owner before the internal trusted reconcile API.
        if args.action!='sync':
            values = {k:v for k,v in {'cron':args.cron,'timezone':args.timezone,
                'max_runs':args.max_runs,'timeout':args.timeout,'overlap':args.overlap}.items() if v is not None}
            schedules.change(key,args.action,**values)
    try:
        asyncio.run(schedules.sync(key))
    except Exception as error:
        print(canonical({'id':key,'sync':'pending','error_type':type(error).__name__}))
        return 2
    print(canonical({'id':key,'sync':'applied'}))
    return 0

if __name__=='__main__':
    raise SystemExit(main())
