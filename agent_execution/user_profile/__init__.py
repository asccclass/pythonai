"""Automatic, identity-scoped user facts and behavioral principles."""
from agent_configuration import load_text
import json

from agent_execution.policy import current_context
from .extractor import SECRET, direct_text, validate_changes
from .store import ProfileStore

MARKER = '[PythonAI current user profile]\n'


def prepare_turn(text, extractor):
    """Return (command reply, ephemeral extraction status). No assistant/document input."""
    try:
        store = ProfileStore()
        reply = store.command(text)
        if reply is not None:
            return reply, ''
        text = direct_text(text)
        if extractor is None or not text or len(text) > 12000 or SECRET.search(text):
            return None, '未進行 profile 萃取；不可宣稱已記住本輪資訊。'
        version, entries = store.snapshot()
        changes = validate_changes(extractor.extract(text, entries), text, entries)
        if changes:
            store.apply(version, changes)
        return None, f'本輪 profile 已套用 {len(changes)} 筆變更；僅此結果可作為已儲存的依據。'
    except Exception:
        # Do not log provider errors, which can contain original private content.
        if text.strip().split()[:1] == ['/profile']:
            return 'Profile 操作失敗；尚未確認儲存結果，請重試。', ''
        return None, '本輪 profile 未能更新；請明確告知使用者未儲存，不可宣稱已記住。可用 /profile 核對或重試。'


def refresh_messages(messages, status=''):
    """Replace stale managed context, including context recovered from checkpoints."""
    cleaned = [m for m in messages if not (m.get('role') == 'system' and isinstance(m.get('content'), str) and m['content'].startswith(MARKER))]
    try:
        _, entries = ProfileStore().snapshot()
        content = (load_text('instructions.json', 'profile_context') + json.dumps(entries, ensure_ascii=False))
        if current_context().identity == 'local' and current_context().source == 'local':
            from base import workspace_root
            legacy = workspace_root() / 'profile.md'
            if legacy.is_file():
                content += '\n本機手動維護的 profile.md（自動 profile 的明確更正優先）：\n' + legacy.read_text(encoding='utf-8')[:20000]
    except Exception:
        content = 'Profile 載入失敗；不可推測先前個人資料或宣稱已套用原則。'
    cleaned.insert(1 if cleaned and cleaned[0].get('role') == 'system' else 0,
                   {'role': 'system', 'content': MARKER + content + '\n' + status})
    messages[:] = cleaned
    return messages
