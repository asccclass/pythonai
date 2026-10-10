"""At-most-once scheduled notifications through configured communication adapters."""
from contextlib import closing
import json
import time
import uuid
from communication_models import OutboundMessage
from permissions import get_user_role
from ..policy import ExecutionContext
from ..tasks import task_owner
from .store import SchedulerStore


class NotificationDispatcher:
    def __init__(self,adapters,allowed_senders=None,store=None):
        self.adapters={name:adapter for name,adapter in adapters.items() if name in {'telegram','discord','line'} and getattr(adapter,'platform',None)==name}
        self.allowed_senders=allowed_senders;self.store=store or SchedulerStore()
    def route(self,row):
        try:
            scope=json.loads(row['context']);identity=scope['identity'];session=scope['session']
            platform,sender=identity.split(':',1);session_platform,conversation=session.split(':',1)
            context=ExecutionContext(**{key:scope[key] for key in ('source','identity','project','session')})
            if scope['source']!='communication' or task_owner(context)!=row['owner'] or platform!=session_platform:raise ValueError()
            if platform not in self.adapters:return None
            if not sender or not conversation or len(conversation)>512 or len(sender)>512:raise ValueError()
            if self.allowed_senders is not None and identity not in self.allowed_senders:raise PermissionError()
            if get_user_role(identity)!='admin':raise PermissionError()
            return platform,conversation
        except (KeyError,ValueError,TypeError,PermissionError):return False
    def dispatch_once(self,check_cancelled=None):
        if not self.adapters:return 0
        sent=0;deadline=time.monotonic()+10
        with closing(self.store.connect()) as conn,conn:
            conn.execute("UPDATE schedule_notification_delivery SET status='uncertain',error_type='LostDeliveryWorker' WHERE status='sending' AND expires<?",(time.time(),))
            placeholders=','.join('?' for _ in self.adapters)
            rows=conn.execute(f'''SELECT n.*,j.context,d.status FROM schedule_notifications n JOIN scheduled_jobs j ON j.id=n.job_id
                LEFT JOIN schedule_notification_delivery d ON d.id=n.id WHERE d.id IS NULL
                AND json_extract(j.context,'$.source')='communication'
                AND substr(json_extract(j.context,'$.identity'),1,instr(json_extract(j.context,'$.identity'),':')-1) IN ({placeholders})
                ORDER BY n.created LIMIT 100''',list(self.adapters)).fetchall()
        for row in rows:
            if time.monotonic()>deadline or (check_cancelled and check_cancelled()):break
            route=self.route(row)
            if route is None:continue
            token=uuid.uuid4().hex
            with closing(self.store.connect()) as conn,conn:
                claimed=conn.execute('INSERT OR IGNORE INTO schedule_notification_delivery VALUES(?,?,?,?,?,?)',
                    (row['id'],'sending' if route else 'cancelled',token,time.time()+60,None,None)).rowcount
            if not claimed or not route:continue
            platform,conversation=route
            # One platform message: bounded output, no ambiguous partial multi-chunk resend.
            text=('排程通知 ['+row['state']+']\n'+row['output'])[:1800]
            try:
                self.adapters[platform].send_message(OutboundMessage(platform,conversation,text))
                status,error='sent',None;sent+=1
            except Exception as failure:status,error='uncertain',type(failure).__name__
            with closing(self.store.connect()) as conn,conn:
                conn.execute('UPDATE schedule_notification_delivery SET status=?,error_type=?,completed=? WHERE id=? AND token=? AND status=\'sending\'',
                    (status,error,time.time(),row['id'],token))
        return sent
