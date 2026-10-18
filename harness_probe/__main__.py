"""JSON command-line interface; stdout always contains one result object."""
import argparse
import json
from pathlib import Path
import sys

from .runner import ProbeRunner, read_json, validate_task


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--task', required=True, help='Task JSON file, or - for stdin')
    parser.add_argument('--runs-root', required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--harness-dir')
    parser.add_argument('--timeout', type=float, default=300)
    args = parser.parse_args()
    try:
        task = json.load(sys.stdin) if args.task == '-' else read_json(args.task)
        validate_task(task)
        if task.get('files_dir') and args.task != '-':
            task['files_dir'] = str((Path(args.task).resolve().parent / task['files_dir']).resolve())
        run_dir = ProbeRunner(args.runs_root).execute(task, args.harness_dir, args.run_id, timeout=args.timeout)
        result = read_json(run_dir / 'result.json')
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result['status'] == 'completed' else 1
    except (OSError, ValueError, RuntimeError) as exc:
        print(json.dumps({'schema_version': 1, 'status': 'invalid_request', 'error': str(exc)}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    sys.exit(main())
