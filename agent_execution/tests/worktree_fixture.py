"""Real process loss after a durable workspace binding and checkpoint."""
import os
from pathlib import Path
import sys

from base import workspace_root
from agent_execution.policy import execution_scope
from agent_execution.tasks import durable_invoke,save_checkpoint


if __name__=='__main__':
    messages=[{'role':'user','content':'crash worktree'}]
    def crash(saved):
        (workspace_root()/'same.txt').write_text('durable child bytes')
        save_checkpoint('model_pending',messages)
        os._exit(23)
    with execution_scope(workspace=Path(sys.argv[1]),task_id='process-crash'):
        durable_invoke(crash,messages,None)
