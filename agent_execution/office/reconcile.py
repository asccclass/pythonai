"""Bounded, owner-scoped receipt reconciliation; no tool/model execution."""

from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid

from agent_execution.approvals import ApprovalStore
from agent_execution.delivery import EventStore
from agent_execution.sessions import project_workspace
from .contracts import OwnerScope
from .directory import OfficeConflict
from .persistence import PersistenceLimits, digest
from .targets import BridgeTarget


def _outside_workspaces(path):
    from base import workspace_root
    mappings = json.loads(os.environ.get('AGENT_PROJECT_WORKSPACES','{}'))
    if not isinstance(mappings,dict):
        raise ValueError('Invalid project workspace mappings')
    if any(path.is_relative_to(root.resolve()) for root in [workspace_root(),*(project_workspace(p) for p in mappings)]):
        raise ValueError('Bridge databases must be outside agent workspaces')


class OfficeReconciler:
    def __init__(self, ledger, *, events, approvals):
        self.ledger = ledger
        paths = [ledger.store.path.resolve(),Path(events.path).resolve(),Path(approvals.path).resolve()]
        for index,path in enumerate(paths):
            _outside_workspaces(path)
            if any(path == other or os.path.samefile(path,other) for other in paths[:index]):
                raise ValueError('Office, event and approval databases must be distinct')
        self.targets = {'event':BridgeTarget('event',events,ledger.limits,ledger.store.path),
                        'approval':BridgeTarget('approval',approvals,ledger.limits,ledger.store.path)}

    def _bind(self, conn, owner, kind):
        target = self.targets[kind]
        instance = target.setup()
        old = conn.execute('SELECT * FROM office_targets WHERE kind=?', (kind,)).fetchone()
        if old is not None and (old['path'],old['instance']) != (str(target.path),instance):
            raise OfficeConflict('Office target path or database identity changed')
        conn.execute('INSERT OR IGNORE INTO office_targets VALUES(?,?,?)', (kind,str(target.path),instance))
        attempted = conn.execute('SELECT 1 FROM office_outbox WHERE owner=? AND kind=? AND attempts>0 LIMIT 1', (owner.key,kind)).fetchone()
        target.reserve_owner(owner,require_existing=attempted is not None)
        return target

    def _observe(self, conn, owner, row, state):
        if row['operation_id'] is None:
            return
        if state == 'consumed':
            conn.execute("UPDATE office_operations SET state='uncertain',approval_state=? WHERE owner=? AND id=?", (state,owner.key,row['operation_id']))
            conn.execute("UPDATE office_assignments SET state='uncertain' WHERE owner=? AND id=?", (owner.key,row['assignment_id']))
        else:
            operation = {'approved':'ready','denied':'denied','expired':'expired','cancelled':'cancelled'}.get(state,'waiting_approval')
            conn.execute('UPDATE office_operations SET state=?,approval_state=? WHERE owner=? AND id=?', (operation,state,owner.key,row['operation_id']))

    def reconcile(self, *, limit=20):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('Reconcile limit must be 1..100')
        with self.ledger.directory._connection(write=True) as (conn,owner):
            ids = [row['id'] for row in conn.execute("""SELECT b.id FROM office_outbox b
                LEFT JOIN office_operations o ON o.owner=b.owner AND o.id=b.operation_id
                WHERE b.owner=? AND (b.state IN ('pending','sending','uncertain','cancel_pending') OR
                  (b.kind='approval' AND b.state='delivered' AND o.approval_state IN ('pending','approved')))
                ORDER BY CASE WHEN b.state='cancel_pending' THEN 0 ELSE 1 END,b.attempts,b.created,b.id LIMIT ?""", (owner.key,limit))]
        results = []
        for key in ids:
            result = self._one(key)
            results.append(result)
        return tuple(results)

    def _one(self, key):
        token = uuid.uuid4().hex
        with self.ledger.directory._connection(write=True) as (conn,owner):
            row = dict(self.ledger._row(conn,'office_outbox',owner,key))
            if digest(row['payload']) != row['fingerprint']:
                raise OfficeConflict('Source outbox fingerprint mismatch')
            if row['state'] in {'cancelled'}:
                return {'id':key,'state':row['state']}
            if row['state'] in {'sending','cancel_pending'} and row['token'] and (row['lease_until'] or 0) > time.time():
                conn.execute('UPDATE office_outbox SET attempts=attempts+1 WHERE owner=? AND id=?', (owner.key,key))
                return {'id':key,'state':'busy'}
            target = self._bind(conn,owner,row['kind'])
            # Persist the attempt before any target publication. A missing receipt
            # after a started attempt is uncertain, never proof of no effect.
            action = 'cancel' if row['state'] == 'cancel_pending' else 'publish' if row['state'] == 'pending' else 'probe'
            conn.execute("UPDATE office_outbox SET state=?,token=?,lease_until=?,attempts=attempts+1,error='' WHERE owner=? AND id=?",
                         ('cancel_pending' if action=='cancel' else 'sending',token,time.time()+self.ledger.limits.delivery_lease_seconds,owner.key,key))
        try:
            if action == 'publish':
                target.publish(owner,row)
            state = target.cancel(owner,row) if action == 'cancel' else target.probe(owner,row)
            if state is None:
                state = 'uncertain'
            with self.ledger.directory._connection(write=True) as (conn,current):
                fresh = self.ledger._row(conn,'office_outbox',current,key)
                if fresh['token'] != token:
                    return {'id':key,'state':'fenced'}
                if state == 'consumed' and action == 'cancel':
                    result = 'uncertain'
                else:
                    result = 'cancelled' if action == 'cancel' else 'uncertain' if state=='uncertain' else 'delivered'
                conn.execute('UPDATE office_outbox SET state=?,token=NULL,lease_until=NULL,error=? WHERE owner=? AND id=?',
                             (result,'missing_receipt' if state=='uncertain' else '',owner.key,key))
                if state != 'uncertain':
                    self._observe(conn,owner,row,state)
                return {'id':key,'state':result}
        except Exception:
            # Preserve only a fixed code; provider arguments and DB paths are private.
            with self.ledger.directory._connection(write=True) as (conn,current):
                failed = 'cancel_pending' if action=='cancel' else 'uncertain'
                conn.execute("UPDATE office_outbox SET state=?,token=NULL,lease_until=NULL,error='target_or_ack_failed' WHERE owner=? AND id=? AND token=?", (failed,current.key,key,token))
            return {'id':key,'state':failed}


