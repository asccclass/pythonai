import argparse
import json
from .store import ImprovementStore
from .evaluation import configured_evaluate

def main():
    parser=argparse.ArgumentParser(description='Review and measure Doctor proposals before activation')
    parser.add_argument('action',choices=['show','evaluate','activate','rollback','revoke','list'])
    parser.add_argument('value',nargs='?');parser.add_argument('--approval-id')
    args=parser.parse_args();store=ImprovementStore()
    if args.action=='show':result=store.get(args.value)
    elif args.action=='evaluate':result=configured_evaluate(store,args.value)
    elif args.action=='activate':store.activate(args.value,args.approval_id);result={'active':args.value}
    elif args.action=='rollback':store.activate(args.value,args.approval_id,rollback=True);result={'restored':args.value}
    elif args.action=='revoke':store.revoke(args.value);result={'revoked':args.value}
    else:result=store.active()
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
