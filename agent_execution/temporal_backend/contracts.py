"""SDK-free, side-effect-free wire contracts; safe for Workflow sandbox imports."""
import re
import math

SCHEMA_VERSION = 1
WORKFLOW_NAME = 'PythonAIReadOnlyBridgeV1'
ACTIVITY_NAME = 'pythonai_readonly_bridge_v1'
STEP_WORKFLOW_NAME = 'AgentTaskWorkflowV1'
HUMAN_WORKFLOW_NAME = 'AgentTaskWorkflowV2'
STEP_ACTIVITIES = {kind: 'pythonai_step_' + kind + '_v1' for kind in
                   ('bootstrap', 'model', 'tool', 'evaluation', 'recover', 'finalize', 'human')}
HUMAN_SIGNAL = 'pythonai_human_decision_v1'
STEP_IO_MAX_ATTEMPTS = 1
STEP_METADATA_MAX_ATTEMPTS = 3
STEP_RECOVERY_LIMIT = 3
STEP_LIMIT = 1024
READ_TOOLS = frozenset({'read_file', 'list_files', 'fetch_url', 'git_status', 'git_diff',
                        'search_codebase', 'code_symbols', 'go_to_definition',
                        'find_references', 'workspace_info'})
STEP_TOOLS = READ_TOOLS | {'ask_user'}


def validate_envelope(value):
    if (not isinstance(value, dict) or set(value) != {'schema_version', 'submission_id'}
            or type(value['schema_version']) is not int or value['schema_version'] != SCHEMA_VERSION
            or not isinstance(value['submission_id'], str)
            or not re.fullmatch(r'[a-f0-9]{32}', value['submission_id'])):
        raise ValueError('Invalid Temporal reference envelope')
    return value['submission_id']


def validate_reference(value):
    if (not isinstance(value, dict) or set(value) != {'schema_version', 'id', 'sha256', 'bytes'}
            or type(value['schema_version']) is not int or value['schema_version'] != 1
            or not isinstance(value['id'], str) or not re.fullmatch('[a-f0-9]{32}', value['id'])
            or not isinstance(value['sha256'], str) or not re.fullmatch('[a-f0-9]{64}', value['sha256'])
            or type(value['bytes']) is not int or not 0 <= value['bytes'] <= 64*1024*1024):
        raise ValueError('Invalid state reference')
    return value


def validate_cursor(value):
    if (not isinstance(value, dict) or set(value) != {'schema_version', 'submission_id', 'revision', 'next_kind', 'state_ref'}
            or type(value['revision']) is not int or not 0 <= value['revision'] <= STEP_LIMIT
            or not isinstance(value['next_kind'], str) or value['next_kind'] not in {'model', 'tool', 'evaluation', 'finalize', 'human'}):
        raise ValueError('Invalid step cursor')
    validate_envelope({k: value[k] for k in ('schema_version', 'submission_id')})
    validate_reference(value['state_ref'])
    return value


def validate_snapshot(value):
    if (not isinstance(value, dict) or set(value) != {'schema_version', 'phase', 'checkpoint', 'task_state', 'reply'}
            or type(value['schema_version']) is not int or value['schema_version'] != 1
            or not isinstance(value['phase'], str) or value['phase'] not in {'temporal_pending', 'step_ready', 'step_preparing', 'model_pending', 'model_received',
                                      'tool_started', 'tool_done', 'evaluation_pending', 'evaluation_done', 'waiting_user'}
            or not isinstance(value['task_state'], str) or value['task_state'] not in {'pending', 'running', 'completed', 'failed', 'suspended'}
            or (value['reply'] is not None and not isinstance(value['reply'], str))
            or not isinstance(value['checkpoint'], dict)):
        raise ValueError('Invalid continuation snapshot')
    saved = value['checkpoint']
    if not isinstance(saved.get('messages'), list) or any(not isinstance(m, dict) for m in saved['messages']):
        raise ValueError('Invalid continuation messages')
    for name in ('iteration', 'eval_retries', 'evaluation_start'):
        if name in saved and (type(saved[name]) is not int or saved[name] < 0):
            raise ValueError('Invalid continuation counters')
    if 'iteration_limit' in saved and (type(saved['iteration_limit']) is not int or not 1 <= saved['iteration_limit'] <= 256):
        raise ValueError('Invalid continuation iteration limit')
    for name in ('provider_retry_limit', 'eval_retry_limit'):
        if name in saved and (type(saved[name]) is not int or not 0 <= saved[name] <= 256):
            raise ValueError('Invalid continuation retry limit')
    return value


def validate_budget_contract(value):
    if (not isinstance(value, dict)
            or set(value) != {'schema_version', 'root_ref', 'deadline', 'limits'}
            or type(value['schema_version']) is not int or value['schema_version'] != 1
            or not isinstance(value['root_ref'], str) or not re.fullmatch('[a-f0-9]{64}', value['root_ref'])
            or type(value['deadline']) not in {int, float} or not math.isfinite(value['deadline'])
            or value['deadline'] <= 0 or not isinstance(value['limits'], dict)):
        raise ValueError('Invalid root budget contract')
    limits = value['limits']
    if set(limits) != {'tokens', 'requests', 'children', 'depth', 'cost', 'output', 'rates'}:
        raise ValueError('Invalid root budget limits')
    for name in ('tokens', 'requests', 'children', 'depth', 'output'):
        if type(limits[name]) is not int or limits[name] < 0:
            raise ValueError('Invalid root budget limits')
    if (type(limits['cost']) not in {int, float} or not math.isfinite(limits['cost'])
            or limits['cost'] < 0 or not isinstance(limits['rates'], dict)):
        raise ValueError('Invalid root budget limits')
    for rate in limits['rates'].values():
        if not isinstance(rate, dict) or not {'input', 'output'} <= set(rate):
            raise ValueError('Invalid root budget rates')
        if any(type(rate[name]) not in {int, float} or not math.isfinite(rate[name]) or rate[name] < 0
               for name in ('input', 'output')):
            raise ValueError('Invalid root budget rates')
    return value
