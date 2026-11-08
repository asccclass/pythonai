"""Atomic target receipts and owner fences in EventStore / ApprovalStore DBs."""

from contextlib import closing, contextmanager
import json
from pathlib import Path
import time
import uuid

from agent_execution.approvals import canonical as approval_json, ApprovalStore
from agent_execution.delivery import EventStore
from .directory import OfficeConflict
from .ledger import OfficeCapacityError
from .persistence import digest


_DDL = (
    "CREATE TABLE IF NOT EXISTS office_bridge_meta(id INTEGER PRIMARY KEY CHECK(id=1),kind TEXT NOT NULL,instance TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS office_bridge_origin(id INTEGER PRIMARY KEY CHECK(id=1),path TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS office_bridge_owners(hash TEXT PRIMARY KEY,state TEXT NOT NULL CHECK(state IN ('active','deleted')))",
    """CREATE TABLE IF NOT EXISTS office_bridge_receipts(
        key TEXT PRIMARY KEY,owner TEXT NOT NULL,fingerprint TEXT NOT NULL,record_id TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('active','cancelled')))""",
    "CREATE INDEX IF NOT EXISTS office_bridge_owner ON office_bridge_receipts(owner)",
)


class BridgeTarget:
    def __init__(self, kind, store, limits, source_path):
        expected = EventStore if kind == 'event' else ApprovalStore
        if kind not in {'event','approval'} or not isinstance(store, expected):
            raise TypeError('Unsupported office target store')
        self.kind, self.store, self.limits = kind, store, limits
        self.path = Path(store.path).resolve()
        self.source_path = str(Path(source_path).resolve())
        self.instance = None

    def setup(self):
        with closing(self.store.connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            for statement in _DDL:
                conn.execute(statement)
            row = conn.execute('SELECT kind,instance FROM office_bridge_meta WHERE id=1').fetchone()
            if row is None:
                instance = uuid.uuid4().hex
                conn.execute('INSERT INTO office_bridge_meta VALUES(1,?,?)', (self.kind,instance))
            else:
                if row['kind'] != self.kind:
                    raise OfficeConflict('Target database kind mismatch')
                instance = row['instance']
                if not isinstance(instance,str) or len(instance)!=32 or any(ch not in '0123456789abcdef' for ch in instance):
                    raise OfficeConflict('Invalid target database identity')
            origin = conn.execute('SELECT path FROM office_bridge_origin WHERE id=1').fetchone()
            if origin is None:
                conn.execute('INSERT INTO office_bridge_origin VALUES(1,?)', (self.source_path,))
            elif origin['path'] != self.source_path:
                raise OfficeConflict('Target database belongs to a different office source')
            self.instance = instance
            return instance

    @contextmanager
    def transaction(self):
        with closing(self.store.connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT kind,instance FROM office_bridge_meta WHERE id=1').fetchone()
            if row is None or row['instance'] != self.instance or row['kind'] != self.kind:
                raise OfficeConflict('Target database instance changed')
            origin = conn.execute('SELECT path FROM office_bridge_origin WHERE id=1').fetchone()
            if origin is None or origin['path'] != self.source_path:
                raise OfficeConflict('Target source binding changed')
            yield conn

    def reserve_owner(self, owner, *, require_existing=False):
        with self.transaction() as conn:
            key = digest(owner.key)
            row = conn.execute('SELECT state FROM office_bridge_owners WHERE hash=?', (key,)).fetchone()
            if row is None:
                if require_existing:
                    raise OfficeConflict('Target owner fence was lost; inspect the restored database')
                if conn.execute('SELECT COUNT(*) FROM office_bridge_owners').fetchone()[0] >= self.limits.target_receipts:
                    raise OfficeCapacityError('Target owner fence capacity reached')
                conn.execute("INSERT INTO office_bridge_owners VALUES(?,'active')", (key,))
            elif row['state'] != 'active':
                raise PermissionError('Office target owner was deleted')

    @staticmethod
    def _owner(conn, owner):
        row = conn.execute('SELECT state FROM office_bridge_owners WHERE hash=?', (digest(owner.key),)).fetchone()
        if row is None or row['state'] != 'active':
            raise PermissionError('Office target owner is not active')

    @staticmethod
    def _key(owner, row):
        if row['owner'] != owner.key:
            raise PermissionError('Outbox owner mismatch')
        if digest(row['payload']) != row['fingerprint']:
            raise OfficeConflict('Outbox payload fingerprint mismatch')
        return digest(owner.key + ':' + row['id'])

    def _receipt(self, conn, owner, row):
        result = conn.execute('SELECT * FROM office_bridge_receipts WHERE key=?', (self._key(owner,row),)).fetchone()
        if result is not None and (result['owner'] != owner.key or result['fingerprint'] != row['fingerprint']):
            raise OfficeConflict('Target receipt fingerprint mismatch')
        if result is not None and self.kind == 'approval' and result['record_id'] != json.loads(row['payload'])['approval_id']:
            raise OfficeConflict('Target receipt record ID mismatch')
        return result

    def _capacity(self, conn):
        if conn.execute('SELECT COUNT(*) FROM office_bridge_receipts').fetchone()[0] >= self.limits.target_receipts:
            raise OfficeCapacityError('Target receipt capacity reached')

    @staticmethod
    def _approval(conn, owner, payload):
        row = conn.execute('SELECT * FROM approvals WHERE id=?', (payload['approval_id'],)).fetchone()
        if row is not None:
            args = approval_json(payload['arguments'])
            if (row['identity'],row['session'],row['action'],row['arguments'],row['digest'],row['expires']) != (
                owner.identity,owner.key,payload['action'],args,digest(args),payload['expires']
            ):
                raise OfficeConflict('Target approval binding mismatch')
        return row

    def probe(self, owner, row):
        with self.transaction() as conn:
            self._owner(conn,owner)
            receipt = self._receipt(conn,owner,row)
            if receipt is None:
                return None
            if receipt['state'] == 'cancelled':
                return 'cancelled'
            if self.kind == 'event':
                # Receipts survive ACK/TTL cleanup; do not recreate an old event.
                return 'delivered'
            approval = self._approval(conn,owner,json.loads(row['payload']))
            if approval is None:
                raise OfficeConflict('Published approval is missing')
            if approval['state'] in {'pending','approved'} and approval['expires'] <= time.time():
                return 'expired'
            return approval['state']

    def publish(self, owner, row):
        payload = json.loads(row['payload'])
        with self.transaction() as conn:
            self._owner(conn,owner)
            previous = self._receipt(conn,owner,row)
            if previous is not None:
                return
            self._capacity(conn)
            if self.kind == 'event':
                saved = self.store.append_transaction(conn,owner.key,payload,event_key=self._key(owner,row))
                if saved is None:
                    raise OfficeConflict('Event identity exists without its bridge receipt')
                record_id = str(saved['seq'])
            else:
                record_id = payload['approval_id']
                if self._approval(conn,owner,payload) is not None:
                    raise OfficeConflict('Approval exists without its bridge receipt')
                args = approval_json(payload['arguments'])
                state = 'pending' if payload['expires'] > time.time() else 'expired'
                conn.execute('INSERT INTO approvals VALUES(?,?,?,?,?,?,?,?)',
                             (record_id,owner.identity,owner.key,payload['action'],args,digest(args),payload['expires'],state))
                self.store.audit(conn,record_id,owner.identity,owner.key,'requested')
            conn.execute("INSERT INTO office_bridge_receipts VALUES(?,?,?,?,'active')",
                         (self._key(owner,row),owner.key,row['fingerprint'],record_id))

    def cancel(self, owner, row):
        payload = json.loads(row['payload'])
        with self.transaction() as conn:
            self._owner(conn,owner)
            receipt = self._receipt(conn,owner,row)
            if receipt is None:
                self._capacity(conn)
            record_id = receipt['record_id'] if receipt else (payload['approval_id'] if self.kind == 'approval' else '')
            if self.kind == 'approval':
                approval = self._approval(conn,owner,payload)
                if approval is not None and approval['state'] == 'consumed':
                    return 'consumed'
                if approval is not None and approval['state'] in {'pending','approved'}:
                    conn.execute("UPDATE approvals SET state='cancelled' WHERE id=?", (record_id,))
                    self.store.audit(conn,record_id,owner.identity,owner.key,'cancelled')
            conn.execute("""INSERT INTO office_bridge_receipts VALUES(?,?,?,?,'cancelled')
                ON CONFLICT(key) DO UPDATE SET state='cancelled'""",
                (self._key(owner,row),owner.key,row['fingerprint'],record_id))
            return 'cancelled'

    def erase(self, owner):
        with self.transaction() as conn:
            fence = conn.execute('SELECT state FROM office_bridge_owners WHERE hash=?', (digest(owner.key),)).fetchone()
            if fence is None:
                raise OfficeConflict('Target owner erasure fence is missing')
            conn.execute("UPDATE office_bridge_owners SET state='deleted' WHERE hash=?", (digest(owner.key),))
            if self.kind == 'event':
                for table in ('ws_events','ws_sessions','ws_inputs','ws_event_keys'):
                    if conn.execute('SELECT 1 FROM sqlite_master WHERE name=?', (table,)).fetchone():
                        conn.execute(f'DELETE FROM {table} WHERE owner=?', (owner.key,))
            else:
                ids = [item['id'] for item in conn.execute("SELECT id FROM approvals WHERE identity=? AND session=? AND action LIKE 'office:%'", (owner.identity,owner.key))]
                for key in ids:
                    conn.execute('DELETE FROM approval_events WHERE approval_id=?', (key,))
                    conn.execute('DELETE FROM approvals WHERE id=?', (key,))
            conn.execute('DELETE FROM office_bridge_receipts WHERE owner=?', (owner.key,))
