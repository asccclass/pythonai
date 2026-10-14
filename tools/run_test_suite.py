"""Run tests with process status and a machine-readable report."""
from pathlib import Path
import tempfile
import sys
import json
import xml.etree.ElementTree as ET

from agent_execution import policy_tool, resolve_path
from agent_execution.process import run_process
from agent_execution.results import ToolResult


@policy_tool('run_test_suite')
def run_test_suite(test_target: str, framework: str = 'auto'):
    target=resolve_path(test_target)
    if not target.exists():return ToolResult(False,error=f"Test target '{test_target}' does not exist")
    framework=framework.lower()
    if framework=='auto':framework='go' if target.suffix=='.go' or (target.is_dir() and (target/'go.mod').is_file()) else 'pytest'
    if framework not in {'pytest','python','go'}:
        return ToolResult(False,error='Unsupported test framework')
    try:
        if framework=='go':
            directory=target if target.is_dir() else target.parent
            process=run_process(['go','test','-json','./...'],cwd=directory,capture_output=True,text=True,timeout=30)
            counts={'passed':0,'failed':0,'errors':0,'skipped':0}
            for line in (process.stdout or '').splitlines():
                try:event=json.loads(line)
                except ValueError:continue
                if isinstance(event,dict) and event.get('Test'):
                    key={'pass':'passed','fail':'failed','skip':'skipped'}.get(event.get('Action'))
                    if key:counts[key]+=1
            success=process.returncode==0 and counts['passed']>0 and not counts['failed']
            return ToolResult(success,(process.stdout or '')+(process.stderr or ''),process.returncode,
                '' if success else 'Go tests failed or produced no passing test cases',
                evidence={'kind':'test_process','framework':'go','target':test_target,'tests':counts})
        with tempfile.TemporaryDirectory(prefix='pythonai-tests-') as directory:
            report=Path(directory)/'junit.xml'
            command=[sys.executable,'-m','pytest',str(target),'-q',f'--junitxml={report}'] if framework.lower()=='pytest' else [sys.executable,str(target)]
            process=run_process(command,cwd=resolve_path('.'),capture_output=True,text=True,timeout=30)
            counts={'passed':0,'failed':0,'errors':0,'skipped':0}
            if report.exists() and report.stat().st_size<=4*1024*1024:
                tree=ET.fromstring(report.read_bytes())
                for case in tree.iter('testcase'):
                    key='failed' if case.find('failure') is not None else 'errors' if case.find('error') is not None else 'skipped' if case.find('skipped') is not None else 'passed'
                    counts[key]+=1
            # A script exit code is evidence of execution, not collected test assertions.
            success=process.returncode==0 and counts['passed']>0 and not counts['failed'] and not counts['errors']
            output=(process.stdout or '')+(process.stderr or '')
            error='' if success else 'Tests failed, were cancelled, or produced no passing test cases'
            return ToolResult(success,output,process.returncode,error,
                evidence={'kind':'test_process','framework':framework,'target':test_target,'tests':counts})
    except Exception as error:
        from agent_execution.budgets import BudgetExhausted
        if isinstance(error,BudgetExhausted):raise
        return ToolResult(False,error=f'Test execution failed ({type(error).__name__})')
