"""Check tracked Python maintenance/application source without importing it."""
import ast
from pathlib import Path
from ..process import run_process

def check_syntax(root):
    root=Path(root).resolve();result=run_process(['git','ls-files','--','*.py'],cwd=root,timeout=10,check=True)
    failures=[];checked=0
    for name in result.stdout.splitlines():
        source=root/name
        if not source.is_file():continue
        try:
            raw=source.read_bytes()
            try:text=raw.decode('utf-8-sig')
            except UnicodeDecodeError:text=raw.decode('cp950')
            ast.parse(text,filename=name);checked+=1
        except Exception as error:failures.append({'file':name,'error':type(error).__name__,'line':getattr(error,'lineno',None)})
    return {'passed':not failures,'checked':checked,'failures':failures}
