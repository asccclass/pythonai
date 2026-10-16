"""Fit recent tool evidence without silently deleting the entire latest observation."""


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
