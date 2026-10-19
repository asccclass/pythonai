"""Token-bounded messages with complete tool pairs and preserved task constraints."""
import copy
import json
import re


class ContextTooLarge(ValueError):
    pass


def count_tokens(value):
    from .budgets import token_upper_bound
    return token_upper_bound(value)


def message_tokens(messages, image_tokens=None):
    """Count text conservatively, reserving vision tokens instead of Base64 bytes.

    This is a configurable provider-independent reservation, not a tokenizer for
    a specific vision model. Never modify the images passed to the provider.
    Tool schemas, ordinary text and tool arguments still use the byte bound.
    """
    text_messages=[]
    images=0
    for message in messages:
        content=message.get('content')
        if not isinstance(content,list):
            text_messages.append(message)
            continue
        parts=[]
        for part in content:
            image=part.get('image_url') if isinstance(part,dict) and part.get('type')=='image_url' else None
            if isinstance(image,dict) and isinstance(image.get('url'),str) and image['url']:
                images+=1
                parts.append({**part,'image_url':{**image,'url':'<image>'}})
            else:
                parts.append(part)
        text_messages.append({**message,'content':parts})
    if not images:
        return count_tokens(text_messages)
    if image_tokens is None:
        import os
        image_tokens=os.environ.get('AGENT_MODEL_IMAGE_TOKENS','8192')
    if isinstance(image_tokens,bool):
        raise ValueError('Image token reservation must be a positive integer')
    try:
        reservation=int(image_tokens)
    except (ValueError,TypeError,OverflowError) as error:
        raise ValueError('Image token reservation must be a positive integer') from error
    if reservation<=0 or str(reservation)!=str(image_tokens):
        raise ValueError('Image token reservation must be a positive integer')
    return count_tokens(text_messages)+images*reservation


def normalize_blocks(messages):
    """Group calls/results; do not submit orphaned or unresolved historical calls."""
    values=copy.deepcopy(messages)
    blocks=[]
    index=0
    while index<len(values):
        message=values[index]
        index+=1
        if message.get('role')=='tool':
            # Old compaction may have left an orphan. Never invent its call.
            continue
        calls=message.get('tool_calls') or []
        if not calls:
            blocks.append([message])
            continue
        if message.get('role')!='assistant':
            raise ValueError('Only assistant messages may contain tool calls')
        if any(not isinstance(call,dict) or not call.get('id') for call in calls) or len({call['id'] for call in calls})!=len(calls):
            raise ValueError('Tool calls require unique IDs')
        following=[]
        while index<len(values) and values[index].get('role')!='assistant':
            following.append(values[index]);index+=1
        results={m.get('tool_call_id'):m for m in following if m.get('role')=='tool'}
        paired=[call for call in calls if call['id'] in results]
        if paired:
            message['tool_calls']=paired
            blocks.append([message,*[results[call['id']] for call in paired],
                *[m for m in following if m.get('role')!='tool']])
        elif message.get('content'):
            message.pop('tool_calls',None)
            blocks.append([message])
        # Preserve the user's next request or hook feedback after the complete pair.
        if not paired:
            blocks.extend([[m] for m in following if m.get('role')!='tool'])
    return blocks


def has_approval(block):
    text=json.dumps(block,ensure_ascii=False)
    return 'approval_required:' in text or 'approval_id' in text


