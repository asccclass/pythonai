import json
from agent_execution import policy_tool
from agent_execution.media.browser import browse

@policy_tool('browser_session')
def browser_session(url,actions=None,session_id='default',screenshot=False):
    return json.dumps(browse(url,actions or [],session_id,screenshot),ensure_ascii=False)
