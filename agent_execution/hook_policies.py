"""Configured built-in policies; hooks never grant authority or execute a tool."""
import inspect
import json
import os
from pathlib import Path
import math

from hooks import FunctionHook, HookLifecycle, HookManager, HookResult


def request_validation(context):
    if not isinstance(context.messages,list) or not all(isinstance(m,dict) and m.get('role') in {'system','developer','user','assistant','tool'} for m in context.messages):
        raise ValueError('Invalid model messages')
    if not isinstance(context.model_request.get('model'),str) or not context.model_request['model']:
        raise ValueError('Model ID is required')
    if not isinstance(context.model_request.get('tools',[]),list):
        raise ValueError('Tools must be a list')
    return HookResult.continue_()


def model_budget(context):
    from .budgets import active_budget
    budget=active_budget()
    if budget:budget.check()
    return HookResult.continue_(budget_enabled=budget is not None)


def response_validation(context):
    message=context.model_response
    calls=getattr(message,'tool_calls',None) or []
    if len({call.id for call in calls})!=len(calls):raise ValueError('Duplicate model tool call IDs')
    for call in calls:
        if not call.id or not isinstance(call.function.name,str) or not call.function.name:
            raise ValueError('Tool calls require an ID and function name')
        arguments=json.loads(call.function.arguments or '{}')
        if not isinstance(arguments,dict):raise ValueError('Tool arguments must be an object')
    return HookResult.continue_()


def argument_validation(context):
    if not isinstance(context.tool_arguments,dict):raise ValueError('Tool arguments must be an object')
    function=context.metadata.get('function')
    if function is None:
        from base import TOOLS
        function=TOOLS.get(context.tool_name)
    if function is not None:
        values={k:v for k,v in context.tool_arguments.items() if k!='approval_id'}
        inspect.signature(function).bind(**values)
    from base import TOOLS_SCHEMAS
    spec=next((item.get('function',{}).get('parameters',{}) for item in TOOLS_SCHEMAS
        if item.get('function',{}).get('name')==context.tool_name),{})
    required=set(spec.get('required',[]))
    for name,schema in spec.get('properties',{}).items():
        if name not in context.tool_arguments:continue
        value=context.tool_arguments[name]
        if value is None and name not in required:continue
        validate_value(value,schema,name)
    return HookResult.continue_()


def validate_value(value,schema,name):
    expected=schema.get('type')
    types={
        'string':isinstance(value,(str,Path)), 'array':isinstance(value,list),
        'object':isinstance(value,dict),'boolean':type(value) is bool,
        'integer':type(value) is int,'number':type(value) in {int,float} and math.isfinite(value),
        'null':value is None,
    }
    if isinstance(expected,str) and expected in types and not types[expected]:
        raise ValueError(f'Invalid argument type: {name} must be {expected}')
    if 'enum' in schema and value not in schema['enum']:raise ValueError(f'Invalid argument choice: {name}')
    if expected=='array' and isinstance(schema.get('items'),dict):
        for item in value:validate_value(item,schema['items'],name+'[]')


def tool_admission(context):
    from .policy import current_context, _validate_paths, PolicyDenied,check_tool_permission,current_role
    from .budgets import active_budget
    if not check_tool_permission(context.tool_name):
        raise PolicyDenied(f"Role '{current_role()}' cannot execute {context.tool_name}")
    scope=current_context()
    _validate_paths(context.tool_arguments,scope.trusted_assets if context.tool_name=='run_command' else ())
    budget=active_budget()
    if budget:budget.check()
    if scope.check_cancelled and scope.check_cancelled():raise InterruptedError('Task cancelled')
    return HookResult.continue_(source=scope.source,identity=scope.identity,project=scope.project)


def result_audit(context):
    from .results import normalize_result
    execution=normalize_result(context.tool_result,context.tool_name)
    context.audit_metadata['execution']=execution.to_dict()
    return HookResult.continue_(execution=execution.to_dict())


POLICIES={
    'request_validation':(HookLifecycle.PRE_MODEL_CALL,request_validation,10),
    'model_budget':(HookLifecycle.PRE_MODEL_CALL,model_budget,20),
    'response_validation':(HookLifecycle.POST_MODEL_CALL,response_validation,10),
    'argument_validation':(HookLifecycle.PRE_TOOL_EXECUTE,argument_validation,10),
    'tool_admission':(HookLifecycle.PRE_TOOL_EXECUTE,tool_admission,20),
    'result_audit':(HookLifecycle.POST_TOOL_EXECUTE,result_audit,10),
}


def configured_manager(diagnose=False):
    config=json.loads(os.environ.get('AGENT_HOOK_POLICIES','{}'))
    if not isinstance(config,dict) or set(config)-set(POLICIES):raise ValueError('Unknown hook policy configuration')
    hooks=[]
    for name,(lifecycle,function,priority) in POLICIES.items():
        options=config.get(name,{})
        if not isinstance(options,dict) or set(options)-{'enabled','priority','fail_closed'}:
            raise ValueError(f'Invalid hook policy options: {name}')
        enabled=options.get('enabled',True)
        fail_closed=options.get('fail_closed',True)
        rank=options.get('priority',priority)
        if type(enabled) is not bool or type(fail_closed) is not bool or type(rank) is not int:
            raise ValueError(f'Invalid hook policy types: {name}')
        hooks.append(FunctionHook(name,lifecycle,function,priority=rank,enabled=enabled,fail_closed=fail_closed))
    manager=HookManager(hooks)
    if diagnose:
        print('Hook policies: '+json.dumps(manager.diagnostics(),ensure_ascii=False))
    return manager


def tool_context(name,arguments,function=None,result=None):
    from .policy import current_context
    from permissions import current_role
    from hooks import HookContext
    scope=current_context()
    return HookContext(HookLifecycle.PRE_TOOL_EXECUTE,tool_name=name,tool_arguments=dict(arguments),tool_result=result,
        memory=scope.memory,episode_id=scope.episode_id,
        metadata={'function':function,'source':scope.source,'identity':scope.identity,'project':scope.project,
            'task_id':scope.task_id,'role':current_role()})