def govern_messages(messages, max_tokens, tools=None, goal='', criteria=None, image_tokens=None):
    blocks=normalize_blocks(messages)
    schema_tokens=count_tokens(tools or [])
    protected={index for index,block in enumerate(blocks) if any(m.get('role') in {'system','developer'} for m in block) or has_approval(block)}
    latest_tools = next((index for index in range(len(blocks)-1, -1, -1) if any(m.get('role') == 'tool' for m in blocks[index])), None)
    if latest_tools is not None:
        protected.add(latest_tools)
    last_user=next((index for index in range(len(blocks)-1,-1,-1) if any(m.get('role')=='user' for m in blocks[index])),None)
    if last_user is not None:
        protected.add(last_user)
    constraints=[]
    if goal or criteria:
        # These are task data in a user message; a summary never gains system authority.
        constraints=[{'role':'user','content':'Retained task goal and acceptance data:\n'+json.dumps({'goal':goal,'criteria':criteria or {}},ensure_ascii=False)}]
    def flatten(chosen):
        return [m for index,block in enumerate(blocks) if index in chosen for m in block]+constraints
    chosen=set(range(len(blocks)))
    if message_tokens(flatten(chosen),image_tokens)+schema_tokens<=max_tokens:
        return flatten(chosen)
    # Drop entire old call/result groups, never half of a pair or the active goal.
    for index in range(len(blocks)):
        if index in protected:
            continue
        chosen.discard(index)
        if message_tokens(flatten(chosen),image_tokens)+schema_tokens<=max_tokens:
            return flatten(chosen)
    from .turn_control.context import fit_latest_evidence
    fitted = fit_latest_evidence(flatten(chosen), max_tokens-schema_tokens,
                                 lambda values: message_tokens(values, image_tokens))
    if fitted is not None:
        return fitted
    raise ContextTooLarge('Protected instructions, goal, approvals or latest input exceed model context budget')


def fit_tool_schemas(messages, max_tokens, tools, goal='', criteria=None, image_tokens=None):
    """Return a useful, deterministic tool subset that leaves room for protected context.

    This is an admission control fallback.  It never changes messages or bypasses
    permissions: omitted tools simply cannot be called during this model round.
    """
    if not tools:
        return []
    # Confirm that the protected prompt itself fits before considering schemas.
    govern_messages(messages, max_tokens, [], goal, criteria, image_tokens)
    text = ' '.join(str(message.get('content') or '') for message in messages
                    if message.get('role') == 'user') + ' ' + str(goal or '')
    words = set(re.findall(r'[a-z0-9_]+', text.lower()))
    recent_calls = {call.get('function', {}).get('name') for message in messages
                    for call in (message.get('tool_calls') or []) if isinstance(call, dict)}
    def name(schema):
        return str(schema.get('function', {}).get('name') or '')
    def priority(schema):
        tool = name(schema)
        aliases = set(tool.lower().split('_')) | {tool.lower()}
        mentioned = bool(aliases & words) or (tool == 'fetch_url' and ('http' in text.lower() or 'url' in words))
        return (0 if tool in recent_calls else 1 if mentioned else 2 if tool == 'ask_user' else 3,
                count_tokens(schema), tool)
    selected = []
    for schema in sorted(tools, key=priority):
        candidate = [*selected, schema]
        try:
            govern_messages(messages, max_tokens, candidate, goal, criteria, image_tokens)
        except ContextTooLarge:
            continue
        selected = candidate
    return selected


def model_capabilities(model):
    import os
    configured=json.loads(os.environ.get('AGENT_MODEL_CAPABILITIES','{}'))
    if not isinstance(configured,dict) or not isinstance(configured.get(model,{}),dict):
        raise ValueError('AGENT_MODEL_CAPABILITIES must map model IDs to objects')
    return configured.get(model,{})


def route_model(model,messages):
    import os
    images=any(isinstance(m.get('content'),list) and any(p.get('type')=='image_url' for p in m['content'] if isinstance(p,dict)) for m in messages)
    if images:
        model=(os.environ.get('OLLAMA_VISION_MODEL') or model) if not _probing.get() else model
        capabilities=model_capabilities(model)
        if capabilities.get('vision') is False:
            raise ValueError('Configured model does not support vision; image analysis was not performed')
    else:
        capabilities=model_capabilities(model)
    if capabilities.get('available') is False or capabilities.get('chat') is False:
        raise ValueError('Configured model is unavailable or does not support chat')
    return model,capabilities


from contextvars import ContextVar
_probing = ContextVar('probing_explicit_model',default=False)
