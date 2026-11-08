"""Atomic admission of assignments, tool intents and cross-database outboxes.

Task IDs are reserved here; only the separately invoked R2 dispatcher admits
native TaskStore execution. Submitting an intent never starts a model/tool.
"""

import json
import time
import uuid

from .config import OfficeSettings
from .contracts import _identifier, _text
from .directory import OfficeConflict, OfficeNotFound, _page
from .models import StaffConfig
from .persistence import PersistenceLimits, canonical, digest


class OfficeCapacityError(OfficeConflict):
    pass


class OfficeLedger:
    def __init__(self, directory, *, settings=None, limits=None):
        self.directory = directory
        self.store = directory.store
        self.settings = settings if settings is not None else OfficeSettings.load()
        self.limits = limits if limits is not None else PersistenceLimits.load()
        if not isinstance(self.settings, OfficeSettings) or not isinstance(self.limits, PersistenceLimits):
            raise ValueError('Expected validated office settings and persistence limits')

    def _json(self, value):
        return canonical(value, self.limits.payload_bytes)

    @staticmethod
    def _row(conn, table, owner, key):
        _identifier(key, 'office ID')
        row = conn.execute(f'SELECT * FROM {table} WHERE owner=? AND id=?', (owner.key, key)).fetchone()
        if row is None:
            raise OfficeNotFound('Office intent not found')
        return row

    def _capacity(self, conn, owner, *, assignments=0, operations=0, outbox=0, bytes_added=0):
        for table, delta, maximum in (
            ('office_assignments', assignments, self.limits.assignments),
            ('office_operations', operations, self.limits.operations),
            ('office_outbox', outbox, self.limits.outbox),
        ):
            if conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] + delta > maximum:
                raise OfficeCapacityError('Office record capacity reached')
        if conn.execute('SELECT COUNT(*) FROM office_assignments WHERE owner=?', (owner.key,)).fetchone()[0] + assignments > self.limits.owner_assignments:
            raise OfficeCapacityError('Office owner record capacity reached')
        used = sum(conn.execute(f'SELECT COALESCE(SUM(length(CAST(payload AS BLOB))),0) FROM {table}').fetchone()[0]
                   for table in ('office_assignments', 'office_operations', 'office_outbox'))
        if used + bytes_added > self.limits.storage_bytes:
            raise OfficeCapacityError('Office payload storage capacity reached')

    @staticmethod
    def _assignment(row):
        return {key:row[key] for key in ('id','request_id','task_id','staff_id','team_id','staff_revision','team_revision','queue_seq','state')}

    @staticmethod
    def _operation(row):
        return {key:row[key] for key in ('id','assignment_id','request_id','fingerprint','state','approval_state')}

    @staticmethod
    def _delivery(row):
        return {key:row[key] for key in ('id','assignment_id','operation_id','kind','fingerprint','state','attempts','error')}

    def _outbox(self, conn, owner, key, assignment, operation, kind, payload):
        conn.execute('''INSERT INTO office_outbox
            (owner,id,assignment_id,operation_id,kind,fingerprint,payload,state,created)
            VALUES(?,?,?,?,?,?,?,'pending',?)''',
            (owner.key,key,assignment,operation,kind,digest(payload),payload,time.time()))

    def submit(self, request_id, staff_id, goal, *, staff_revision, criteria=None, team_id=None, team_revision=None):
        _identifier(request_id, 'request_id')
        _identifier(staff_id, 'staff_id')
        _text(goal, 'goal')
        if type(staff_revision) is not int or staff_revision < 1:
            raise ValueError('staff_revision must be a positive integer')
        if criteria is not None and type(criteria) is not dict:
            raise ValueError('criteria must be a JSON object')
        if team_id is not None:
            _identifier(team_id, 'team_id')
            if type(team_revision) is not int or team_revision < 1:
                raise ValueError('team_revision must be a positive integer')
        elif team_revision is not None:
            raise ValueError('team_revision requires a team')
        request = self._json(dict(staff_id=staff_id,goal=goal,staff_revision=staff_revision,
                                 criteria=criteria,team_id=team_id,team_revision=team_revision))
        fingerprint = digest(request)
        with self.directory._connection(write=True) as (conn, owner):
            previous = conn.execute('SELECT * FROM office_assignments WHERE owner=? AND request_id=?', (owner.key,request_id)).fetchone()
            if previous:
                if previous['fingerprint'] != fingerprint:
                    raise OfficeConflict('Request ID reused with different assignment content')
                return self._assignment(previous)
            staff = self.directory._row(conn,'office_staff',owner,staff_id,staff_revision)
            if not staff['enabled']:
                raise OfficeConflict('Staff is disabled')
            if team_id is not None:
                team = self.directory._row(conn,'office_teams',owner,team_id,team_revision)
                if not team['enabled'] or not conn.execute('SELECT 1 FROM office_members WHERE owner=? AND team_id=? AND staff_id=?', (owner.key,team_id,staff_id)).fetchone():
                    raise OfficeConflict('Assignment requires an enabled team and its member')
            queued = conn.execute("SELECT COUNT(*) FROM office_assignments WHERE owner=? AND state NOT IN ('cancelled','completed')", (owner.key,)).fetchone()[0]
            if queued >= self.settings.queue_limit:
                raise OfficeCapacityError('Office owner queue is full')
            key, event_id = uuid.uuid4().hex, uuid.uuid4().hex
            task_id = 'office:' + key
            if conn.execute('SELECT 1 FROM tasks WHERE owner=? AND id=?', (owner.key,task_id)).fetchone():
                raise OfficeConflict('Reserved task ID is already in use')
            from agent_execution.policy import current_context
            ceiling = current_context().allowed_tools
            snapshot = self._json({'request':json.loads(request),'staff':self.directory._staff(staff).to_dict(),
                                   'tool_ceiling':None if ceiling is None else sorted(ceiling)})
            message = self._json({'v':1,'id':event_id,'type':'office.assignment.queued','ref':request_id,
                                  'payload':{'assignment_id':key,'task_id':task_id,'staff_id':staff_id}})
            self._capacity(conn,owner,assignments=1,outbox=1,bytes_added=len(snapshot.encode())+len(message.encode()))
            sequence = conn.execute('SELECT COALESCE(MAX(queue_seq),0)+1 FROM office_assignments WHERE owner=?', (owner.key,)).fetchone()[0]
            conn.execute('''INSERT INTO office_assignments
                (owner,id,request_id,fingerprint,task_id,staff_id,team_id,staff_revision,team_revision,queue_seq,payload,state,created)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,'queued',?)''',
                (owner.key,key,request_id,fingerprint,task_id,staff_id,team_id,staff_revision,team_revision,sequence,snapshot,time.time()))
            self._outbox(conn,owner,event_id,key,None,'event',message)
            return self._assignment(self._row(conn,'office_assignments',owner,key))

    def prepare_operation(self, assignment_id, request_id, tool_name, arguments):
        _identifier(request_id,'operation request_id')
        _identifier(tool_name,'tool_name')
        if type(arguments) is not dict:
            raise ValueError('arguments must be a JSON object')
        request = self._json({'tool_name':tool_name,'arguments':arguments})
        fingerprint = digest(request)
        with self.directory._connection(write=True) as (conn, owner):
            assignment = self._row(conn,'office_assignments',owner,assignment_id)
            old = conn.execute('SELECT * FROM office_operations WHERE owner=? AND assignment_id=? AND request_id=?', (owner.key,assignment_id,request_id)).fetchone()
            if old:
                if old['fingerprint'] != fingerprint:
                    raise OfficeConflict('Operation request ID reused with different arguments')
                return self._operation(old)
            if assignment['state'] != 'queued':
                raise OfficeConflict('Assignment is not accepting operations')
            payload = json.loads(assignment['payload'])
            snapshot = payload['staff']
            ceiling = payload.get('tool_ceiling',[])
            if tool_name not in snapshot['allowed_tools'] or (ceiling is not None and tool_name not in ceiling):
                raise PermissionError('Tool is outside the frozen staff capability ceiling')
            self.directory._tools(StaffConfig(snapshot['name'],allowed_tools=frozenset({tool_name})))
            operation_id, outbox_id = uuid.uuid4().hex, uuid.uuid4().hex
            intent = self._json({'assignment_id':assignment_id,'operation_id':operation_id,
                                 'source':owner.source,'project':owner.project,'staff_id':assignment['staff_id'],
                                 'staff_revision':assignment['staff_revision'],'generation':1,**json.loads(request)})
            payload = self._json({'approval_id':outbox_id,'action':'office:'+operation_id,
                                  'arguments':json.loads(intent),'expires':time.time()+self.settings.approval_ttl_seconds})
            self._capacity(conn,owner,operations=1,outbox=1,bytes_added=len(intent.encode())+len(payload.encode()))
            conn.execute('''INSERT INTO office_operations
                (owner,id,assignment_id,request_id,fingerprint,payload,state,created)
                VALUES(?,?,?,?,?,?,'waiting_approval',?)''',
                (owner.key,operation_id,assignment_id,request_id,fingerprint,intent,time.time()))
            self._outbox(conn,owner,outbox_id,assignment_id,operation_id,'approval',payload)
            return self._operation(self._row(conn,'office_operations',owner,operation_id))

    def get_assignment(self, key):
        with self.directory._connection() as (conn,owner):
            return self._assignment(self._row(conn,'office_assignments',owner,key))

    def get_operation(self, key):
        with self.directory._connection() as (conn,owner):
            return self._operation(self._row(conn,'office_operations',owner,key))

    def list_assignments(self, *, limit=50, after_id=None):
        _page(limit,after_id)
        with self.directory._connection() as (conn,owner):
            return tuple(self._assignment(row) for row in conn.execute('SELECT * FROM office_assignments WHERE owner=? AND id>? ORDER BY id LIMIT ?', (owner.key,after_id or '',limit)))

    def list_outbox(self, *, limit=50, after_id=None):
        _page(limit,after_id)
        with self.directory._connection() as (conn,owner):
            return tuple(self._delivery(row) for row in conn.execute('SELECT * FROM office_outbox WHERE owner=? AND id>? ORDER BY id LIMIT ?', (owner.key,after_id or '',limit)))

    def cancel_assignment(self, key):
        with self.directory._connection(write=True) as (conn,owner):
            assignment = self._row(conn,'office_assignments',owner,key)
            if assignment['state'] == 'cancelled':
                return self._assignment(assignment)
            if assignment['state'] == 'active' and conn.execute('SELECT 1 FROM office_executions WHERE owner=? AND assignment_id=?', (owner.key,key)).fetchone():
                from .cancellation import cancel_tasks
                cancel_tasks(conn,owner.key,assignment['task_id'])
                conn.execute("UPDATE tasks SET state='cancelled',lease_owner=NULL,lease_until=NULL,reply='Task cancelled' WHERE owner=? AND id=? AND state NOT IN ('completed','failed','cancelled')", (owner.key,assignment['task_id']))
                return self._assignment(self._row(conn,'office_assignments',owner,key))
            if assignment['state'] != 'queued':
                raise OfficeConflict('Unknown or active work requires resolution before cancellation')
            conn.execute("UPDATE office_assignments SET state='cancelled' WHERE owner=? AND id=?", (owner.key,key))
            conn.execute("UPDATE office_operations SET state='cancelled' WHERE owner=? AND assignment_id=?", (owner.key,key))
            conn.execute("""UPDATE office_outbox SET state=CASE WHEN attempts=0 THEN 'cancelled' ELSE 'cancel_pending' END,
                token=NULL,lease_until=NULL WHERE owner=? AND assignment_id=? AND state!='cancelled'""", (owner.key,key))
            return self._assignment(self._row(conn,'office_assignments',owner,key))
