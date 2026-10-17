"""Read-only configuration/backup inventory. Never creates databases or connects."""
import importlib.metadata
import json
import os
from pathlib import Path

from .config import Settings, selected_backend, operation_limits


def inspect():
    root = Path(os.environ.get('LOCALAPPDATA') or Path.home()/'.local'/'share') / 'PythonAI'
    repository = Path(__file__).resolve().parents[2]
    paths = {
        'tasks': Path(os.environ.get('AGENT_TASK_DB') or root/'tasks.db'),
        'approvals': Path(os.environ.get('AGENT_APPROVAL_DB') or root/'approvals.db'),
        'schedules': Path(os.environ.get('SCHEDULER_DB_PATH') or repository/'communication'/'scheduler.db'),
        'tenants': Path(os.environ.get('AGENT_TENANT_ROOT') or root/'tenants'),
    }
    try:
        sdk = importlib.metadata.version('temporalio')
    except importlib.metadata.PackageNotFoundError:
        sdk = None
    settings = Settings.load()
    return {'backend': selected_backend(), 'sdk_version': sdk,
            'address': settings.address, 'namespace': settings.namespace,
            'mode': 'read_only_bridge', 'connected': False,
            'operation_limits': operation_limits(),
            'backup_inventory': {name: {'path': str(path.resolve()), 'exists': path.exists()}
                                 for name, path in paths.items()},
            'note': 'Inventory only. Back up SQLite consistently (including WAL via backup API); no backup was created.'}


def main():
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[2] / '.env')
    print(json.dumps(inspect(), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
