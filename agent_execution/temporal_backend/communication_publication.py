"""Final/wait notifications through the existing communication delivery outbox."""
from contextlib import closing
import json
from pathlib import Path

from agent_execution.tasks import TaskStore, task_owner
from agent_execution.policy import ExecutionContext


def event_key(command, text):
    tasks = TaskStore()
    owner = task_owner(ExecutionContext(source='communication',identity=f'{command.platform}:{command.sender_id}',
        project=command.project,session=command.session or f'{command.platform}:{command.conversation_id}'))
    with closing(tasks.connect()) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='temporal_events'").fetchone():
            return None
        item = conn.execute("SELECT e.payload FROM temporal_events e JOIN temporal_submissions s ON s.id=e.submission_id WHERE s.owner=? AND s.task_id=? AND e.kind='end' ORDER BY e.seq DESC LIMIT 1",(owner,command.command_id)).fetchone()
    if item:
        payload = json.loads(item[0])
        if payload['text']==text:
            return payload['temporal_event_id']
    return None


def publish(store, row, target):
    from communication_store import CommunicationStore
    scope = json.loads(row['context'])
    if (scope['source']!='communication' or target['owner']!=row['owner'] or target['ref']!=row['task_id'] or
        str(Path(scope.get('communication_db','')).resolve())!=target['path'] or not Path(target['path']).is_file()):
        raise PermissionError('Invalid communication delivery scope')
    outbox = CommunicationStore(target['path'])
    command = outbox.command_by_id(row['task_id'])
    expected = task_owner(ExecutionContext(source='communication',identity=f'{command.platform}:{command.sender_id}',
        project=command.project,session=command.session or f'{command.platform}:{command.conversation_id}'))
    if expected!=row['owner']:
        raise PermissionError('Communication command owner changed')
    with closing(store.tasks.connect()) as conn,conn:
        if row['state'] in {'cancelled','uncertain'}:
            conn.execute("UPDATE temporal_events SET delivery='cancelled' WHERE submission_id=? AND delivery='pending' AND event_key!='final:1'",(row['id'],))
        pending = conn.execute("SELECT * FROM temporal_events WHERE submission_id=? AND kind='end' AND delivery='pending' ORDER BY seq",(row['id'],)).fetchall()
    for item in pending:
        payload = json.loads(item['payload'])
        outbox.queue_delivery(command,payload['text'],outbox.reply_context(command.source_message_id),
                              payload.get('failed',False),event_key=payload['temporal_event_id'])
        if not payload.get('suspended'):
            outbox.complete_command(command.command_id,payload['text'])
        with closing(store.tasks.connect()) as conn,conn:
            conn.execute("UPDATE temporal_events SET delivery='sent' WHERE submission_id=? AND seq=?",(row['id'],item['seq']))
