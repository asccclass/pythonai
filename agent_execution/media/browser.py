import hashlib
import json
import os
from pathlib import Path
import tempfile
import uuid
import sys
from ..policy import current_context,resolve_path
from ..tasks import task_owner
from ..process import run_process


def browse(url,actions,session_id='default',screenshot=False):
    if not isinstance(session_id,str) or not 1<=len(session_id)<=128:raise ValueError('Invalid browser session')
    if not isinstance(url,str) or len(url)>8192 or not isinstance(actions,list) or len(actions)>32:raise ValueError('Invalid browser request')
    data=json.dumps(actions,allow_nan=False)
    if len(data.encode())>65536:raise ValueError('Browser actions exceed 64 KiB')
    key=hashlib.sha256((task_owner(current_context())+'\n'+session_id).encode()).hexdigest()
    root=Path(os.environ.get('AGENT_BROWSER_ROOT') or Path(os.environ.get('LOCALAPPDATA',Path.home()))/'PythonAI'/'browser')
    root.mkdir(parents=True,exist_ok=True);profile=root/key
    if not profile.resolve().is_relative_to(root.resolve()):raise PermissionError('Browser profile path escape')
    # Downloads may contain login-protected data: never place them in a shared workspace.
    artifacts=root/(key+'.artifacts')/uuid.uuid4().hex;artifacts.mkdir(parents=True)
    if not artifacts.resolve().is_relative_to(root.resolve()):raise PermissionError('Browser artifact path escape')
    with tempfile.TemporaryDirectory(prefix='pythonai-browser-') as directory:
        request=Path(directory)/'request.json';response=Path(directory)/'response.json'
        request.write_text(json.dumps({'url':url,'actions':actions,'profile':str(profile),'artifacts':str(artifacts),'screenshot':screenshot,
            'allowed_hosts':os.environ.get('AGENT_BROWSER_ALLOWED_HOSTS','').split(',')}),encoding='utf-8')
        result=run_process([sys.executable,str(Path(__file__).with_name('browser_worker.py')),str(request),str(response)],timeout=60,max_output_bytes=65536)
        if result.returncode or not response.exists():raise RuntimeError('Browser worker stopped: '+str(result.returncode))
        if response.stat().st_size>1024*1024:raise ValueError('Browser result capacity exceeded')
        value=json.loads(response.read_text(encoding='utf-8'))
        if not value.get('success'):raise RuntimeError(value.get('error','Browser action failed'))
        for path in value['artifacts']:
            target=Path(path)
            if not target.resolve().is_relative_to(artifacts.resolve()) or target.is_symlink() or not target.is_file() or target.stat().st_size>10*1024*1024:raise PermissionError('Invalid browser artifact')
        return value
