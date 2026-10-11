"""Secret-free conflict diagnostics and OS-released, per-bot polling locks."""
import hashlib
import json
import os
import math
import time
from pathlib import Path


class TelegramPollingConflict(RuntimeError):
    def __init__(self,kind='unknown'):
        self.kind=kind
        messages={
            'webhook':'A webhook is configured for this Bot. getUpdates cannot run until webhook mode is disabled. Use one receiving mode.',
            'poller':'Another getUpdates consumer is using this Bot. Stop the other polling worker on this or another host, then restart.',
            'local':'A polling worker for this Bot is already running on this host. Keep only one instance.',
            'unknown':'Telegram rejected getUpdates with HTTP 409. Check for a configured webhook or another polling consumer before restarting.'}
        super().__init__(messages.get(kind,messages['unknown']))


def conflict_from_payload(payload):
    description=str(payload.get('description','')).lower() if isinstance(payload,dict) else ''
    kind='webhook' if 'webhook' in description else 'poller' if 'getupdates' in description else 'unknown'
    # Never echo provider text: it can contain credential-bearing URLs or tokens.
    return TelegramPollingConflict(kind)


def conflict_from_http(error):
    try:
        try:payload=json.loads(error.read(8192))
        except (ValueError,OSError,AttributeError,TypeError):payload={}
        return conflict_from_payload(payload)
    finally:error.close()


class PollingLease:
    """A native advisory lock; process exit/crash releases it without stale leases."""
    def __init__(self,bot_token,directory=None):
        if not bot_token:raise ValueError('TELEGRAM_BOT_TOKEN is required to poll updates')
        bot_id=bot_token.split(':',1)[0]
        identity=bot_id if bot_id.isdigit() else bot_token
        key=hashlib.sha256(identity.encode()).hexdigest()
        root=Path(directory or os.environ.get('AGENT_TELEGRAM_POLL_LOCK_DIR') or
                  Path(os.environ.get('LOCALAPPDATA',Path.home()))/'PythonAI'/'telegram-polling')
        self.path=root/(key+'.lock');self.file=None
    def __enter__(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        handle=self.path.open('a+b')
        try:
            if self.path.stat().st_size==0:handle.write(b'0');handle.flush()
            handle.seek(0)
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            handle.close();raise TelegramPollingConflict('local') from None
        self.file=handle;return self
    def __exit__(self,*args):
        if self.file:self.file.close();self.file=None

    def pending_delay(self):
        """Bounded recovery for an interrupted request from the previous owner."""
        self.file.seek(1)
        try:
            deadline=json.loads(self.file.read(512)).get('pending_until',0)
            if type(deadline) not in (int,float) or not math.isfinite(deadline):return 0
            return min(310,max(0,deadline-time.time()))
        except (ValueError,AttributeError,TypeError):
            return 0

    def _save_pending(self,deadline):
        # Keep the locked first byte intact. Metadata contains no Bot credentials.
        self.file.seek(1)
        self.file.truncate()
        self.file.write(json.dumps({'pending_until':deadline}).encode('utf-8'))
        self.file.flush()

    def begin_request(self,timeout):
        self._save_pending(time.time()+timeout+10)

    def complete_request(self):
        self._save_pending(0)
