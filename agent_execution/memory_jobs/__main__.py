"""Explicit offline maintenance; see this package's README before recovery."""
import argparse
import json
from . import inspect_jobs, recover


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['inspect','recover'])
    parser.add_argument('--database',required=True)
    parser.add_argument('--legacy',action='store_true')
    parser.add_argument('--stopped-pid',type=int,action='append',default=[])
    args = parser.parse_args()
    result = (inspect_jobs(args.database) if args.action=='inspect' else
              recover(args.database,legacy=args.legacy,stopped_pids=args.stopped_pid))
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':
    main()
