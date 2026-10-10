"""Immutable, approved Git delivery plans and head-bound CI/review reconciliation."""
from contextlib import closing
import json
import os
from pathlib import Path
import re
import tempfile
import time

from .patches import ArtifactStore,digest
from .policy import current_context,git_workspace
from .tasks import _active
from .acceptance import load_contract,verify_contract,CODE_SUFFIXES
from .worktrees import git,selected_files
from .process import run_process
from .git_provider import configuration,GitProvider,ProviderError


def client(plan):
    config=configuration()
    if config!=plan['provider']:raise ValueError('Provider configuration changed; prepare a new plan')
    repository=git_workspace()
    if str(repository)!=plan['workspace'] or git(repository,'remote','get-url',config['remote']).strip()!=config['remote_url']:raise ValueError('Delivery repository/remote changed')
    return GitProvider(config)


def local_checks(repository,criteria,files):
    from tools.run_test_suite import run_test_suite
    if not criteria.get('checks'):raise ValueError('Delivery requires frozen acceptance checks')
    if any(Path(name).suffix.lower() in CODE_SUFFIXES for name in files) and not any(check.get('kind')=='tests' for check in criteria['checks']):raise ValueError('Code delivery requires an explicit tests acceptance check')
    results,errors,evidence=verify_contract(criteria,repository,run_test_suite)
    if errors or not results or not all(results.values()):raise ValueError('Local acceptance failed; delivery is not verified')
    return {'checks':results,'evidence':evidence}


def prepare_delivery(files,branch,title,body,previous_id=''):
    repository=git_workspace();config=configuration();store=ArtifactStore()
    if git(repository,'remote','get-url',config['remote']).strip()!=config['remote_url']:raise ValueError('Configured Git remote does not match repository')
    selected=selected_files(repository,files)
    if not isinstance(title,str) or not 0<len(title)<=500 or not isinstance(body,str) or len(body.encode())>16000:raise ValueError('Invalid delivery title/body')
    if not isinstance(branch,str) or not branch.startswith('codex/'):raise ValueError('Delivery branches require codex/ prefix')
    git(repository,'check-ref-format','--branch',branch)
    if git(repository,'status','--porcelain').strip():raise ValueError('Commit task changes in a clean worktree before preparing delivery')
    head=git(repository,'rev-parse','HEAD').strip()
    base=git(repository,'rev-parse','--verify',config['base_ref']+'^{commit}').strip()
    git(repository,'merge-base','--is-ancestor',base,head)
    changed=[name for name in git(repository,'diff','--name-only','--no-renames','-z',base,head,'--').split('\x00') if name]
    # Push transfers history too: include transient files absent from the final diff.
    commits=git(repository,'rev-list',base+'..'+head).splitlines()
    if not 1<=len(commits)<=256:raise ValueError('Delivery requires 1..256 new commits')
    history=set()
    for commit in commits:
        history.update(name for name in git(repository,'diff-tree','--root','-m','--no-commit-id','--name-only','--no-renames','-r','-z',commit,'--').split('\x00') if name)
    if history!=set(selected) or not changed:raise ValueError('Select exactly all files changed in the published commit history')
    diff=git(repository,'diff','--no-ext-diff','--no-textconv','--no-renames',base,head,'--')
    history_diff=git(repository,'log','--reverse','--format=commit %H','-m','--patch','--no-ext-diff','--no-textconv','--no-renames',base+'..'+head,'--')
    active=_active.get()
    criteria=active[0].get(active[1],active[2])['criteria'] if active else load_contract(title)
    pull=None;previous_head='';feedback=[]
    if previous_id:
        old=store.get(previous_id,'delivery',strict_task=False)
        if old['state'] not in {'published','ready','ci_pending_or_failed','review_pending','stale'}:raise ValueError('Reconcile previous publication before preparing repair')
        old_plan=old['payload']
        if config!=old_plan['provider'] or branch!=old_plan['branch'] or base!=old_plan['base_commit'] or not old_plan.get('pull'):raise ValueError('Repair must preserve provider, branch, base and pull request')
        if head==old_plan['head']:raise ValueError('Repair requires a new committed head')
        git(repository,'merge-base','--is-ancestor',old_plan['head'],head)
        checks=list(criteria['checks'])
        for check in old_plan['criteria']['checks']:
            if check not in checks:checks.append(check)
        criteria={**criteria,'checks':checks}
        pull=old_plan['pull'];previous_head=old_plan['head'];feedback=old_plan.get('observation',{}).get('findings',[])
    acceptance=local_checks(repository,criteria,selected)
    # Tests must not change the bytes about to be published.
    if git(repository,'status','--porcelain').strip() or git(repository,'rev-parse','HEAD').strip()!=head:raise ValueError('Acceptance changed the committed workspace')
    plan={'workspace':str(repository),'provider':config,'files':selected,'branch':branch,'base':config['base'],'base_commit':base,
        'head':head,'commits':commits,'history_diff':history_diff,'title':title,'body':body,'diff':diff,'criteria':criteria,'local_acceptance':acceptance,
        'pull':pull,'previous_id':previous_id,'previous_head':previous_head,'feedback':feedback}
    plan['digest']=digest(json.dumps(plan,sort_keys=True,ensure_ascii=False).encode())
    key=store.add('delivery',plan)
    return {'plan_id':key,'state':'prepared','head':head,'base':base,'files':selected,'diff':diff,'history_diff':history_diff,
        'local_acceptance':acceptance,'feedback':feedback,'digest':plan['digest'],'verified_delivery':False}


