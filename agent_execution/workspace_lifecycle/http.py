"""Authenticated human management boundary; no owner/path supplied by request JSON."""
import json
import re
from contextlib import closing
from .store import LifecycleStore


def handle(route,method,body):
    store=LifecycleStore()
    if route=='/api/workspace' and method=='GET':return 200,store.inventory()
    if route=='/api/session-delete/preview' and method=='POST':return 200,store.preview('delete_session')
    prefix='/api/session-delete/'
    if not route.startswith(prefix) or method!='POST':return 404,{'error':'not_found'}
    action=route[len(prefix):]
    if action not in {'confirm','status','resume'}:return 404,{'error':'not_found'}
    data=json.loads(body or '{}')
    if not isinstance(data,dict) or set(data)!={'plan_id'} or not isinstance(data['plan_id'],str) or not re.fullmatch('[0-9a-f]{32}',data['plan_id']):
        raise ValueError('Expected only plan_id from the current preview')
    key=data['plan_id'];plan=store.plan(key)
    if plan['action']!='delete_session':raise ValueError('Not a session deletion')
    if action=='confirm':result=store.confirm(key)
    elif action=='resume':
        from .operations import resume
        result=resume(store,key)
    else:
        with closing(store.connect()) as conn:row=conn.execute('SELECT state,error FROM wl_operations WHERE id=?',(key,)).fetchone()
        result={'operation_id':key,**(dict(row) if row else {'state':'preview'})}
    if result['state']=='completed':
        from .erasure import finalize
        result=finalize(store,key)
    return 200,result
