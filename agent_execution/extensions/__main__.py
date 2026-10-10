"""Human CLI for reviewing, testing, enabling and reverting immutable versions."""
import argparse
import json
from pathlib import Path
from .store import ExtensionStore


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['stage','show','test','activate','disable','list'])
    parser.add_argument('value',nargs='?');parser.add_argument('--approval-id')
    args=parser.parse_args();store=ExtensionStore()
    if args.action=='stage':
        package=json.loads(Path(args.value).read_text(encoding='utf-8'))
        result={'version':store.stage(**package)}
    elif args.action=='show':result=store.get(args.value)
    elif args.action=='test':result=store.test(args.value)
    elif args.action=='activate':result={'version':store.activate(args.value,args.approval_id)}
    elif args.action=='disable':store.disable(args.value);result={'disabled':args.value}
    else:result=store.active()
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