def remote_head(plan):
    result=run_process(['git','ls-remote','--exit-code',plan['provider']['remote'],'refs/heads/'+plan['branch']],cwd=plan['workspace'],
        env={**os.environ,'GIT_TERMINAL_PROMPT':'0','GCM_INTERACTIVE':'never'},timeout=15)
    if result.returncode==2:return ''
    if result.returncode:raise ProviderError('Cannot verify remote branch')
    lines=result.stdout.strip().splitlines()
    if len(lines)!=1 or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}',lines[0].split()[0]):raise ProviderError('Invalid remote branch response')
    return lines[0].split()[0]


def verify_pull(plan,pull):
    if not pull or pull['sha']!=plan['head'] or pull['branch']!=plan['branch'] or pull['base']!=plan['base'] or not pull['open']:raise ProviderError('Pull request does not match approved plan')
    if not isinstance(pull['url'],str) or not pull['url'].startswith(('https://','http://')):raise ProviderError('Invalid pull request URL')


def publish_delivery(key):
    store=ArtifactStore();row=store.get(key,'delivery');plan=row['payload'];provider=client(plan)
    if row['state']!='prepared':raise ValueError('Publication already started; reconcile without replaying mutations')
    if git(plan['workspace'],'rev-parse','HEAD').strip()!=plan['head'] or git(plan['workspace'],'status','--porcelain').strip():raise ValueError('Prepared commit/workspace changed')
    current=remote_head(plan)
    if current!=plan['previous_head']:raise ValueError('Remote branch changed; no overwrite')
    if plan['pull']:
        previous=provider.pull(plan['pull'])
        if previous['sha']!=plan['previous_head'] or previous['branch']!=plan['branch'] or previous['base']!=plan['base'] or not previous['open']:raise ValueError('Previous pull request changed')
    elif provider.find(plan['branch'],plan['base']):raise ValueError('Pull request already exists; reconcile explicitly')
    store.update(row,'publishing',expected='prepared')
    try:
        # Exact SHA/ref and lease, never broad branch push; disable local pre-push hooks.
        with tempfile.TemporaryDirectory(prefix='pythonai-push-') as directory:
            result=run_process(['git','-c','core.hooksPath='+directory,'push','--force-with-lease=refs/heads/'+plan['branch']+':'+plan['previous_head'],
                plan['provider']['remote'],plan['head']+':refs/heads/'+plan['branch']],cwd=plan['workspace'],timeout=30,
                env={**os.environ,'GIT_TERMINAL_PROMPT':'0','GCM_INTERACTIVE':'never'})
        if result.returncode:raise ProviderError('Scoped Git push failed; inspect remote outcome')
        pull=provider.publish(plan);verify_pull(plan,pull)
        updated={**plan,'pull':pull['number'],'url':pull['url']}
        store.update(row,'published',updated,expected='publishing')
        return {'plan_id':key,'state':'published','url':pull['url'],'head':plan['head'],'verified_delivery':False}
    except BaseException:
        store.update(row,'needs_reconciliation',expected='publishing');raise


