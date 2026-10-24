"""Narrow recovery: a refused mkdir can be satisfied by verified directory creation."""
import json
from pathlib import Path


def recovered_directory_failure(step, steps, root):
    try:
        result = json.loads(step.get('result') or '{}')
        args = json.loads(step['input'])
        command = args.get('command', [])
        if (step['name'] != 'run_command' or step['state'] != 'failed' or
                result.get('evidence', {}).get('kind') not in {'unsupported_shell_builtin', 'repeat_prevented'} or
                result.get('side_effects', {}).get('status') != 'none' or len(command) != 2 or
                command[0].lower() not in {'mkdir', 'md'}):
            return False
        root = Path(root).resolve()
        target = (root / (args.get('cwd') or '.') / command[1]).resolve()
        if not target.is_relative_to(root) or not target.is_dir():
            return False
        for other in steps[steps.index(step) + 1:]:
            if other['name'] == 'create_directory' and other['state'] == 'completed':
                data = json.loads(other.get('result') or '{}')
                if data.get('success') is True and str(target) in json.loads(data['output']).get('directories', []):
                    return True
    except (ValueError, TypeError, KeyError, AttributeError):
        pass
    return False


def recovered_edit_failure(step, steps, root):
    try:
        failed = json.loads(step.get('result') or '{}')
        args = json.loads(step['input'])
        if step['name'] != 'edit_file' or step['state'] != 'failed' or failed.get('evidence', {}).get('kind') not in {'edit_no_match', 'repeat_prevented'} or failed.get('side_effects', {}).get('status') != 'none':
            return False
        root = Path(root).resolve()
        target = (root / args['path']).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            return False
        content = target.read_text(encoding='utf-8')
        for other in steps[steps.index(step) + 1:]:
            if other['name'] not in {'edit_file', 'write_file'} or other['state'] != 'completed':
                continue
            repair = json.loads(other['input'])
            result = json.loads(other.get('result') or '{}')
            if (root / repair['path']).resolve() != target or result.get('success') is not True:
                continue
            if other['name'] == 'write_file' and repair['content'] == content:
                return True
            if other['name'] == 'edit_file' and repair.get('new_str') and repair['new_str'] in content:
                return True
    except (ValueError, TypeError, KeyError, AttributeError, OSError):
        pass
    return False


def recovered_read_failure(step, steps, root=None):
    """A duplicate, side-effect-free read cannot invalidate existing evidence,
    and read-only web failures are recovered by subsequent successful sources.
    """
    try:
        failed = json.loads(step.get('result') or '{}')
        args = json.loads(step['input'])
        if (step['name'] not in {'browse_webpage', 'fetch_url'} or step['state'] != 'failed' or
                failed.get('side_effects', {}).get('status') not in {None, 'none'} or
                not isinstance(args.get('url'), str)):
            return False
        for other in steps:
            if other is step or other['name'] != step['name'] or other['state'] != 'completed':
                continue
            other_args = json.loads(other['input'])
            other_result = json.loads(other.get('result') or '{}')
            if other_args.get('url') == args['url'] and other_result.get('success') is True:
                return True
        idx = steps.index(step) if step in steps else -1
        if idx >= 0:
            for other in steps[idx + 1:]:
                if other['name'] not in {'browse_webpage', 'fetch_url'} or other['state'] != 'completed':
                    continue
                other_args = json.loads(other['input'])
                other_result = json.loads(other.get('result') or '{}')
                if (other_result.get('success') is True and
                        other_result.get('side_effects', {}).get('status') in {None, 'none'} and
                        isinstance(other_result.get('output'), str) and other_result['output'].strip() and
                        isinstance(other_args.get('url'), str) and other_args['url'].startswith(('http://', 'https://'))):
                    return True
    except (ValueError, TypeError, KeyError, AttributeError):
        pass
    return False
