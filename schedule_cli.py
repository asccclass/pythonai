import argparse
import json
from scheduler import SchedulerStore

def main():
    parser = argparse.ArgumentParser(description="Manage scheduled cron jobs")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # list
    subparsers.add_parser("list", help="List all scheduled jobs")

    # add
    add_parser = subparsers.add_parser("add", help="Add a new job")
    add_parser.add_argument("--name", required=True)
    when = add_parser.add_mutually_exclusive_group(required=True)
    when.add_argument("--cron", help="Five-field cron")
    when.add_argument("--schedule", help="每天 HH:MM / every day at HH:MM / every N minutes")
    add_parser.add_argument('--timezone',default='Asia/Taipei')
    add_parser.add_argument('--timeout',type=float,default=60)
    add_parser.add_argument('--max-runs',type=int,default=1000)
    add_parser.add_argument("--type", required=True, help="Job type (e.g. command, script, maintenance.summary)")
    add_parser.add_argument("--payload", default="{}", help="JSON payload")
    add_parser.add_argument("--desc", default="")
    disable_parser=subparsers.add_parser('disable');disable_parser.add_argument('job_id')
    subparsers.add_parser('notifications')

    # run-now (or just remove/enable/disable - keep it simple for now)
    
    args = parser.parse_args()
    store = SchedulerStore()

    if args.command == "list":
        jobs = store.list_jobs()
        if not jobs:
            print("No scheduled jobs found.")
        else:
            for j in jobs:
                print(f"[{j['id'][:8]}] {j['name']} ({j['cron_expression']}) - Type: {j['job_type']}")
                print(f"  Next run: {j['next_run_at']}")
                print(f"  Enabled: {'Yes' if j['is_enabled'] else 'No'}")
                print()
    elif args.command == "add":
        payload = json.loads(args.payload)
        job_id = store.add_job(
            name=args.name,
            cron_expression=args.cron or __import__('agent_execution.scheduling.store',fromlist=['natural_cron']).natural_cron(args.schedule),
            job_type=args.type,
            job_payload=payload,
            description=args.desc,timezone=args.timezone,timeout=args.timeout,max_runs=args.max_runs
        )
        print(f"Added job {args.name} with ID {job_id}")
    elif args.command=='disable':print(json.dumps({'disabled':store.disable(args.job_id)}))
    elif args.command=='notifications':print(json.dumps(store.notifications(),ensure_ascii=False))

if __name__ == "__main__":
    main()
