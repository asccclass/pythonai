"""Trusted benchmark adapters execute in bounded native workers, without auto retry."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from ..process import run_process


def configured_evaluate(store,key):
    runner=Path(os.environ['AGENT_IMPROVEMENT_RUNNER']).resolve()
    cases_path=Path(os.environ['AGENT_IMPROVEMENT_CASES']).resolve()
    if not runner.is_file() or runner.is_symlink() or runner.stat().st_size>1024*1024 or cases_path.stat().st_size>65536:raise ValueError('Invalid trusted benchmark configuration')
    cases=json.loads(cases_path.read_text(encoding='utf-8'));deadline=time.monotonic()+120
    def call(case,proposals):
        from ..policy import current_context,resolve_path
        from permissions import current_role
        remaining=deadline-time.monotonic()
        if remaining<=0:raise TimeoutError('A/B suite deadline exceeded')
        with tempfile.TemporaryDirectory(prefix='pythonai-improvement-') as directory:
            request=Path(directory)/'request.json';result=Path(directory)/'result.json'
            scope=current_context()
            context={k:getattr(scope,k) for k in ('source','identity','project','session')};context['workspace']=str(resolve_path('.'))
            request.write_text(json.dumps({'runner':str(runner),'case':case,'proposals':proposals,'context':context,'role':current_role()}),encoding='utf-8')
            completed=run_process([sys.executable,str(Path(__file__).with_name('worker.py')),str(request),str(result)],timeout=min(remaining,15),max_output_bytes=65536)
            if completed.returncode or not result.is_file() or result.stat().st_size>65536:raise RuntimeError('Benchmark worker failed')
            return json.loads(result.read_text(encoding='utf-8'))
    return store.evaluate(key,cases,call)
