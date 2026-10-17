"""Activity-local ledger context; importing this module never imports the SDK."""
from contextlib import contextmanager
from contextvars import ContextVar

_ledger = ContextVar('temporal_operation_ledger', default=None)


@contextmanager
def operation_scope(ledger):
    token = _ledger.set(ledger)
    try:
        yield
    finally:
        _ledger.reset(token)


def run_operation(kind, step, arguments, execute, **serialization):
    ledger = _ledger.get()
    if ledger is None:
        return execute()
    if step is None:
        raise ValueError('Temporal operation requires a stable logical step ID')
    if callable(arguments):
        arguments = arguments()
    from agent_execution.policy import current_context
    from permissions import current_role, get_user_role, set_current_role, check_tool_permission
    context = current_context()
    role = current_role()
    if context.identity != 'local':
        levels = {'viewer': 0, 'operator': 1, 'admin': 2}
        role = min((role, get_user_role(context.identity)), key=lambda value: levels.get(value, -1))
    with set_current_role(role):
        if kind == 'tool':
            name = arguments['name']
            if not check_tool_permission(name) or (context.allowed_tools is not None and name not in context.allowed_tools):
                raise PermissionError('Operation tool is no longer authorized')
        elif kind == 'model':
            for schema in arguments['request']['tools']:
                name = schema.get('function', {}).get('name')
                if not check_tool_permission(name) or (context.allowed_tools is not None and name not in context.allowed_tools):
                    raise PermissionError('Model capabilities changed; refresh the execution context')
        return ledger.run(step, kind, arguments, execute, **serialization)
