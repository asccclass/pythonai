import os
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[3]))
from agent_execution.policy import execution_scope,execute_tool
from agent_execution.budgets import TaskBudget,_budget

if __name__=='__main__':
    database,owner,root=sys.argv[1:]
    budget=TaskBudget(owner,root,database);token=_budget.set(budget)
    try:
        with execution_scope(source='subagent',task_id='child-task',coordinator_owner=owner,coordinator_node='child-node'):
            execute_tool('fixture',{},lambda:'done')
    finally:_budget.reset(token)
