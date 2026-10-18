"""Persistent transport session fences and owner-scoped logical erasure."""
from contextlib import closing
import json
import re
import time
import uuid
import os
import sqlite3
from pathlib import Path


class DeletedConversation(PermissionError):
    pass


def cleanup_control(text):
    return bool(re.fullmatch(r'/session-delete (?:preview|(?:confirm|status|resume) [0-9a-f]{32})',text.strip()))


def scope_from_owner(owner):
    source,identity,project,session=json.loads(owner)
    platform,separator,sender=identity.partition(':')
    if source!='communication' or not separator:raise PermissionError('Invalid communication owner')
    return platform,sender,project,session


class LifecycleCommunication:
    def recovery_session(self,message):
        """Resolve before taking the transport write lock; keep cross-DB lock order acyclic."""
        match=re.fullmatch(r'/session-delete (?:confirm|status|resume) ([0-9a-f]{32})',message.text.strip())
        if not match:return None
        root=Path(os.environ.get('LOCALAPPDATA') or Path.home()/'.local'/'share')/'PythonAI'
        path=Path(os.environ.get('AGENT_TASK_DB') or root/'tasks.db')
        if not path.exists():return None
        with closing(sqlite3.connect(path)) as conn:
            names={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {'wl_transports','wl_plans'}<=names:return None
            principal=json.dumps(['communication',message.platform+':'+message.sender_id,message.project],ensure_ascii=False)
            rows=conn.execute('SELECT t.owner,t.path FROM wl_transports t JOIN wl_plans p ON p.session=t.session WHERE p.id=? AND p.principal=?',(match[1],principal)).fetchall()
        for owner,registered in rows:
            if Path(registered).resolve()!=self.db_path.resolve():continue
            platform,sender,project,session=scope_from_owner(owner)
            with closing(self.connect()) as conn:
                state=conn.execute('SELECT conversation FROM comm_session_states WHERE platform=? AND sender=? AND project=? AND session=?',(platform,sender,project,session)).fetchone()
            if state and state[0]==message.conversation_id:return session
        return None

    def lifecycle_initialize(self,conn):
        conn.executescript('''CREATE TABLE IF NOT EXISTS comm_session_heads (
            platform TEXT,sender TEXT,conversation TEXT,project TEXT,session TEXT NOT NULL,
            PRIMARY KEY(platform,sender,conversation,project));
            CREATE TABLE IF NOT EXISTS comm_session_states (
            platform TEXT,sender TEXT,project TEXT,session TEXT,conversation TEXT NOT NULL,state TEXT NOT NULL,
            PRIMARY KEY(platform,sender,project,session));''')
        for table,definitions in {
            'agent_command_jobs':{'session':"TEXT NOT NULL DEFAULT ''"},
            'comm_messages':{'project':'TEXT','session':'TEXT','owner_sender':'TEXT'},
            'comm_deliveries':{'session':"TEXT NOT NULL DEFAULT ''",'cleanup_receipt':'INTEGER NOT NULL DEFAULT 0'},
        }.items():
            columns={r[1] for r in conn.execute('PRAGMA table_info('+table+')')}
            for name,definition in definitions.items():
                if name not in columns:conn.execute('ALTER TABLE '+table+' ADD COLUMN '+name+' '+definition)
        conn.execute("UPDATE agent_command_jobs SET session=platform||':'||conversation_id WHERE session=''")
        conn.execute("UPDATE comm_deliveries SET session=platform||':'||conversation WHERE session=''")
        conn.execute('''UPDATE comm_messages SET
            project=(SELECT j.project FROM agent_command_jobs j WHERE j.source_message_id=comm_messages.id LIMIT 1),
            session=(SELECT j.session FROM agent_command_jobs j WHERE j.source_message_id=comm_messages.id LIMIT 1),
            owner_sender=(SELECT j.sender_id FROM agent_command_jobs j WHERE j.source_message_id=comm_messages.id LIMIT 1)
            WHERE session IS NULL AND EXISTS(SELECT 1 FROM agent_command_jobs j WHERE j.source_message_id=comm_messages.id)''')
        conn.execute('''UPDATE comm_messages SET
            project=(SELECT d.project FROM comm_deliveries d WHERE d.id=comm_messages.platform_message_id),
            session=(SELECT d.session FROM comm_deliveries d WHERE d.id=comm_messages.platform_message_id),
            owner_sender=(SELECT d.sender FROM comm_deliveries d WHERE d.id=comm_messages.platform_message_id)
            WHERE session IS NULL AND direction='outbound' AND EXISTS(SELECT 1 FROM comm_deliveries d WHERE d.id=comm_messages.platform_message_id)''')
        conn.execute("INSERT OR IGNORE INTO comm_session_states SELECT platform,sender_id,project,session,conversation_id,'active' FROM agent_command_jobs")
        conn.execute('INSERT OR IGNORE INTO comm_session_heads SELECT platform,sender_id,conversation_id,project,session FROM agent_command_jobs ORDER BY id DESC')

    def incoming_session(self,conn,message,source_message_id=None,recovery_session=None):
        if message.attachments and (cleanup_control(message.text) or message.text.strip()=='/new-session'):
            raise ValueError('Attachments cannot accompany session control commands')
        bound=conn.execute('SELECT session FROM comm_messages WHERE id=? AND platform=? AND sender_id=? AND project=? AND conversation_id=?',
            (source_message_id,message.platform,message.sender_id,message.project,message.conversation_id)).fetchone() if source_message_id else None
        if source_message_id and not bound:raise PermissionError('Source message belongs to another conversation')
        row=conn.execute('SELECT session FROM comm_session_heads WHERE platform=? AND sender=? AND conversation=? AND project=?',
            (message.platform,message.sender_id,message.conversation_id,message.project)).fetchone()
        session=bound[0] if bound and bound[0] else recovery_session or (row[0] if row else message.platform+':'+message.conversation_id)
        if message.text.strip()=='/new-session' and not bound:session=message.platform+':'+message.conversation_id+':'+uuid.uuid4().hex
        conn.execute("INSERT OR IGNORE INTO comm_session_states VALUES(?,?,?,?,?,'active')",(message.platform,message.sender_id,message.project,session,message.conversation_id))
        state=conn.execute('SELECT state FROM comm_session_states WHERE platform=? AND sender=? AND project=? AND session=?',
            (message.platform,message.sender_id,message.project,session)).fetchone()[0]
        recovery=bool(re.fullmatch(r'/session-delete (?:confirm|status|resume) [0-9a-f]{32}',message.text.strip()))
        cancellation=state=='deleting' and bool(re.fullmatch(r'/cancel \S{1,256}',message.text.strip()))
        if state!='active' and not recovery and not cancellation:
            raise DeletedConversation('Conversation was deleted; use /new-session to begin a new conversation')
        if not bound and not recovery_session:
            conn.execute('INSERT INTO comm_session_heads VALUES(?,?,?,?,?) ON CONFLICT(platform,sender,conversation,project) DO UPDATE SET session=excluded.session',
                (message.platform,message.sender_id,message.conversation_id,message.project,session))
        return session

    def lifecycle_state(self,command,conn=None):
        if conn is None:
            with closing(self.connect()) as db:return self.lifecycle_state(command,db)
        session=command.session or command.platform+':'+command.conversation_id
        row=conn.execute('SELECT state FROM comm_session_states WHERE platform=? AND sender=? AND project=? AND session=?',
            (command.platform,command.sender_id,command.project,session)).fetchone()
        return row[0] if row else 'active'

    def lifecycle_blockers(self,owner,conn=None):
        if conn is None:
            with closing(self.connect()) as db:return self.lifecycle_blockers(owner,db)
        platform,sender,project,session=scope_from_owner(owner)
        jobs=[r[0] for r in conn.execute("SELECT command_id,text FROM agent_command_jobs WHERE platform=? AND sender_id=? AND project=? AND session=? AND status IN ('pending','running','cancel_requested','suspended','recovery_required')",
            (platform,sender,project,session)) if not cleanup_control(r[1]) and r[1].strip()!='/new-session']
        sends=[r[0] for r in conn.execute("SELECT id FROM comm_deliveries WHERE platform=? AND sender=? AND project=? AND session=? AND state='sending' AND lease>?",(platform,sender,project,session,time.time()))]
        return ['communication:'+key for key in jobs]+['delivery:'+key for key in sends]

    def begin_session_deletion(self,owner):
        platform,sender,project,session=scope_from_owner(owner)
        with closing(self.connect()) as conn,conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute("UPDATE comm_session_states SET state='deleting' WHERE platform=? AND sender=? AND project=? AND session=? AND state='active'",(platform,sender,project,session))
            blockers=self.lifecycle_blockers(owner,conn)
        if blockers:raise ValueError('Communication work still active; cancel/wait before cleanup')

    def lifecycle_summary(self,owner):
        platform,sender,project,session=scope_from_owner(owner)
        with closing(self.connect()) as conn:
            state=conn.execute('SELECT conversation FROM comm_session_states WHERE platform=? AND sender=? AND project=? AND session=?',(platform,sender,project,session)).fetchone()
            messages=conn.execute('SELECT COUNT(*) FROM comm_messages WHERE platform=? AND owner_sender=? AND project=? AND session=?',(platform,sender,project,session)).fetchone()[0]
            attachments=conn.execute('SELECT COUNT(*) FROM comm_attachments a JOIN agent_command_jobs j ON j.command_id=a.command_id WHERE j.platform=? AND j.sender_id=? AND j.project=? AND j.session=?',(platform,sender,project,session)).fetchone()[0]
            unknown=conn.execute('SELECT COUNT(*) FROM comm_messages WHERE platform=? AND conversation_id=? AND session IS NULL',(platform,state[0])).fetchone()[0] if state else 0
        return {'kind':'communication','messages':messages,'attachments':attachments,'legacy_unmanaged_messages':unknown}

    def erase_session(self,owner):
        platform,sender,project,session=scope_from_owner(owner)
        with closing(self.connect()) as conn,conn:
            conn.execute('BEGIN IMMEDIATE')
            if self.lifecycle_blockers(owner,conn):raise ValueError('Communication work is still active')
            keys=[r[0] for r in conn.execute('SELECT command_id FROM agent_command_jobs WHERE platform=? AND sender_id=? AND project=? AND session=?',(platform,sender,project,session))]
            for key in keys:
                conn.execute("UPDATE comm_attachments SET filename='',mime='',file_id=NULL,data=X'',digest='',size=0,expires=0 WHERE command_id=?",(key,))
            conn.execute("UPDATE comm_messages SET text='',raw_payload='{}',status='deleted' WHERE platform=? AND owner_sender=? AND project=? AND session=?",(platform,sender,project,session))
            conn.execute("UPDATE agent_command_jobs SET text='',result_text='',last_error=NULL,state=NULL,suspended_tool_call_id=NULL,status='deleted' WHERE platform=? AND sender_id=? AND project=? AND session=?",(platform,sender,project,session))
            conn.execute("UPDATE comm_deliveries SET text='',reply_context='{}',state='deleted',lease=NULL,error=NULL WHERE platform=? AND sender=? AND project=? AND session=? AND cleanup_receipt=0",(platform,sender,project,session))
            conn.execute("UPDATE comm_session_states SET state='deleted' WHERE platform=? AND sender=? AND project=? AND session=?",(platform,sender,project,session))
