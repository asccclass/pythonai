"""Fit recent tool evidence without silently deleting the entire latest observation."""


def conversation_anchors(blocks, goal=''):
    """Keep the preceding exchange available throughout the current task.

    The active goal, rather than the latest synthetic user feedback, locates
    the task boundary. Preserve original roles; prior assistant prose is
    reference material and never becomes a new instruction.
    """
    def text(message):
        content = message.get('content')
        if isinstance(content,list):
            return '\n'.join(part.get('text','') for part in content if isinstance(part,dict))
        return content if isinstance(content,str) else ''
    users = [index for index,block in enumerate(blocks)
             if any(message.get('role')=='user' for message in block)]
    if not users:
        return set()
    matches = [index for index in users if goal and any(
        message.get('role')=='user' and (text(message)==goal or
            (len(goal)==16000 and text(message).startswith(goal))) for message in blocks[index])]
    current = matches[-1] if matches else users[-1]
    protected = {current}
    previous_reply = next((index for index in range(current-1,-1,-1) if any(
        message.get('role')=='assistant' and text(message).strip() and not message.get('tool_calls')
        for message in blocks[index])),None)
    if previous_reply is None:
        return protected
    protected.add(previous_reply)
    previous_user = next((index for index in reversed(users) if index<previous_reply),None)
    if previous_user is not None:
        protected.add(previous_user)
    return protected


def fit_latest_evidence(messages, limit, count):
    start = next((i for i in range(len(messages)-1, -1, -1) if messages[i].get('role') == 'assistant' and messages[i].get('tool_calls')), None)
    if start is None:
        return None
    eligible = [(message, message['content']) for message in messages[start+1:]
                if message.get('role') == 'tool' and isinstance(message.get('content'), str)
                and not any(marker in message['content'] for marker in ('approval_id', 'approval_required:'))]
    if not eligible:
        return None
    marker = '\n[Tool output truncated for model context; complete evidence remains in task history.]\n'
    def resize(fraction):
        for message, original in eligible:
            keep = int(len(original) * fraction)
            if keep >= len(original):
                message['content'] = original
            else:
                head = int(keep * .75)
                tail = keep - head
                message['content'] = original[:head] + marker + (original[-tail:] if tail else '')
    resize(0)
    if count(messages) > limit:
        return None
    low, high = 0.0, 1.0
    for _ in range(18):
        middle = (low + high) / 2
        resize(middle)
        if count(messages) <= limit:
            low = middle
        else:
            high = middle
    resize(low)
    return messages
