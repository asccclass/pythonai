"""Trusted per-owner legacy schedule migration; preview is read-only by default."""
import argparse
import asyncio
from contextlib import closing
from pathlib import Path
from .schedule_migration import preview,Migration
from .store import canonical

def main(argv=None):
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[2]/'.env')
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['preview','stage','cutover','rollback','reconcile'])
    parser.add_argument('--id',required=True)
    parser.add_argument('--digest',help='source_digest from the reviewed preview, required for stage')
    parser.add_argument('--at',help='Read-only preview base time; never a backfill request')
    args=parser.parse_args(argv)
    if args.action=='preview':
        print(canonical(preview(args.id,at=args.at)))
        return 0
    if args.at:parser.error('--at is only accepted for preview')
    if args.action=='stage' and not args.digest:parser.error('--digest is required for stage')
    migration=Migration()
    if args.action=='stage':
        migration.stage(args.id,args.digest)
    elif args.action=='cutover':
        migration.cutover(args.id)
    elif args.action=='rollback':
        migration.rollback(args.id)
    else:
        # Check current ownership before the trusted Worker-facing apply method.
        with closing(migration.schedules.scheduler.connect()) as conn:
            migration.row(conn,args.id)
    try:
        asyncio.run(migration.apply(args.id))
    except Exception as error:
        print(canonical({'id':args.id,'state':'pending','error_type':type(error).__name__}))
        return 2
    with closing(migration.schedules.scheduler.connect()) as conn:
        from .schedule_migration import migration as saved
        state=saved(conn,args.id)['state']
    print(canonical({'id':args.id,'state':state}))
    return 0

if __name__=='__main__':
    raise SystemExit(main())
