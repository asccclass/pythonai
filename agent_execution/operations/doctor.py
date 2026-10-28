import importlib.util
import json
import os
from pathlib import Path
import shutil
import tempfile
from .backup import inspect_database


def readiness():
    from ..policy import resolve_path
    checks={};errors=[]
    try:
        root=resolve_path('.')
        with tempfile.TemporaryFile(dir=root) as probe:probe.write(b'readiness')
        checks['workspace']=True
    except Exception as error:checks['workspace']=False;errors.append({'check':'workspace','error':type(error).__name__})
    required=['openai','fastapi','croniter','jsonschema']
    checks['dependencies']=all(importlib.util.find_spec(name) is not None for name in required)
    if os.environ.get('AGENT_EXECUTION_MODE')=='container':checks['docker_command']=shutil.which('docker') is not None
    for variable in ('AGENT_TASK_DB','SCHEDULER_DB_PATH','AGENT_APPROVAL_DB','COMMUNICATION_DB_PATH',
                     'AGENT_EXTENSION_DB','AGENT_IMPROVEMENT_DB','AGENT_TRACE_DB','AGENT_MCP_OAUTH_DB'):
        path=os.environ.get(variable)
        if not path:continue
        try:
            target=Path(path)
            if target.exists():inspect_database(target)
            else:
                parent=target.parent
                if not parent.exists():raise FileNotFoundError('Configured storage parent missing')
                with tempfile.TemporaryFile(dir=parent):pass
            checks[variable]=True
        except Exception as error:checks[variable]=False;errors.append({'check':variable,'error':type(error).__name__})
    temporal = None
    from ..temporal_backend.config import selected_backend
    try:
        backend = selected_backend()
    except Exception as error:
        checks['backend_config'] = False
        errors.append({'check':'backend_config','error':type(error).__name__})
        backend = None
    if backend == 'temporal':
        try:
            from ..temporal_backend.readiness import inspect_sync
            temporal = inspect_sync()
        except Exception as error:
            temporal = {'connected': False, 'ready': False, 'error_type': type(error).__name__,
                        'scope': 'Temporal poller probe failed; details omitted'}
        checks['temporal_service'] = temporal.get('connected', False)
        checks['temporal_pollers'] = temporal.get('ready', False)
    report = {'ready':all(checks.values()),'checks':checks,'errors':errors,'provider':'not probed','scope':'local readiness; not an external service guarantee'}
    if temporal is not None:
        report['temporal'] = temporal
    return report


def dependency_doctor():
    report=readiness()
    report['optional']={name:importlib.util.find_spec(name) is not None for name in ('playwright','pdfplumber','openpyxl','pypdfium2')}
    report['commands']={name:bool(shutil.which(name)) for name in ('git','docker','tesseract','node')}
    from ..extensions.store import ExtensionStore
    from ..improvement.store import ImprovementStore
    try:report['active_extensions']=len(ExtensionStore().active());report['active_improvements']=len(ImprovementStore().active())
    except Exception as error:report['extension_storage_error']=type(error).__name__;report['ready']=False
    return report
