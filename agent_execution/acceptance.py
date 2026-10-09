"""Trusted, frozen task acceptance contracts and observable checks."""
import hashlib
import json
import os
from pathlib import Path

CODE_SUFFIXES={'.py','.go','.js','.jsx','.ts','.tsx','.java','.rs','.c','.cpp','.cs'}


def validate_contract(value):
    if not isinstance(value,dict) or type(value.get('version')) is not int or value.get('version')!=1:
        raise ValueError('Acceptance contract requires version 1')
    checks=value.get('checks')
    if not isinstance(checks,list) or len(checks)>32:
        raise ValueError('Acceptance checks must be a list with at most 32 entries')
    for check in checks:
        if not isinstance(check,dict) or check.get('kind') not in {'file_exists','file_contains','file_sha256','tests'}:
            raise ValueError('Unsupported acceptance check')
        field='target' if check['kind']=='tests' else 'path'
        if not isinstance(check.get(field),str) or not check[field]:
            raise ValueError(f'Acceptance check requires {field}')
        if check['kind']=='file_contains' and (not isinstance(check.get('text'),str) or not check['text']):
            raise ValueError('file_contains requires nonempty text')
        if check['kind']=='file_sha256' and (not isinstance(check.get('sha256'),str) or len(check['sha256'])!=64):
            raise ValueError('file_sha256 requires a SHA-256 hex digest')
        if check['kind']=='tests' and (type(check.get('min_tests',1)) is not int or check.get('min_tests',1)<1):
            raise ValueError('tests requires at least one test')
    return json.loads(json.dumps(value))


def load_contract(goal):
    from .policy import resolve_path
    name=os.environ.get('AGENT_ACCEPTANCE_FILE','.agent/acceptance.json')
    path=resolve_path(name)
    if not path.exists():return {'goal':goal,'version':1,'checks':[]}
    if path.stat().st_size>65536:raise ValueError('Acceptance contract exceeds 64 KiB')
    return {**validate_contract(json.loads(path.read_text(encoding='utf-8'))),'goal':goal}


def verify_contract(contract,workspace,run_tests):
    contract=validate_contract(contract)
    root=Path(workspace).resolve()
    results={}
    errors=[]
    evidence=[]
    if any(check['kind']!='tests' and Path(check['path']).suffix.lower() in CODE_SUFFIXES for check in contract['checks']) and not any(check['kind']=='tests' for check in contract['checks']):
        results['acceptance:tests_required']=False
        errors.append('Code acceptance requires an explicit tests check')
    for index,check in enumerate(contract['checks']):
        key=f"acceptance:{index}:{check['kind']}"
        try:
            if check['kind']=='tests':
                path=(root/check['target']).resolve()
                if not path.is_relative_to(root):raise PermissionError('Acceptance tests outside workspace')
                from .results import normalize_result
                result=normalize_result(run_tests(check['target']),'run_test_suite')
                tests=result.evidence.get('tests',{})
                passed=result.success and result.exit_code==0 and tests.get('passed',0)>=check.get('min_tests',1)
                evidence.append(result.to_dict())
            else:
                path=(root/check['path']).resolve()
                if not path.is_relative_to(root):raise PermissionError('Acceptance path outside workspace')
                passed=path.is_file()
                if passed and check['kind']!='file_exists':
                    if path.stat().st_size>8*1024*1024:raise ValueError('Acceptance file exceeds 8 MiB')
                    if check['kind']=='file_contains':passed=check['text'] in path.read_text(encoding='utf-8')
                    else:passed=hashlib.sha256(path.read_bytes()).hexdigest()==check['sha256'].lower()
                evidence.append({'kind':check['kind'],'path':check['path'],'passed':passed})
            results[key]=bool(passed)
            if not passed:errors.append(f'Acceptance check failed: {key}')
        except Exception as error:
            from .budgets import BudgetExhausted
            if isinstance(error,BudgetExhausted):raise
            results[key]=False
            errors.append(f'Acceptance check unavailable: {key} ({type(error).__name__})')
    return results,errors,evidence
