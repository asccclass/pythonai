"""Offline protocol fixture for real pipe transport tests."""
import json
import sys

for line in sys.stdin:
    message=json.loads(line)
    if 'id' not in message:continue
    method=message['method']
    if method=='initialize':result={'protocolVersion':'2024-11-05','capabilities':{}}
    elif method=='tools/list':result={'tools':[{'name':'read_note','inputSchema':{'type':'object','properties':{},'additionalProperties':False}}]}
    elif method=='tools/call':result={'content':[{'type':'text','text':'verified'}]}
    else:result={}
    print(json.dumps({'jsonrpc':'2.0','id':message['id'],'result':result}),flush=True)
