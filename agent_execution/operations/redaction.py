import os
import re

SENSITIVE=re.compile(r'(?:password|passwd|secret|credential|authorization|api[_-]?key|access[_-]?token|refresh[_-]?token)',re.I)


def redact(value,depth=0):
    if depth>20:return '[depth limited]'
    if isinstance(value,dict):return {str(key):('[redacted]' if SENSITIVE.search(str(key)) else redact(item,depth+1)) for key,item in list(value.items())[:256]}
    if isinstance(value,(list,tuple)):return [redact(item,depth+1) for item in value[:256]]
    if not isinstance(value,str):return value
    text=value[:65536]
    # Deployment secrets also occur under innocent keys or in provider exception text.
    secrets=[item for key,item in os.environ.items() if SENSITIVE.search(key) and len(item)>=8]
    for name in ('COMPANION_WS_TOKEN','COMPANION_WS_TOKENS'):
        for item in os.environ.get(name,'').split(','):
            token=item.split(':',1)[0].strip()
            if len(token)>=8:secrets.append(token)
    try:
        import json
        tokens=json.loads(os.environ.get('AGENT_API_TOKENS','{}'))
        if isinstance(tokens,dict):secrets.extend(key for key in tokens if isinstance(key,str) and len(key)>=8)
    except ValueError:pass
    for secret in sorted(secrets,key=len,reverse=True):text=text.replace(secret,'[redacted]')
    text=re.sub(r'(?i)(Bearer\s+)[A-Za-z0-9._~+/=-]+',r'\1[redacted]',text)
    text=re.sub(r'\bsk-[A-Za-z0-9_-]{8,}\b','[redacted]',text)
    text=re.sub(r'(?i)((?:password|secret|api[_-]?key|access[_-]?token|refresh[_-]?token)\s*[=:]\s*)[^\s,;}&]+',r'\1[redacted]',text)
    text=re.sub(r'(https?://)[^\s/@]+:[^\s/@]+@',r'\1[redacted]@',text)
    return text
