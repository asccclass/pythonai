"""Offline contracts for editable policy and existing long-lived runtimes."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import agent_configuration as config


@pytest.fixture
def configured(tmp_path, monkeypatch):
    directory = tmp_path / 'configuration'
    shutil.copytree(config.DEFAULT_DIRECTORY, directory)
    monkeypatch.setenv('AGENT_CONFIG_DIR', str(directory))
    return directory


def edit_json(path, change):
    data = json.loads(path.read_text(encoding='utf-8'))
    change(data)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')


def test_initialization_reloads_policy_and_agents_from_other_cwd(configured, tmp_path, monkeypatch):
    import server
    agents = tmp_path / 'rules.md'
    agents.write_text('專案規則一', encoding='utf-8')
    monkeypatch.chdir(tmp_path)
    first = server.build_system_prompt(agents_path=agents)
    assert '專案規則一' in first
    assert config.load_text('system.md') in first
    (configured / 'system.md').write_text('新的系統提示詞', encoding='utf-8')
    (configured / 'safety.md').write_text('新的安全限制', encoding='utf-8')
    agents.write_text('專案規則二', encoding='utf-8')
    second = server.build_system_prompt(agents_path=agents)
    assert '新的系統提示詞' in second and '新的安全限制' in second and '專案規則二' in second
    assert '專案規則一' not in second


def test_refresh_preserves_history_persona_and_tool_protocol(configured):
    messages = [{'role': 'system', 'content': config.instruction_block() + '\nPersona: reviewer'},
                {'role': 'system', 'content': '[PythonAI current user profile]\nprivate profile'},
                {'role': 'user', 'content': 'original goal'},
                {'role': 'assistant', 'tool_calls': [{'id': 'call_1', 'type': 'function',
                    'function': {'name': 'read_file', 'arguments': '{}'}}]},
                {'role': 'tool', 'tool_call_id': 'call_1', 'content': 'evidence'}]
    saved = deepcopy(messages[1:])
    (configured / 'system.md').write_text('Current policy', encoding='utf-8')
    config.refresh_instructions(messages)
    config.refresh_instructions(messages)
    assert messages[1:] == saved
    assert messages[0]['content'].count(config.START) == 1
    assert 'Current policy' in messages[0]['content'] and 'Persona: reviewer' in messages[0]['content']


def test_refresh_keeps_policy_in_its_own_message_after_skill_context_is_added(configured):
    guidance = {'role': 'system', 'content': '  Current skill guidance\n'}
    main = {'role': 'system', 'content': config.instruction_block()}
    profile = {'role': 'system', 'content': '[PythonAI current user profile]\nprivate profile\n'}
    messages = [guidance, profile, main]
    saved = deepcopy(messages[:2])
    config.refresh_instructions(messages)
    assert messages[:2] == saved
    assert config.START in main['content']


def test_refresh_never_hides_the_profile_message_marker(configured):
    profile = {'role': 'system', 'content': '[PythonAI current user profile]\nprofile'}
    messages = [profile, {'role': 'user', 'content': 'goal'}]
    assert config.refresh_instructions(messages) is True
    assert messages[1] is profile
    assert profile['content'].startswith('[PythonAI current user profile]\n')


@pytest.mark.parametrize('kind', ['missing', 'empty', 'encoding'])
def test_invalid_policy_aborts_without_mutating_history(configured, kind):
    path = configured / 'safety.md'
    if kind == 'missing':
        path.unlink()
    elif kind == 'empty':
        path.write_text('  ', encoding='utf-8')
    else:
        path.write_bytes(b'\xff')
    messages = [{'role': 'system', 'content': 'keep'}, {'role': 'user', 'content': 'goal'}]
    original = deepcopy(messages)
    with pytest.raises(config.ConfigurationError, match='safety.md'):
        config.refresh_instructions(messages)
    assert messages == original


def test_json_validation_and_literal_template_substitution(configured):
    path = configured / 'custom.json'
    path.write_text(json.dumps({'template': 'Result: ${result}'}), encoding='utf-8')
    assert config.load_text('custom.json', 'template', result='${secret} {code}') == 'Result: ${secret} {code}'
    with pytest.raises(config.ConfigurationError):
        config.load_text('custom.json', 'missing')
    path.write_text('{broken', encoding='utf-8')
    with pytest.raises(config.ConfigurationError):
        config.load_json('custom.json')


def test_core_and_builtin_descriptions_reload_without_changing_contracts(configured, tmp_path):
    import base
    schemas = deepcopy([s for s in base.TOOLS_SCHEMAS if s['function']['name'] in {'read_file', 'desktop_tool'}])
    original = deepcopy(schemas)
    edit_json(configured / 'tool_descriptions.json', lambda data: data['read_file'].update({
        '/description': 'Read with revised guidance', '/parameters/properties/path/description': 'Updated path guidance'}))
    function = config.tool_schemas(schemas)[0]['function']
    assert function['description'] == 'Read with revised guidance'
    assert function['parameters']['properties']['path']['description'] == 'Updated path guidance'
    assert schemas == original
    # A changed file cannot register executable tools or alter approved argument types.
    tool_dir = tmp_path / 'tools'
    tool_dir.mkdir()
    schema = {'type': 'function', 'function': {'name': 'external', 'description': 'old',
              'parameters': {'type': 'object', 'properties': {'count': {'type': 'integer', 'description': 'old count'}}}}}
    external = deepcopy(schema)
    external['function']['description'] = 'new'
    external['function']['parameters']['properties']['count'] = {'type': 'string', 'description': 'new count'}
    (tool_dir / 'external.json').write_text(json.dumps(external), encoding='utf-8')
    with patch.object(base, 'DYNAMIC_TOOLS_DIR', tool_dir):
        refreshed = config.tool_schemas([schema])[0]
    assert refreshed['function']['description'] == 'new'
    assert refreshed['function']['parameters']['properties']['count'] == {'type': 'integer', 'description': 'new count'}


@pytest.mark.parametrize('remove_tool', [False, True])
def test_missing_tool_description_is_an_error(configured, remove_tool):
    import base
    schema = next(s for s in base.TOOLS_SCHEMAS if s['function']['name'] == 'read_file')
    edit_json(configured / 'tool_descriptions.json',
              lambda data: data.pop('read_file') if remove_tool else data['read_file'].pop('/description'))
    with pytest.raises(config.ConfigurationError):
        config.tool_schemas([schema])


def test_existing_guard_reloads_questions_on_each_assessment(configured):
    from laya_guard import LayaGuard
    captured = []
    guard = LayaGuard.__new__(LayaGuard)
    guard._agent = SimpleNamespace(predict=lambda state, questions: captured.append(questions) or {'answers': {}})
    guard.assess('first')
    edit_json(configured / 'guard_questions.json', lambda data: data['risk'].update(instructions='新的風險判斷'))
    guard.assess('second')
    assert captured[0]['risk']['instructions'] != captured[1]['risk']['instructions']
    assert captured[1]['risk']['instructions'] == '新的風險判斷'


def test_invalid_guard_configuration_aborts_before_prediction(configured):
    from laya_guard import LayaGuard
    guard = LayaGuard.__new__(LayaGuard)
    guard._agent = None
    (configured / 'guard_questions.json').write_text('{broken', encoding='utf-8')
    with pytest.raises(config.ConfigurationError):
        guard.assess('run something')


def test_turn_entry_refreshes_even_when_no_model_call_is_needed(configured):
    from agent_runtime import run_agent_turn
    runtime = SimpleNamespace(messages=[{'role': 'system', 'content': config.instruction_block()}], profile_extractor=None)
    (configured / 'system.md').write_text('Reload before any turn work', encoding='utf-8')
    with patch('agent_execution.workspace_lifecycle.LifecycleStore'), \
         patch('agent_execution.user_profile.prepare_turn', return_value=('handled', '')):
        result = run_agent_turn('/profile help', runtime)
    assert result.reply == 'handled'
    assert 'Reload before any turn work' in runtime.messages[0]['content']


def test_existing_server_agent_reloads_policy_and_tools_per_task(configured):
    import server
    from openai.types.chat import ChatCompletionMessage
    captured = []
    def model_step(client, request, messages, **kwargs):
        captured.append((deepcopy(messages), deepcopy(request)))
        return ChatCompletionMessage(role='assistant', content='done')
    messages = [{'role': 'system', 'content': server.build_system_prompt()}, {'role': 'user', 'content': 'first'}]
    with patch('server.get_client', return_value=object()), patch('agent_execution.turn_control.steps.model_step', side_effect=model_step):
        server.run_agent(messages, max_iterations=4)
        (configured / 'system.md').write_text('Second task policy', encoding='utf-8')
        edit_json(configured / 'tool_descriptions.json', lambda data: data['read_file'].update({'/description': 'Second task tool guidance'}))
        messages.append({'role': 'user', 'content': 'second'})
        server.run_agent(messages, max_iterations=4)
    assert 'Second task policy' not in captured[0][0][0]['content']
    assert 'Second task policy' in captured[1][0][0]['content']
    read = next(s for s in captured[1][1]['tools'] if s['function']['name'] == 'read_file')
    assert read['function']['description'] == 'Second task tool guidance'
    assert any(m.get('content') == 'first' for m in captured[1][0])


def test_auxiliary_prompt_reloads_in_existing_extractor(configured):
    from semantic_extractor import LLMSemanticExtractor
    captured = []
    def create(**kwargs):
        captured.append(kwargs['messages'][0]['content'])
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='[]'))])
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    extractor = LLMSemanticExtractor(lambda: client, 'offline-model')
    extractor.extract('first')
    (configured / 'semantic_extraction.md').write_text('Revised extraction rules', encoding='utf-8')
    extractor.extract('second')
    assert captured[0] != captured[1] == 'Revised extraction rules'


def test_all_external_assets_parse_and_templates_match_call_sites():
    import ast
    with patch.dict('os.environ', {'AGENT_CONFIG_DIR': str(config.DEFAULT_DIRECTORY)}):
        for path in config.DEFAULT_DIRECTORY.glob('*.json'):
            assert config.load_json(path.name)
        # Migration guard: every text catalog key referenced by live source exists.
        files = ['server.py', 'base.py', 'retrieval.py', 'task_evaluator.py', 'summary.py', 'agent_runtime.py',
                 'agent_execution/ide/server.py', 'agent_execution/user_profile/__init__.py',
                 'agent_execution/turn_control/__init__.py']
        used = set()
        for file in files:
            tree = ast.parse((config.DEFAULT_DIRECTORY.parents[1] / file).read_text(encoding='utf-8'))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'load_text' and len(node.args) >= 2:
                    name, key = node.args[:2]
                    if isinstance(name, ast.Constant) and name.value == 'instructions.json' and isinstance(key, ast.Constant):
                        values = {kw.arg: 'literal test data' for kw in node.keywords}
                        assert config.load_text(name.value, key.value, **values)
                        used.add(key.value)
        assert used == set(config.load_json('instructions.json'))
