"""Bounded GitHub/GitLab REST clients. Provider data is evidence, never instructions."""
from datetime import datetime
import json
import os
import re
import time
from urllib.parse import quote,urlsplit,urlencode
from urllib.request import Request,build_opener,HTTPRedirectHandler
from urllib.error import HTTPError,URLError


class ProviderError(RuntimeError):pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None


def configuration():
    value=json.loads(os.environ.get('AGENT_GIT_PROVIDER_CONFIG','{}'))
    required={'kind','api_url','repository','remote_url','base','base_ref','required_ci','required_reviewers'}
    if not isinstance(value,dict) or not required<=set(value) or set(value)-required-{'remote','token_env','allow_http_loopback','api_version'}:raise ValueError('Invalid AGENT_GIT_PROVIDER_CONFIG')
    if value['kind'] not in {'github','gitlab'}:raise ValueError('Unsupported Git provider')
    parsed=urlsplit(value['api_url'])
    local=value.get('allow_http_loopback') is True and parsed.hostname in {'127.0.0.1','::1','localhost'}
    if (parsed.scheme!='https' and not (local and parsed.scheme=='http')) or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:raise ValueError('Provider API requires HTTPS without credentials/query/fragment')
    if not isinstance(value['repository'],str) or not re.fullmatch(r'[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+',value['repository']):raise ValueError('Invalid provider repository')
    if value['kind']=='github' and len(value['repository'].split('/'))!=2:raise ValueError('GitHub repository requires owner/name')
    if any(part in {'.','..'} for part in value['repository'].split('/')):raise ValueError('Invalid repository segment')
    for field in ('remote_url','base','base_ref'):
        if not isinstance(value[field],str) or not value[field] or '\x00' in value[field] or value[field].startswith('-'):raise ValueError('Invalid provider '+field)
    remote_url=urlsplit(value['remote_url'])
    if remote_url.scheme in {'http','https'} and (remote_url.username or remote_url.password):raise ValueError('Do not embed credentials in Git remote URLs')
    for field in ('required_ci','required_reviewers'):
        items=value[field]
        if not isinstance(items,list) or not 1<=len(items)<=32 or any(not isinstance(item,str) or not item or len(item)>200 for item in items) or len(set(items))!=len(items):raise ValueError('Configure nonempty unique '+field)
    prefix=('check:','status:') if value['kind']=='github' else ('job:',)
    if any(not item.startswith(prefix) for item in value['required_ci']):raise ValueError('CI names require check:/status: or job: prefix')
    remote=value.get('remote','origin')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+',remote) or remote.startswith('-'):raise ValueError('Invalid Git remote name')
    token_env=value.get('token_env','GITHUB_TOKEN' if value['kind']=='github' else 'GITLAB_TOKEN')
    if not isinstance(token_env,str) or not re.fullmatch('[A-Z][A-Z0-9_]*',token_env):raise ValueError('Invalid token environment name')
    return {**value,'api_url':value['api_url'].rstrip('/'),'remote':remote,'token_env':token_env,'api_version':value.get('api_version','2026-03-10')}


def finding(identifier,path,line,message,commit,resolved=False):
    from pathlib import Path
    if not isinstance(path,str) or not path or Path(path).is_absolute() or Path(path).drive or '..' in Path(path).parts or '.git' in Path(path).parts:raise ProviderError('Invalid review path')
    if type(line) is not int or line<1 or not isinstance(message,str) or not 0<len(message)<=16000:raise ProviderError('Invalid review location/message')
    return {'id':str(identifier),'path':path,'line':line,'message':message,'reproduction':message,'commit':commit,'resolved':resolved}


def timestamp(value):
    try:return datetime.fromisoformat(value.replace('Z','+00:00')).timestamp()
    except (ValueError,AttributeError):return None


