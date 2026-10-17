"""SDK-free, side-effect-free wire contracts; safe for Workflow sandbox imports."""
import re

SCHEMA_VERSION = 1
WORKFLOW_NAME = 'PythonAIReadOnlyBridgeV1'
ACTIVITY_NAME = 'pythonai_readonly_bridge_v1'
READ_TOOLS = frozenset({'read_file', 'list_files', 'fetch_url', 'git_status', 'git_diff',
                        'search_codebase', 'code_symbols', 'go_to_definition',
                        'find_references', 'workspace_info'})


def validate_envelope(value):
    if (not isinstance(value, dict) or set(value) != {'schema_version', 'submission_id'}
            or type(value['schema_version']) is not int or value['schema_version'] != SCHEMA_VERSION
            or not isinstance(value['submission_id'], str)
            or not re.fullmatch(r'[a-f0-9]{32}', value['submission_id'])):
        raise ValueError('Invalid Temporal reference envelope')
    return value['submission_id']