def inspect_delivery(key):
    store=ArtifactStore();row=store.get(key,'delivery');plan=row['payload'];provider=client(plan)
    if row['state'] not in {'published','ready','ci_pending_or_failed','review_pending','stale'}:raise ValueError('Publish/reconcile this plan before inspecting CI')
    observation=provider.inspect(plan)
    store.update(row,observation['state'],{**plan,'observation':observation},expected=row['state'])
    return {'plan_id':key,**observation,'url':plan.get('url')}


def merge_delivery(key):
    store=ArtifactStore();row=store.get(key,'delivery');plan=row['payload'];provider=client(plan)
    if row['state']!='ready':raise ValueError('Delivery is not ready for merging')
    # Recheck live provider evidence after approval; no cached-CI merge.
    observation=provider.inspect(plan)
    if not observation['ready']:
        store.update(row,observation['state'],{**plan,'observation':observation},expected='ready')
        raise ValueError('CI, review or pull head changed after approval')
    store.update(row,'merging',expected='ready')
    try:
        evidence=provider.merge(plan)
        result={'plan_id':key,'state':'merged','head':plan['head'],'url':plan['url'],'verified_delivery':True,
            'merge':evidence,'local_acceptance':plan['local_acceptance'],'ci':observation['ci'],'review':observation}
        store.update(row,'merged',{**plan,'observation':observation,'delivery_summary':result},expected='merging')
        return result
    except BaseException:
        store.update(row,'needs_reconciliation',expected='merging');raise


def reconcile_delivery(key):
    store=ArtifactStore();row=store.get(key,'delivery');plan=row['payload'];provider=client(plan)
    if row['state'] not in {'publishing','merging','needs_reconciliation'}:raise ValueError('No uncertain operation to reconcile')
    # Read-only recovery. A previously uncertain merge is not labelled verified.
    pull=provider.pull(plan['pull']) if plan.get('pull') else provider.find(plan['branch'],plan['base'])
    if pull and pull['merged'] and pull['sha']==plan['head'] and pull['branch']==plan['branch'] and pull['base']==plan['base']:
        store.update(row,'merged_unverified',{**plan,'reconciliation':pull},expected=row['state'])
        return {'plan_id':key,'state':'merged_unverified','verified_delivery':False,'pull':pull}
    verify_pull(plan,pull)
    if remote_head(plan)!=plan['head']:raise ProviderError('Remote branch differs from prepared commit')
    store.update(row,'published',{**plan,'pull':pull['number'],'url':pull['url']},expected=row['state'])
    return {'plan_id':key,'state':'published','url':pull['url'],'verified_delivery':False}


def human_delivery_command(text):
    from permissions import current_role
    parts=text.split(maxsplit=3)
    if len(parts)<2:raise ValueError('Usage: /delivery <artifact_id> status|abandon [inspection_note]')
    store=ArtifactStore();row=store.get(parts[1],strict_task=False)
    action=parts[2] if len(parts)>2 else 'status'
    if action=='status':return json.dumps(row,ensure_ascii=False)
    if action!='abandon' or current_role()!='admin' or len(parts)!=4 or not parts[3].strip() or len(parts[3].encode())>16000:raise ValueError('Admin inspection note required for abandonment')
    if row['state'] not in {'needs_resolution','needs_reconciliation','applying','publishing','merging'}:raise ValueError('Artifact is not awaiting recovery')
    if row['updated']+180>time.time():raise ValueError('Wait 180 seconds after the last operation and inspect outstanding workers before abandonment')
    payload={**row['payload'],'human_disposition':parts[3]}
    store.update(row,'abandoned',payload,expected=row['state'])
    return 'Artifact abandoned after human inspection. Files, remote branch/PR and evidence are retained; no mutation was replayed.'
