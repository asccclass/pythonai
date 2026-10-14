"""Persistent root task budgets shared by copied contexts and child agents."""
from contextlib import closing, contextmanager
from contextvars import ContextVar
from functools import wraps
import json
import math
import os
import sqlite3
import threading
import time
from types import SimpleNamespace
import uuid


class BudgetExhausted(RuntimeError):
    pass


def numeric(name, default, allow_zero=False):
    value = float(os.environ.get(name,default))
    if not math.isfinite(value) or value < 0 or (value==0 and not allow_zero):
        raise ValueError(f'{name} must be finite and positive')
    return value


def token_upper_bound(value):
    # UTF-8 bytes are deliberately conservative for ordinary byte-tokenized text.
    return len(json.dumps(value,ensure_ascii=False,separators=(',', ':')).encode())+128


_budget = ContextVar('task_budget',default=None)


class TaskBudget:
    def __init__(self, owner, key, path=None):
        from .tasks import TaskStore
        self.path = TaskStore(path).path
        self.owner,self.key = owner,key
        self.max_tokens=int(numeric('AGENT_TASK_MAX_TOKENS',256000))
        self.max_requests=int(numeric('AGENT_TASK_MAX_REQUESTS',24))
        self.max_children=int(numeric('AGENT_TASK_MAX_CHILDREN',4,allow_zero=True))
        self.max_depth=int(numeric('AGENT_TASK_MAX_DEPTH',2,allow_zero=True))
        self.max_cost=numeric('AGENT_TASK_MAX_COST_USD',0,allow_zero=True)
        self.max_output=int(numeric('AGENT_MODEL_MAX_OUTPUT_TOKENS',2048))
        self.concurrency=int(numeric('AGENT_MODEL_CONCURRENCY',4))
        with closing(self.connect()) as conn, conn:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS task_budgets (
                    owner TEXT NOT NULL, id TEXT NOT NULL, deadline REAL NOT NULL,
                    limits TEXT NOT NULL, requests INTEGER NOT NULL DEFAULT 0,
                    tokens INTEGER NOT NULL DEFAULT 0, cost REAL NOT NULL DEFAULT 0,
                    children INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(owner,id));
                CREATE TABLE IF NOT EXISTS budget_calls (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, task_id TEXT NOT NULL,
                    tokens INTEGER NOT NULL, cost REAL NOT NULL, status TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS model_slots (
                    id TEXT PRIMARY KEY, expires REAL NOT NULL);
            ''')
            limits={'tokens':self.max_tokens,'requests':self.max_requests,'children':self.max_children,
                    'depth':self.max_depth,'cost':self.max_cost,'output':self.max_output,
                    'rates':json.loads(os.environ.get('AGENT_MODEL_RATES','{}'))}
            conn.execute('BEGIN IMMEDIATE')
            exists=conn.execute('SELECT 1 FROM task_budgets WHERE owner=? AND id=?',(owner,key)).fetchone()
            if not exists and conn.execute('SELECT COUNT(*) FROM task_budgets').fetchone()[0]>=int(numeric('AGENT_TASK_LIMIT',10000)):
                raise BudgetExhausted('Task budget storage limit reached')
            conn.execute('INSERT OR IGNORE INTO task_budgets(owner,id,deadline,limits) VALUES(?,?,?,?)',
                (owner,key,time.time()+numeric('AGENT_TASK_DEADLINE_SECONDS',600),json.dumps(limits)))
        # Restart cannot increase an existing budget through changed environment settings.
        row=self.snapshot()
        limits=json.loads(row['limits'])
        self.max_tokens,self.max_requests,self.max_children,self.max_depth,self.max_cost,self.max_output=[limits[k] for k in ('tokens','requests','children','depth','cost','output')]
        self.rates=limits.get('rates',{})
        if not isinstance(self.rates,dict):
            raise ValueError('AGENT_MODEL_RATES must be an object')

    def connect(self):
        conn=sqlite3.connect(self.path,timeout=10)
        conn.row_factory=sqlite3.Row
        return conn

    def snapshot(self):
        with closing(self.connect()) as conn:
            return dict(conn.execute('SELECT * FROM task_budgets WHERE owner=? AND id=?',(self.owner,self.key)).fetchone())

    def remaining_seconds(self):
        return max(0,self.snapshot()['deadline']-time.time())

    def check(self):
        row=self.snapshot()
        if row['deadline']<=time.time():
            raise BudgetExhausted('Task deadline exhausted')
        if row['tokens']>self.max_tokens or (self.max_cost and row['cost']>self.max_cost):
            raise BudgetExhausted('Task token/cost budget exceeded')

    def reserve_child(self, depth):
        with closing(self.connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            row=conn.execute('SELECT children,deadline FROM task_budgets WHERE owner=? AND id=?',(self.owner,self.key)).fetchone()
            if row['deadline']<=time.time() or row['children']>=self.max_children or depth>=self.max_depth:
                raise BudgetExhausted('Task child count/depth/deadline budget exhausted')
            conn.execute('UPDATE task_budgets SET children=children+1 WHERE owner=? AND id=?',(self.owner,self.key))

    def reserve(self, model, prompt_tokens, output_tokens):
        rate=self.rates.get(model)
        cost=0.0
        if self.max_cost:
            if not isinstance(rate,dict):
                raise BudgetExhausted('Cost cap requires configured rates for this model')
            input_rate,output_rate=float(rate.get('input',-1)),float(rate.get('output',-1))
            if not all(math.isfinite(v) and v>=0 for v in (input_rate,output_rate)):
                raise ValueError('Model rates must be finite nonnegative USD per million tokens')
            cost=(prompt_tokens*input_rate+output_tokens*output_rate)/1_000_000
        key=uuid.uuid4().hex
        tokens=prompt_tokens+output_tokens
        with closing(self.connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            row=conn.execute('SELECT * FROM task_budgets WHERE owner=? AND id=?',(self.owner,self.key)).fetchone()
            reasons=[]
            if row['deadline']<=time.time():reasons.append('deadline')
            if row['requests']>=self.max_requests:reasons.append(f"requests ({row['requests']}/{self.max_requests})")
            if row['tokens']+tokens>self.max_tokens:reasons.append(f"tokens ({row['tokens']} + {tokens} > {self.max_tokens})")
            if self.max_cost and row['cost']+cost>self.max_cost:reasons.append('cost')
            if reasons:
                raise BudgetExhausted('Task budget exhausted: '+', '.join(reasons))
            conn.execute('UPDATE task_budgets SET requests=requests+1,tokens=tokens+?,cost=cost+? WHERE owner=? AND id=?',(tokens,cost,self.owner,self.key))
            conn.execute('INSERT INTO budget_calls VALUES(?,?,?,?,?,?)',(key,self.owner,self.key,tokens,cost,'reserved'))
            conn.execute('CREATE TABLE IF NOT EXISTS budget_rates (id TEXT PRIMARY KEY, input REAL, output REAL)')
            if self.max_cost:
                conn.execute('INSERT INTO budget_rates VALUES(?,?,?)',(key,input_rate,output_rate))
        # Failed/unknown calls stay charged. An SDK retry cannot silently reset the budget.
        return key

    def reconcile(self, key, usage, settle=True):
        if usage is None:
            return
        total=getattr(usage,'total_tokens',None)
        if type(total) is not int or total<0:
            return
        with closing(self.connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            call=conn.execute("SELECT tokens,cost FROM budget_calls WHERE id=? AND owner=? AND task_id=? AND status='reserved'",(key,self.owner,self.key)).fetchone()
            if call is None:
                return
            # Partial streams may increase charges; only successful completion can refund.
            charged=total if settle else max(total,call['tokens'])
            delta=charged-call['tokens']
            actual_cost=call['cost']
            if self.max_cost:
                rates=conn.execute('SELECT input,output FROM budget_rates WHERE id=?',(key,)).fetchone()
                prompt=getattr(usage,'prompt_tokens',getattr(usage,'input_tokens',None))
                output=getattr(usage,'completion_tokens',None)
                if rates and isinstance(prompt,int) and prompt>=0:
                    output=output if isinstance(output,int) and output>=0 else max(0,total-prompt)
                    actual_cost=max(actual_cost,(prompt*rates['input']+output*rates['output'])/1_000_000)
            conn.execute('UPDATE task_budgets SET tokens=tokens+?,cost=cost+? WHERE owner=? AND id=?',(delta,actual_cost-call['cost'],self.owner,self.key))
            conn.execute("UPDATE budget_calls SET status=?,tokens=?,cost=? WHERE id=?",('reported' if settle else 'reserved',charged,actual_cost,key))
            row=conn.execute('SELECT tokens,cost FROM task_budgets WHERE owner=? AND id=?',(self.owner,self.key)).fetchone()
            over=row['tokens']>self.max_tokens or (self.max_cost and row['cost']>self.max_cost)
        if over:
            raise BudgetExhausted('Provider usage exceeded reserved token budget; refusing further tools')

    @contextmanager
    def slot(self, cancelled=None):
        key=uuid.uuid4().hex
        acquired=False
        stop=threading.Event()
        thread=None
        try:
            while not acquired:
                self.check()
                if cancelled and cancelled():
                    raise BudgetExhausted('Task cancelled while waiting for model capacity')
                with closing(self.connect()) as conn, conn:
                    conn.execute('BEGIN IMMEDIATE')
                    conn.execute('DELETE FROM model_slots WHERE expires<=?',(time.time(),))
                    if conn.execute('SELECT COUNT(*) FROM model_slots').fetchone()[0]<self.concurrency:
                        conn.execute('INSERT INTO model_slots VALUES(?,?)',(key,time.time()+65))
                        acquired=True
                if not acquired:
                    time.sleep(.05)
            def renew():
                while not stop.wait(20):
                    try:
                        with closing(self.connect()) as conn, conn:
                            conn.execute('UPDATE model_slots SET expires=? WHERE id=?',(time.time()+65,key))
                    except sqlite3.Error:
                        return
            thread=threading.Thread(target=renew,daemon=True)
            thread.start()
            yield
        finally:
            stop.set()
            if thread:thread.join(1)
            if acquired:
                with closing(self.connect()) as conn, conn:
                    conn.execute('DELETE FROM model_slots WHERE id=?',(key,))


@contextmanager
def budget_scope():
    from .policy import current_context, execution_scope
    from .tasks import task_owner
    context=current_context()
    inherited=_budget.get()
    if inherited is not None or context.task_id is None:
        yield inherited
        return
    budget=TaskBudget(task_owner(context),context.task_id)
    token=_budget.set(budget)
    original=context.check_cancelled
    try:
        with execution_scope(check_cancelled=lambda:bool(original and original()) or budget.remaining_seconds()<=0):
            yield budget
    finally:
        _budget.reset(token)


def budgeted_turn(function):
    @wraps(function)
    def call(*args,**kwargs):
        with budget_scope():
            return function(*args,**kwargs)
    return call


def active_budget():
    return _budget.get()


_depth = ContextVar('budget_child_depth',default=0)


@contextmanager
def child_scope():
    """Prevent a copied child context from overwriting its parent's checkpoint."""
    from .tasks import _active
    task_token=_active.set(None)
    depth_token=_depth.set(_depth.get()+1)
    try:
        yield
    finally:
        _depth.reset(depth_token)
        _active.reset(task_token)


class BudgetStream:
    def __init__(self, stream, budget, key, slot, cancelled):
        self.stream,self.budget,self.key,self.slot=stream,budget,key,slot
        self.stopped=threading.Event()
        self.lock=threading.Lock()
        self.closed=False
        self.interrupted=threading.Event()
        self.deadline=time.monotonic()+min(60,budget.remaining_seconds())
        def monitor():
            while not self.stopped.wait(.05):
                if time.monotonic()>=self.deadline or (cancelled and cancelled()):
                    self.interrupted.set()
                    self.close()
                    return
        self.thread=threading.Thread(target=monitor,daemon=True)
        self.thread.start()

    def __iter__(self):
        usage=None
        try:
            for chunk in self.stream:
                self.budget.check()
                if self.closed:
                    raise BudgetExhausted('Model stream deadline/cancellation reached')
                if getattr(chunk,'usage',None) is not None:
                    usage=chunk.usage
                    self.budget.reconcile(self.key,usage,settle=False)
                yield chunk
            if self.closed or self.interrupted.is_set() or time.monotonic()>=self.deadline:
                raise BudgetExhausted('Model stream wall deadline exhausted')
            self.budget.reconcile(self.key,usage)
            self.budget.check()
        finally:
            self.close()

    def close(self):
        with self.lock:
            if self.closed:return
            self.closed=True
            self.stopped.set()
            try:
                self.stream.close()
            finally:
                self.slot.__exit__(None,None,None)


class BudgetEndpoint:
    def __init__(self,client,kind):
        self.client,self.kind=client,kind

    from .operations.tracing import traced
    @traced('model','model.request')
    def create(self,**kwargs):
        from .policy import current_context
        from .context import govern_messages, route_model, model_capabilities, message_tokens
        budget=active_budget()
        if budget is None:
            endpoint=self.client.chat.completions if self.kind=='chat' else self.client.embeddings
            return endpoint.create(**kwargs)
        budget.check()
        cancelled=current_context().check_cancelled
        if cancelled and cancelled():
            raise BudgetExhausted('Task cancelled before model request')
        kwargs=dict(kwargs)
        if self.kind=='chat':
            model,capabilities=route_model(kwargs['model'],kwargs['messages'])
            kwargs['model']=model
            output=min(budget.max_output,int(kwargs.pop('max_tokens',kwargs.pop('max_completion_tokens',budget.max_output))))
            if output<=0:raise ValueError('Model output token cap must be positive')
            parameter=capabilities.get('output_parameter',os.environ.get('AGENT_OUTPUT_PARAMETER','max_completion_tokens'))
            if parameter not in {'max_tokens','max_completion_tokens'}:raise ValueError('Invalid output parameter')
            kwargs[parameter]=output
            window=int(capabilities.get('context_tokens',numeric('AGENT_MODEL_CONTEXT_TOKENS',32768)))
            from .tasks import _active
            task=_active.get()
            row=task[0].get(task[1],task[2]) if task else None
            kwargs['messages']=govern_messages(kwargs['messages'],window-output,kwargs.get('tools'),
                goal=row['goal'] if row else '',criteria=row['criteria'] if row else None,
                image_tokens=capabilities.get('image_input_tokens'))
            prompt=message_tokens(kwargs['messages'],capabilities.get('image_input_tokens'))+token_upper_bound(kwargs.get('tools',[]))
            if kwargs.get('stream'):
                kwargs['stream_options']={**kwargs.get('stream_options',{}),'include_usage':True}
        else:
            if model_capabilities(kwargs['model']).get('embedding') is False:
                raise ValueError('Configured model does not support embeddings')
            prompt,output=token_upper_bound(kwargs['input']),0
        kwargs['timeout']=min(float(kwargs.get('timeout',60)),60,budget.remaining_seconds())
        client=self.client
        from openai import OpenAI
        if isinstance(client,OpenAI):client=client.with_options(max_retries=0,timeout=kwargs['timeout'])
        slot=budget.slot(cancelled)
        slot.__enter__()
        handed_off=False
        try:
            key=budget.reserve(kwargs['model'],prompt,output)
            endpoint=client.chat.completions if self.kind=='chat' else client.embeddings
            result=endpoint.create(**kwargs)
            if kwargs.get('stream'):
                stream=BudgetStream(result,budget,key,slot,cancelled)
                handed_off=True
                return stream
            budget.reconcile(key,getattr(result,'usage',None))
            budget.check()
            return result
        finally:
            if not handed_off:slot.__exit__(None,None,None)


class BudgetClient:
    def __init__(self,client):
        self.raw=client
        self.chat=SimpleNamespace(completions=BudgetEndpoint(client,'chat'))
        self.embeddings=BudgetEndpoint(client,'embedding')

    def __getattr__(self,name):
        return getattr(self.raw,name)


def budget_client(client):
    if isinstance(client,BudgetClient) or active_budget() is None:return client
    return BudgetClient(client)
