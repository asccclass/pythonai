"""Opt-in, bounded provider capability checks; never disclose provider credentials."""
import argparse
import json
import uuid

from .budgets import budget_scope, budget_client
from .policy import execution_scope


def probe(client, model, vision=False, embedding=False):
    from .context import _probing
    token=_probing.set(True)
    try:
        return _probe(client,model,vision,embedding)
    finally:
        _probing.reset(token)


def _probe(client, model, vision=False, embedding=False):
    """A failed probe is unknown, not evidence that a modality is unsupported."""
    client=budget_client(client)
    results={}
    def check(name,operation):
        try:
            operation()
            results[name]={'status':'supported'}
        except Exception as error:
            results[name]={'status':'unknown','error_type':type(error).__name__}
            status=getattr(error,'status_code',None)
            if isinstance(status,int):results[name]['http_status']=status
    if embedding:
        check('embedding',lambda:client.embeddings.create(model=model,input='capability probe',timeout=15))
    else:
        check('chat',lambda:client.chat.completions.create(model=model,
            messages=[{'role':'user','content':'Reply OK.'}],max_completion_tokens=16,timeout=15))
        if vision:
            # Tiny valid PNG, with no user or workspace content.
            png='iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII='
            check('vision',lambda:client.chat.completions.create(model=model,
                messages=[{'role':'user','content':[{'type':'text','text':'Describe this image briefly.'},
                    {'type':'image_url','image_url':{'url':'data:image/png;base64,'+png}}]}],
                max_completion_tokens=16,timeout=15))
    return {'model':model,'capabilities':results,'meaning':'Successful request only; semantic quality is not verified.'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model',required=True)
    parser.add_argument('--vision',action='store_true')
    parser.add_argument('--embedding',action='store_true')
    args=parser.parse_args()
    from server import get_client
    with execution_scope(source='probe',task_id='probe:'+uuid.uuid4().hex),budget_scope():
        print(json.dumps(probe(get_client(),args.model,args.vision,args.embedding),ensure_ascii=False))


if __name__=='__main__':main()
