"""Uncached, UTF-8 agent instructions. Configuration is trusted operator input."""
from copy import deepcopy
import json
import os
from pathlib import Path
import re
from string import Template

DEFAULT_DIRECTORY = Path(__file__).resolve().parent / 'data'
START = '[PythonAI dynamic instructions]'
END = '[/PythonAI dynamic instructions]'


class ConfigurationError(ValueError):
    pass


def directory():
    configured = os.environ.get('AGENT_CONFIG_DIR', '').strip()
    return Path(configured).expanduser().resolve() if configured else DEFAULT_DIRECTORY


def load_json(name):
    path = directory() / name
    try:
        return json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, UnicodeError, ValueError) as error:
        raise ConfigurationError(f'Cannot load agent configuration {path}: {error}') from error


def load_text(name, key=None, **values):
    if key is None:
        path = directory() / name
        try:
            text = path.read_text(encoding='utf-8-sig').strip()
        except (OSError, UnicodeError) as error:
            raise ConfigurationError(f'Cannot load agent configuration {path}: {error}') from error
    else:
        data = load_json(name)
        text = data.get(key) if isinstance(data, dict) else None
    if not isinstance(text, str) or not text.strip():
        raise ConfigurationError(f'Empty or invalid agent instruction: {name}:{key}')
    # Template substitution is literal data insertion; never evaluate Python or Jinja.
    if values:
        try:
            text = Template(text).substitute(values)
        except (KeyError, ValueError) as error:
            raise ConfigurationError(f'Invalid agent template: {name}:{key}: {error}') from error
    return text


def load_questions(name):
    data = load_json(name)
    if not isinstance(data, dict) or not data or any(
        not isinstance(value, dict) or not isinstance(value.get('instructions'), str)
        or not value['instructions'].strip() or not isinstance(value.get('type'), str)
        for value in data.values()
    ):
        raise ConfigurationError(f'Invalid question configuration: {name}')
    return data


def load_tool_text(key):
    name, path = key.split('/', 1)
    data = load_json('tool_descriptions.json')
    text = data.get(name, {}).get('/' + path)
    if not isinstance(text, str) or not text.strip():
        raise ConfigurationError(f'Missing tool description: {key}')
    return text


def instruction_block(agents_path=None, base_prompt=None):
    parts = [load_text('system.md') if base_prompt is None else base_prompt.strip(),
             load_text('tool_usage.md'), load_text('safety.md')]
    path = Path(agents_path) if agents_path is not None else Path(__file__).resolve().parents[1] / 'AGENTS.md'
    if path.exists():
        text = path.read_text(encoding='utf-8-sig').strip()
        if text:
            parts.append('AGENTS.md instructions:\n' + text)
    return START + '\n' + '\n\n'.join(part for part in parts if part) + '\n' + END


def refresh_instructions(messages):
    """Refresh managed policy while preserving personas, profile, history and call IDs."""
    block = instruction_block()
    pattern = re.compile(re.escape(START) + r'.*?' + re.escape(END), re.S)
    systems = [message for message in messages
               if message.get('role') == 'system' and isinstance(message.get('content'), str)]
    first = next((message for message in systems if pattern.search(message['content'])), None)
    if first is None:
        first = next((message for message in systems
                      if not message['content'].startswith('[PythonAI current user profile]\n')), None)
    for message in systems:
        if pattern.search(message['content']):
            message['content'] = pattern.sub('', message['content']).strip()
    if first is None:
        messages.insert(0, {'role': 'system', 'content': block})
        return True
    first['content'] = block + ('\n\n' + first['content'] if first['content'] else '')
    return False


def tool_schemas(registered):
    """Reload descriptions only; executable registrations and parameter contracts stay fixed."""
    core = load_json('tool_descriptions.json')
    if not isinstance(core, dict):
        raise ConfigurationError('Invalid tool description configuration')
    from base import CORE_TOOL_NAMES, DYNAMIC_TOOLS_DIR
    schemas = deepcopy(registered)

    def apply(node, descriptions, path=''):
        if not isinstance(node, dict):
            return
        for key, value in node.items():
            child = path + '/' + key
            if key == 'description':
                text = descriptions.get(child)
                if not isinstance(text, str) or not text.strip():
                    raise ConfigurationError(f'Missing tool description: {child}')
                node[key] = text
            elif isinstance(value, dict):
                apply(value, descriptions, child)

    def collect(node, path=''):
        result = {}
        if isinstance(node, dict):
            for key, value in node.items():
                child = path + '/' + key
                if key == 'description':
                    result[child] = value
                elif isinstance(value, dict):
                    result.update(collect(value, child))
        return result

    for schema in schemas:
        name = schema.get('function', schema).get('name')
        if name in CORE_TOOL_NAMES:
            if name not in core:
                raise ConfigurationError(f'Missing tool descriptions: {name}')
            if not isinstance(core[name], dict):
                raise ConfigurationError(f'Invalid tool descriptions: {name}')
            apply(schema.get('function', schema), core[name])
        else:
            # Builtin tools already have external JSON schemas. Re-read text without
            # importing/executing tool modules or activating generated extensions.
            path = DYNAMIC_TOOLS_DIR / f'{name}.json'
            if path.is_file():
                try:
                    data = json.loads(path.read_text(encoding='utf-8-sig'))
                    apply(schema.get('function', schema), collect(data.get('function', data)))
                except (OSError, UnicodeError, ValueError, AttributeError) as error:
                    raise ConfigurationError(f'Invalid tool schema text: {path}') from error
    return schemas
