"""Human OAuth CLI: python -m agent_execution.extensions.oauth_cli begin|finish|revoke CONFIG."""
import argparse
import json
from pathlib import Path
from urllib.parse import parse_qs,urlsplit
from .oauth import OAuthClient


def login(client,timeout=120,emit=print,check_cancelled=None):
    from .oauth_callback import LoopbackReceiver
    state=None
    # Bind the exact registered URI before creating or advertising an authorization.
    with LoopbackReceiver(client.redirect,timeout) as receiver:
        try:
            begin=client.begin();receiver.expect(begin['authorization_url'])
            state=parse_qs(urlsplit(begin['authorization_url']).query)['state'][0]
            emit(json.dumps(begin,ensure_ascii=False))
            callback=receiver.wait(check_cancelled)
            return client.finish(callback)
        finally:
            if state:client.cancel_state(state)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['begin','finish','revoke','login']);parser.add_argument('config')
    parser.add_argument('--callback-url');parser.add_argument('--timeout',type=float,default=120);args=parser.parse_args()
    source=Path(args.config)
    if source.stat().st_size>65536:parser.error('OAuth configuration exceeds 64 KiB')
    config=json.loads(source.read_text(encoding='utf-8'));client=OAuthClient(config['url'],config['oauth'])
    try:
        result=login(client,args.timeout,emit=lambda value:print(value,flush=True)) if args.action=='login' else client.begin() if args.action=='begin' else client.finish(args.callback_url) if args.action=='finish' else client.revoke()
        print(json.dumps(result,ensure_ascii=False,indent=2))
    except Exception as error:
        print(json.dumps({'success':False,'error_type':type(error).__name__}));raise SystemExit(1)
    finally:client.close()


if __name__=='__main__':main()
