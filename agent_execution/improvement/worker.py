import importlib.util
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))

if __name__=='__main__':
    value=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    spec=importlib.util.spec_from_file_location('trusted_improvement_benchmark',value['runner'])
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    from agent_execution.policy import execution_scope
    from agent_execution.coordinator import READ_TOOLS
    from agent_execution.budgets import budget_scope
    from permissions import get_user_role,set_current_role
    role=value['role'] if value['context']['identity']=='local' else get_user_role(value['context']['identity'])
    value['context']['workspace']=Path(value['context']['workspace'])
    with execution_scope(**value['context'],allowed_tools=READ_TOOLS),set_current_role(role),budget_scope():result=module.run(value['case'],value['proposals'])
    Path(sys.argv[2]).write_text(json.dumps(result,ensure_ascii=False,allow_nan=False),encoding='utf-8')
