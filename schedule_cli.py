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
    add_parser.add_argument("--cron", required=True, help="Cron expression (e.g. '0 0 * * *')")
    add_parser.add_argument("--type", required=True, help="Job type (e.g. command, script, maintenance.summary)")
    add_parser.add_argument("--payload", default="{}", help="JSON payload")
    add_parser.add_argument("--desc", default="")

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
            cron_expression=args.cron,
            job_type=args.type,
            job_payload=payload,
            description=args.desc
        )
        print(f"Added job {args.name} with ID {job_id}")

if __name__ == "__main__":
    main()