def erase_external(task_path, owners):
    """Outbox erasure consumer. Preserve hashed fences against late publishers."""
    with closing(sqlite3.connect(task_path)) as conn:
        conn.row_factory = sqlite3.Row
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='office_outbox'").fetchone():
            return
        from .store import schema_version, SCHEMA_VERSION, office_busy
        if schema_version(conn) != SCHEMA_VERSION or office_busy(conn,owners):
            raise OfficeConflict('Office work must settle before external erasure')
        for owner_key in owners:
            owner = OwnerScope(*json.loads(owner_key))
            kinds = [r['kind'] for r in conn.execute('SELECT DISTINCT kind FROM office_outbox WHERE owner=? AND attempts>0', (owner_key,))]
            for kind in kinds:
                binding = conn.execute('SELECT * FROM office_targets WHERE kind=?', (kind,)).fetchone()
                if binding is None or not Path(binding['path']).is_file():
                    raise OfficeConflict('Office target unavailable for erasure')
                with closing(sqlite3.connect(Path(binding['path']).resolve().as_uri()+'?mode=ro',uri=True)) as check:
                    meta = check.execute('SELECT kind,instance FROM office_bridge_meta WHERE id=1').fetchone()
                    if meta != (kind,binding['instance']):
                        raise OfficeConflict('Office erasure target identity changed')
                target_store = EventStore(binding['path']) if kind=='event' else ApprovalStore(binding['path'])
                target = BridgeTarget(kind,target_store,PersistenceLimits(),task_path)
                target.instance = binding['instance']
                target.erase(owner)
