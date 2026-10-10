"""Human OAuth CLI: python -m agent_execution.extensions.oauth_cli begin|finish|revoke CONFIG."""
import argparse
import json
from pathlib import Path
from .oauth import OAuthClient


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['begin','finish','revoke']);parser.add_argument('config')
    parser.add_argument('--callback-url');args=parser.parse_args()
    config=json.loads(Path(args.config).read_text(encoding='utf-8'));client=OAuthClient(config['url'],config['oauth'])
    try:
        result=client.begin() if args.action=='begin' else client.finish(args.callback_url) if args.action=='finish' else client.revoke()
        print(json.dumps(result,ensure_ascii=False,indent=2))
    except Exception as error:
        print(json.dumps({'success':False,'error_type':type(error).__name__}));raise SystemExit(1)
    finally:client.close()


if __name__=='__main__':main()
