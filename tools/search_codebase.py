import json
import math
from agent_execution import policy_tool
from agent_execution.code_index import CodeIndex

def cosine_similarity(first,second):
    if not first or not second or len(first)!=len(second):return 0.0
    norm=math.sqrt(sum(x*x for x in first)*sum(x*x for x in second))
    return sum(a*b for a,b in zip(first,second))/norm if norm else 0.0

@policy_tool('search_codebase')
def search_codebase(query: str,limit: int=5,mode: str='semantic') -> str:
    return json.dumps(CodeIndex().search(query,limit,mode),ensure_ascii=False)