class GitProvider:
    def __init__(self,config):
        self.config=config;self.kind=config['kind'];self.count=0;self.until=time.monotonic()+30
        self.prefix='/repos/'+config['repository'] if self.kind=='github' else '/projects/'+quote(config['repository'],safe='')

    def request(self,method,path,payload=None):
        from .budgets import active_budget
        from .policy import current_context
        if not path.startswith('/') or path.startswith('//'):raise ProviderError('Invalid provider path')
        self.count+=1
        budget=active_budget()
        if budget:budget.check()
        if current_context().check_cancelled and current_context().check_cancelled():raise InterruptedError('Provider operation cancelled')
        timeout=min(10,self.until-time.monotonic(),budget.remaining_seconds() if budget else 30)
        if self.count>32 or timeout<=0:raise ProviderError('Provider request/time budget exhausted')
        token=os.environ.get(self.config['token_env'])
        if not token:raise ProviderError('Provider token is not configured')
        headers={'Content-Type':'application/json','Accept':'application/vnd.github+json' if self.kind=='github' else 'application/json'}
        if self.kind=='github':headers.update(Authorization='Bearer '+token,**{'X-GitHub-Api-Version':self.config['api_version']})
        else:headers['PRIVATE-TOKEN']=token
        try:
            request=Request(self.config['api_url']+path,data=json.dumps(payload).encode() if payload is not None else None,headers=headers,method=method)
            with build_opener(NoRedirect()).open(request,timeout=timeout) as response:
                if response.headers.get_content_type()!='application/json':raise ProviderError('Provider returned non-JSON content')
                # read() can wait forever on a server trickling small chunks.
                # Stock urllib HTTP responses expose the connection's socket here.
                data=bytearray()
                while True:
                    if response.fp is None:break
                    if budget:budget.check()
                    if current_context().check_cancelled and current_context().check_cancelled():raise InterruptedError('Provider operation cancelled')
                    remaining=min(self.until-time.monotonic(),budget.remaining_seconds() if budget else 30)
                    if remaining<=0:raise ProviderError('Provider response time budget exhausted')
                    response.fp.raw._sock.settimeout(min(10,remaining))
                    chunk=response.read1(min(65536,2*1024*1024+1-len(data)))
                    if not chunk:break
                    data.extend(chunk)
                    if len(data)>2*1024*1024:raise ProviderError('Provider response exceeds 2 MiB')
                result=json.loads(data)
                if not isinstance(result,(dict,list)):raise ProviderError('Invalid provider JSON')
                return result
        except HTTPError as error:
            # Do not leak URL credentials, bodies or tokens; mutation outcomes remain uncertain.
            error.close();raise ProviderError('Provider HTTP '+str(error.code)) from None
        except (URLError,TimeoutError,OSError,ValueError):raise ProviderError('Provider connection or JSON error') from None

    def pages(self,path,key=None):
        result=[]
        for page in range(1,11):
            payload=self.request('GET',path+('&' if '?' in path else '?')+urlencode({'per_page':100,'page':page}))
            items=payload.get(key) if key and isinstance(payload,dict) else payload
            if not isinstance(items,list):raise ProviderError('Invalid provider page')
            result.extend(items)
            if len(items)<100:
                if key and (type(payload.get('total_count')) is not int or payload['total_count']!=len(result)):raise ProviderError('Provider returned incomplete check pages')
                return result
        raise ProviderError('Provider pagination exceeds 1000 records')

    def pull(self,number):
        if type(number) is not int or number<1:raise ProviderError('Invalid pull request number')
        value=self.request('GET',self.prefix+('/pulls/' if self.kind=='github' else '/merge_requests/')+str(number))
        if not isinstance(value,dict):raise ProviderError('Invalid pull request')
        if self.kind=='github':
            if (value['head'].get('repo') or {}).get('full_name')!=self.config['repository'] or (value['base'].get('repo') or {}).get('full_name')!=self.config['repository']:raise ProviderError('Fork/cross-repository pulls are unsupported')
            return {'number':number,'sha':value['head']['sha'],'branch':value['head']['ref'],'base':value['base']['ref'],'base_sha':value['base']['sha'],
                'url':value['html_url'],'open':value['state']=='open','draft':value.get('draft') is True,'merged':value.get('merged') is True}
        if value.get('source_project_id')!=value.get('target_project_id') or type(value.get('source_project_id')) is not int:raise ProviderError('Fork/cross-project merge requests are unsupported')
        return {'number':number,'sha':value['sha'],'branch':value['source_branch'],'base':value['target_branch'],
            'base_sha':value.get('diff_refs',{}).get('base_sha'),'url':value['web_url'],'open':value['state']=='opened',
            'draft':value.get('draft',value.get('work_in_progress')) is True,'merged':value['state']=='merged',
            'approvals_stable':value.get('detailed_merge_status','unknown') not in {'checking','approvals_syncing','unknown'}}

    def find(self,branch,base):
        query={'head':self.config['repository'].split('/')[0]+':'+branch,'base':base,'state':'open'} if self.kind=='github' else {'source_branch':branch,'target_branch':base,'state':'opened'}
        records=self.pages(self.prefix+('/pulls?' if self.kind=='github' else '/merge_requests?')+urlencode(query))
        if len(records)>1:raise ProviderError('Multiple pull requests require human reconciliation')
        return self.pull(records[0]['number' if self.kind=='github' else 'iid']) if records else None

    def publish(self,plan):
        if plan.get('pull'):
            path=self.prefix+('/pulls/' if self.kind=='github' else '/merge_requests/')+str(plan['pull'])
            fields={'title':plan['title'],'body':plan['body']} if self.kind=='github' else {'title':plan['title'],'description':plan['body']}
            self.request('PATCH' if self.kind=='github' else 'PUT',path,fields)
            return self.pull(plan['pull'])
        fields={'title':plan['title'],'body':plan['body'],'head':plan['branch'],'base':plan['base']} if self.kind=='github' else {'title':plan['title'],'description':plan['body'],'source_branch':plan['branch'],'target_branch':plan['base']}
        value=self.request('POST',self.prefix+('/pulls' if self.kind=='github' else '/merge_requests'),fields)
        return self.pull(value['number' if self.kind=='github' else 'iid'])

    def inspect(self,plan):
        pull=self.pull(plan['pull']);sha=plan['head'];ci={};findings=[];reviewers=set();blocked=False
        if self.kind=='github':
            runs=self.pages(self.prefix+'/commits/'+sha+'/check-runs?filter=latest',key='check_runs')
            for run in runs:
                name='check:'+run['name']
                if name in ci:raise ProviderError('Ambiguous duplicate CI check name')
                ci[name]=run.get('head_sha')==sha and run.get('status')=='completed' and run.get('conclusion')=='success'
            statuses=self.pages(self.prefix+'/commits/'+sha+'/statuses');latest={}
            for status in statuses:
                name=status['context']
                if name not in latest or status['id']>latest[name]['id']:latest[name]=status
            ci.update({'status:'+name:status['state']=='success' for name,status in latest.items()})
            reviews=self.pages(self.prefix+'/pulls/'+str(plan['pull'])+'/reviews');decisions={}
            for review in reviews:
                if review['state'] in {'APPROVED','CHANGES_REQUESTED','DISMISSED'}:
                    login=review['user']['login']
                    if login not in decisions or review['id']>decisions[login]['id']:decisions[login]=review
            reviewers={login for login,review in decisions.items() if review['state']=='APPROVED' and review.get('commit_id')==sha}
            blocked=any(review['state']=='CHANGES_REQUESTED' for review in decisions.values())
            comments=self.pages(self.prefix+'/pulls/'+str(plan['pull'])+'/comments')
            for comment in comments:
                review=decisions.get(comment['user']['login'],{})
                submitted=timestamp(review.get('submitted_at'));updated=timestamp(comment.get('updated_at'))
                resolved=review.get('state')=='APPROVED' and review.get('commit_id')==sha and submitted is not None and updated is not None and submitted>=updated
                findings.append(finding(comment['id'],comment['path'],comment.get('line') or comment.get('original_line'),comment['body'],comment.get('commit_id'),resolved))
        else:
            pipelines=self.pages(self.prefix+'/merge_requests/'+str(plan['pull'])+'/pipelines')
            candidates=[p for p in pipelines if p.get('sha')==sha]
            if candidates:
                pipeline=max(candidates,key=lambda p:p['id'])
                jobs=self.pages(self.prefix+'/pipelines/'+str(pipeline['id'])+'/jobs?include_retried=false')
                for job in jobs:
                    name='job:'+job['name']
                    if name in ci:raise ProviderError('Ambiguous duplicate CI job name')
                    ci[name]=job.get('status')=='success' and pipeline.get('status')=='success'
            approvals=self.request('GET',self.prefix+'/merge_requests/'+str(plan['pull'])+'/approvals')
            settings=self.request('GET',self.prefix+'/approvals')
            reviewers={item['user']['username'] for item in approvals.get('approved_by',[])}
            blocked=approvals.get('approvals_left',1)!=0 or settings.get('reset_approvals_on_push') is not True or not pull['approvals_stable']
            discussions=self.pages(self.prefix+'/merge_requests/'+str(plan['pull'])+'/discussions')
            for discussion in discussions:
                for note in discussion.get('notes',[]):
                    if note.get('system') or note.get('type')!='DiffNote':continue
                    position=note.get('position') or {}
                    findings.append(finding(note['id'],position.get('new_path') or position.get('old_path'),position.get('new_line') or position.get('old_line'),note['body'],position.get('head_sha'),note.get('resolved') is True))
        after=self.pull(plan['pull'])
        stable=after['sha']==pull['sha']==sha and after['branch']==plan['branch'] and after['base']==plan['base']
        ci_ok=bool(ci) and all(ci.values()) and all(ci.get(name) is True for name in self.config['required_ci'])
        review_ok=not blocked and all(name in reviewers for name in self.config['required_reviewers']) and all(item['resolved'] for item in findings)
        ready=stable and after['open'] and not after['draft'] and ci_ok and review_ok
        return {'head':sha,'pull':after,'ci':ci,'ci_passed':ci_ok,'review_passed':review_ok,'findings':findings,'ready':ready,'verified_delivery':False,
            'state':'stale' if not stable else 'ready' if ready else 'ci_pending_or_failed' if not ci_ok else 'review_pending'}

    def merge(self,plan):
        path=self.prefix+('/pulls/' if self.kind=='github' else '/merge_requests/')+str(plan['pull'])+'/merge'
        value=self.request('PUT',path,{'sha':plan['head'],'merge_method':'squash'} if self.kind=='github' else {'sha':plan['head'],'squash':True,'should_remove_source_branch':False})
        if self.kind=='github':
            if value.get('merged') is not True or not isinstance(value.get('sha'),str) or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}',value['sha']):raise ProviderError('Provider did not confirm merge')
            return {'merged':True,'commit':value['sha']}
        commit=value.get('merge_commit_sha') or value.get('squash_commit_sha')
        if value.get('state')!='merged' or not isinstance(commit,str) or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}',commit):raise ProviderError('Provider did not confirm merge')
        return {'merged':True,'commit':commit}
