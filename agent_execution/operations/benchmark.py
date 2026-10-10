import hashlib
import json
from pathlib import Path
import sys
import tempfile
from ..process import run_process


def source_snapshot(root):
    listed=run_process(['git','ls-files','-z'],cwd=root,timeout=10,check=True)
    return {name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in listed.stdout.split('\0') if name and (root/name).is_file()}


def run_benchmark():
    root=Path(__file__).resolve().parents[2];before=source_snapshot(root)
    with tempfile.TemporaryDirectory(prefix='pythonai-benchmark-') as directory:
        result=Path(directory)/'result.json'
        completed=run_process([sys.executable,str(Path(__file__).with_name('benchmark_worker.py')),str(result)],cwd=root,timeout=120,max_output_bytes=65536)
        if completed.returncode or not result.is_file() or result.stat().st_size>1024*1024:raise RuntimeError('Fixed benchmark worker failed')
        report=json.loads(result.read_text(encoding='utf-8'))
    after=source_snapshot(root);changes=sum(before.get(name)!=after.get(name) for name in set(before)|set(after))
    report['unexpected_tracked_source_changes']=changes;report['passed']=report['passed'] and changes==0
    report['side_effect_scope']='tracked repository bytes; other effects are guarded by individual case assertions, not a global OS monitor'
    return report
