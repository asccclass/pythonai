"""Structured execution facts; successful transport is not task verification."""
from dataclasses import dataclass, field, asdict
import json
import subprocess


@dataclass(frozen=True)
class ToolResult:
    success: bool
    output: str = ''
    exit_code: int | None = None
    error: str = ''
    artifacts: list = field(default_factory=list)
    side_effects: dict = field(default_factory=lambda:{'status':'unknown'})
    evidence: dict = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)

    def __str__(self):
        return self.output if self.success else 'Error: '+(self.error or self.output or 'Tool failed')


def normalize_result(value, tool_name=''):
    if isinstance(value,ToolResult):return value
    if isinstance(value,subprocess.CompletedProcess):
        stdout=str(value.stdout or '')
        stderr=str(value.stderr or '')
        return ToolResult(value.returncode==0,stdout,value.returncode,stderr if value.returncode else '',
            evidence={'kind':'process','returncode':value.returncode})
    if isinstance(value,dict):
        if not isinstance(value.get('side_effects',{}),dict) or not isinstance(value.get('evidence',{}),dict):
            return ToolResult(False,error='Invalid structured tool evidence or side effects')
        output=value.get('stdout',value.get('output',json.dumps(value,ensure_ascii=False,default=str)))
        code=value.get('exit_code',value.get('returncode'))
        if code is not None and (type(code) is not int):
            return ToolResult(False,str(output),error='Invalid exit code in tool result')
        success=value.get('success',not bool(value.get('isError') or value.get('error')))
        if type(success) is not bool:
            return ToolResult(False,str(output),error='Tool success must be a boolean')
        success=success and code in {None,0}
        return ToolResult(success,str(output),code,str(value.get('error') or value.get('stderr') or '') if not success else '',
            value.get('artifacts',[]) if isinstance(value.get('artifacts',[]),list) else [],
            value.get('side_effects',{'status':'unknown'}),value.get('evidence',{}))
    text=str(value) if isinstance(value,str) else json.dumps(value,ensure_ascii=False,default=str)
    failed=text.lstrip().lower().startswith(('error:','fatal error','⛔','❌'))
    # Test success needs a real process/report, never a reassuring string.
    if tool_name=='run_test_suite':
        return ToolResult(False,text,error='Test output has no structured execution evidence')
    return ToolResult(not failed,text,error=text if failed else '')
