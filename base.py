from agent_configuration import load_text, load_tool_text
from memory_tools import manage_memory
import os
import json
import platform
import subprocess
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from laya_guard import GuardDecision, LayaGuard
from skills import SkillExecutor, SkillRegistry
from agent_execution import policy_tool
from agent_execution.process import run_process, execution_mode


_approved_commands: set[tuple[tuple[str, ...], str | None]] = set()
_command_guard: LayaGuard | None = None
_auto_approve_commands: ContextVar[bool] = ContextVar("auto_approve_commands", default=False)


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_WORKSPACE_DIR = PROJECT_ROOT / "workspace"


def workspace_root() -> Path:
    from agent_execution.policy import current_context
    scoped = current_context().workspace
    if scoped is not None:
        resolved = Path(scoped).expanduser().resolve()
        resolved.mkdir(parents=True, exist_ok=True)
        return resolved
    configured = os.environ.get("AGENT_WORKSPACE_ROOT", "").strip()
    root = Path(configured).expanduser() if configured else DEFAULT_WORKSPACE_DIR
    resolved = root.resolve()
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def workspace_error(path: str | Path, root: Path) -> str:
    return f"Path is outside Agent workspace ({root}): {path}"


def path_is_inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def resolve_workspace_path(path: str | Path) -> Path:
    root = workspace_root()
    target = Path(path).expanduser()
    resolved = (root / target).resolve() if not target.is_absolute() else target.resolve()
    if '.pythonai-workspace-trash' in resolved.parts:
        raise PermissionError('Workspace archive storage is service-only')
    if not path_is_inside(resolved, root):
        raise PermissionError(workspace_error(path, root))
    return resolved


def trusted_roots(paths: list[str | Path] | None = None) -> list[Path]:
    return [Path(path).expanduser().resolve() for path in paths or []]


def resolve_trusted_asset_path(path: str | Path, trusted_asset_roots: list[str | Path] | None = None) -> Path:
    target = Path(path).expanduser()
    resolved = target.resolve()
    for root in trusted_roots(trusted_asset_roots):
        if path_is_inside(resolved, root):
            return resolved
    raise PermissionError(workspace_error(path, workspace_root()))


def resolve_workspace_or_trusted_path(
    path: str | Path,
    trusted_asset_roots: list[str | Path] | None = None,
) -> Path:
    if trusted_asset_roots:
        try:
            return resolve_trusted_asset_path(path, trusted_asset_roots)
        except PermissionError:
            pass
    try:
        return resolve_workspace_path(path)
    except PermissionError:
        return resolve_trusted_asset_path(path, trusted_asset_roots)


def validate_command_paths(command: list[str], trusted_asset_roots: list[str | Path] | None = None) -> None:
    for index, item in enumerate(command):
        if not isinstance(item, str):
            continue
        if index == 1 and command[0].lower() in {"cmd", "cmd.exe"} and item.lower() in {"/c", "/k"}:
            continue
        value = item.split("=", 1)[1] if item.startswith("-") and "=" in item else item
        if looks_like_path_argument(value):
            resolve_workspace_or_trusted_path(value, trusted_asset_roots)


def looks_like_path_argument(value: str) -> bool:
    if "://" in value:
        return False
    path = Path(value)
    return path.is_absolute() or value.startswith(("..", ".", "~")) or "/" in value or "\\" in value


def read_file(path: str | Path) -> str:
    try:
        target=resolve_workspace_path(path)
        from agent_execution.workspace_lifecycle import LifecycleStore
        LifecycleStore().assert_access(target)
        with open(target, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        print(f"File not found: {path}")
    except Exception as e:
        print(f"Error reading file: {e}")
    return ""


def list_files(path: str | Path = ".") -> list[str]:
    target=resolve_workspace_path(path)
    from agent_execution.workspace_lifecycle import LifecycleStore
    LifecycleStore().assert_access(target)
    blocked=LifecycleStore().inaccessible_paths()
    return [item.name for item in target.iterdir() if not any(item.resolve()==p or item.resolve().is_relative_to(p) for p in blocked)]


def write_file(path: str | Path, content: str) -> dict:
    target = resolve_workspace_path(path)
    from agent_execution.companion.outputs import assert_writable, register
    assert_writable(target)
    newly_created = not target.exists()
    from agent_execution.workspace_lifecycle import prepare_write,program_build,LifecycleStore
    from contextlib import nullcontext
    key=prepare_write(target)
    with program_build(LifecycleStore(),key) if key else nullcontext():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    output = register(target, newly_created)
    return {'success': True, 'path': str(target), 'action': 'written',
            **({'artifacts': [output], 'evidence': {'kind': 'workspace_output', **output}} if output else {})}

def edit_file(path: str | Path, old_str: str, new_str: str) -> dict[str, str]:
    target = resolve_workspace_path(path)
    from agent_execution.workspace_lifecycle import prepare_write,program_build,LifecycleStore
    if not target.exists() and old_str!='':return {"success":False,"error":"file not found; read_file before editing", "path":str(target),"action":"file not found", "side_effects":{"status":"none"},"evidence":{"kind":"edit_no_match"}}
    key=prepare_write(target)
    if not target.exists():
        if old_str == "":
            from contextlib import nullcontext
            with program_build(LifecycleStore(),key) if key else nullcontext():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(new_str, encoding="utf-8")
            return {"path": str(target), "action": "created_file"}
        else:
            return {"path": str(target), "action": "file not found"}
    
    original = target.read_text(encoding="utf-8")
    if original.find(old_str) == -1:
        return {"success": False, "error": "old_str not found; read_file and use the exact current text", "path": str(target), "action": "old_str not found", "side_effects":{"status":"none"},"evidence":{"kind":"edit_no_match"}}
    
    edited = original.replace(old_str, new_str, 1)
    target.write_text(edited, encoding="utf-8")
    return {"path": str(target), "action": "edited"}


def subprocess_text_options() -> dict:
    return {
        "capture_output": True,
        "check": False,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
    }


def delete_file(path: str | Path) -> subprocess.CompletedProcess[str]:
    try:
        target = resolve_workspace_path(path)
        from agent_execution.workspace_lifecycle import LifecycleStore
        LifecycleStore().assert_writable(target)
    except PermissionError as error:
        return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr=str(error))
    if target.is_dir():
        return subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr=f"Refusing to delete directory: {target}",
        )

    try:
        target.unlink(missing_ok=True)
        return subprocess.CompletedProcess(args=["delete_file", str(target)], returncode=0, stdout="", stderr="")
    except OSError as error:
        return subprocess.CompletedProcess(args=["delete_file", str(target)], returncode=1, stdout="", stderr=str(error))


def curl_command(url: str, max_time: int = 20, follow_redirects: bool = True) -> list[str]:
    executable = "curl.exe" if platform.system() == "Windows" else "curl"
    command = [
        executable,
        "--silent",
        "--show-error",
    ]
    if follow_redirects:
        command.append("--location")
    command.extend(["--max-time", str(max_time), url])
    return command


def fetch_url(url: str, max_time: int = 20, follow_redirects: bool = True, raw: bool = False,
              contains: str = '') -> subprocess.CompletedProcess[str]:
    from agent_execution.turn_control.web import fetch_target, readable_content
    from urllib.parse import urlsplit
    if urlsplit(url).scheme not in {'http', 'https'}:
        return subprocess.CompletedProcess([], 1, '', 'fetch_url requires an HTTP(S) URL')
    if not isinstance(contains, str) or len(contains) > 256 or any(ord(char) < 32 for char in contains):
        return subprocess.CompletedProcess([], 1, '', 'fetch_url contains must be at most 256 printable characters')
    target = url if raw else fetch_target(url)
    command = curl_command(target, max_time=max_time, follow_redirects=follow_redirects)
    command[1:1] = ['--fail']
    if target != url:
        command[1:1] = ['--header', 'Accept: application/vnd.github.raw+json']
    result = run_process(command, **subprocess_text_options())
    if result.returncode == 0 and not raw:
        result.stdout = readable_content(result.stdout or '', url, contains=contains)
        source = f'Source: {url}\n' + (f'README: {target}\n' if target != url else '')
        result.stdout = source + load_text('instructions.json', 'untrusted_web') + result.stdout
    return result


def get_command_guard() -> LayaGuard:
    global _command_guard
    if _command_guard is None:
        _command_guard = LayaGuard()
    return _command_guard


def command_key(
    command: list[str],
    cwd: str | Path | None = None,
    env_file: str | Path | None = None,
) -> tuple[tuple[str, ...], str | None, str | None]:
    return tuple(command), str(cwd) if cwd is not None else None, str(env_file) if env_file is not None else None


@contextmanager
def auto_approve_command_runs():
    token = _auto_approve_commands.set(True)
    try:
        yield
    finally:
        _auto_approve_commands.reset(token)


def run_command(
    command: list[str],
    cwd: str | Path | None = None,
    env_file: str | Path | None = None,
    trusted_asset_roots: list[str | Path] | None = None,
    artifacts_directory: str | Path | None = None,
    program_directory: str | None = None,
) -> subprocess.CompletedProcess[str]:
    if program_directory is not None:
        if cwd not in (None,'.'):raise ValueError('Use program_directory as the command cwd')
        from agent_execution.workspace_lifecycle import program_directory as managed_program
        with managed_program(program_directory) as outcome:
            result=run_command(command,None,env_file,trusted_asset_roots,artifacts_directory)
            outcome['success']=result.returncode==0
            return result
    try:
        resolved_cwd = resolve_command_cwd(cwd, trusted_asset_roots)
        resolved_env_file = (
            resolve_workspace_or_trusted_path(env_file, trusted_asset_roots) if env_file is not None else None
        )
        validate_command_paths(command, trusted_asset_roots)
        from agent_execution.turn_control.environment import command_environment_error
        environment_error = command_environment_error(command)
        if environment_error:
            result = subprocess.CompletedProcess(command, 1, '', environment_error)
            result.side_effects = {'status':'none'}
            result.evidence = {'kind':'unsupported_shell_builtin' if command[0].lower() in {'mkdir','md','dir','type'} else 'environment_mismatch'}
            return result
        resolved_command = resolve_command_executable(command, resolved_cwd, trusted_asset_roots)
    except PermissionError as error:
        return subprocess.CompletedProcess(args=command, returncode=1, stdout="", stderr=str(error))
    key = command_key(resolved_command, resolved_cwd, resolved_env_file)
    if _auto_approve_commands.get():
        _approved_commands.add(key)
    elif key not in _approved_commands:
        decision = get_command_guard().assess_command(resolved_command, resolved_cwd)
        if decision.needs_confirmation or not decision.available:
            answer = input(f" Run '{resolved_command}'? [y/N]: ")
            if answer.lower() != "y":
                return subprocess.CompletedProcess(
                    args=resolved_command,
                    returncode=1,
                    stdout="",
                    stderr="User cancelled",
                )
        _approved_commands.add(key)
    try:
        env = command_environment(resolved_env_file)
    except Exception as error:
        return subprocess.CompletedProcess(args=resolved_command, returncode=1, stdout="", stderr=str(error))
    run_options = subprocess_text_options()
    if env is not None:
        run_options["env"] = env
    mode = execution_mode()
    print(f"[Execution] mode={mode}")
    if mode == "container":
        from sandbox import SandboxExecution
        sandbox = SandboxExecution(workspace_root())
        try:
            sandbox.setup()
            result = sandbox.run(resolved_command, cwd=resolved_cwd, **run_options)
            if result.returncode == 0 and artifacts_directory is not None:
                destination = resolve_workspace_path(artifacts_directory)
                sandbox.export_artifacts(destination)
                result.stderr += f"\nArtifacts exported to {destination}"
            return result
        except Exception as error:
            return subprocess.CompletedProcess(args=resolved_command, returncode=1, stdout="", stderr=str(error))
        finally:
            sandbox.cleanup()
    if artifacts_directory is not None:
        return subprocess.CompletedProcess(args=resolved_command, returncode=1, stdout="", stderr="artifacts_directory requires container mode")
    return run_process(resolved_command, cwd=resolved_cwd, **run_options)


def resolve_command_cwd(
    cwd: str | Path | None = None,
    trusted_asset_roots: list[str | Path] | None = None,
) -> Path:
    if cwd is None:
        return workspace_root()
    return resolve_workspace_or_trusted_path(cwd, trusted_asset_roots)


def resolve_command_executable(
    command: list[str],
    cwd: str | Path | None = None,
    trusted_asset_roots: list[str | Path] | None = None,
) -> list[str]:
    if not command:
        return command
    executable = command[0]
    if not isinstance(executable, str) or not looks_like_path_argument(executable):
        return command
    executable_path = Path(executable)
    if executable_path.is_absolute():
        return command
    base = Path(cwd) if cwd is not None else Path.cwd()
    resolved = (base / executable_path).resolve()
    resolve_workspace_or_trusted_path(resolved, trusted_asset_roots)
    return [str(resolved), *command[1:]]


@policy_tool("run_skill")
def run_skill(name: str, inputs: dict | None = None, memory=None, episode_id: int | None = None, program_directory: str | None = None) -> dict:
    if program_directory is not None:
        from agent_execution.workspace_lifecycle import program_directory as managed_program
        with managed_program(program_directory) as outcome:
            result=run_skill(name,inputs,memory,episode_id)
            outcome['success']=result.get('success') is True
            return result
    skill = SkillRegistry().get(name)
    result = SkillExecutor(TOOLS, memory=memory, episode_id=episode_id).execute(skill, inputs or {})
    return result.to_dict()


def load_dotenv(path: str | Path = ".env") -> None:
    """Load simple KEY=value pairs into the process environment."""

    env_path = Path(path)
    if not env_path.exists():
        return

    for key, value in parse_env_lines(env_path.read_text(encoding="utf-8").splitlines()).items():
        if key and key not in os.environ:
            os.environ[key] = value


def command_environment(env_file: str | Path | None = None) -> dict[str, str] | None:
    if env_file is None:
        return None
    env_path = Path(env_file)
    values = parse_env_lines(env_path.read_text(encoding="utf-8").splitlines())
    env = os.environ.copy()
    env.update(values)
    return env


def parse_env_lines(lines) -> dict[str, str]:
    values = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def ask_user(question: str) -> str:
    if not isinstance(question, str) or not question.strip() or len(question) > 2000:
        raise ValueError('ask_user question must contain 1..2000 characters')
    return question


TOOLS = {
    'ask_user': ask_user,
    "read_file": read_file,
    "list_files": list_files,
    "write_file": write_file,
    "delete_file": delete_file,
    "fetch_url": fetch_url,
    "run_command": run_command,
    "run_skill": run_skill,
    "manage_memory": manage_memory,
}


def run_tool(tool_call):
    return run_tool_with_context(tool_call)


def run_tool_with_context(tool_call, memory=None, episode_id: int | None = None):
    from agent_execution import execute_tool, execution_scope
    name = tool_call.function.name
    try:
        args = json.loads(tool_call.function.arguments)
        if not isinstance(args, dict):
            raise ValueError("Tool arguments must be an object")
        # These fields are controlled by the runtime, never by the model.
        for field in ("trusted_asset_roots", "memory", "episode_id"):
            args.pop(field, None)
        if name in ("run_skill", "manage_memory"):
            args.update(memory=memory, episode_id=episode_id)
        if name.startswith("mcp_"):
            from mcp_manager import execute_mcp_tool
            function = lambda **values: execute_mcp_tool(name, values)
        else:
            function = TOOLS.get(name)
        if function is None:
            return f"Error: Tool '{name}' not found. Choose a tool from the supplied schemas (fetch_url for URLs, web_research for search when available); do not repeat an unavailable tool name."
        with execution_scope(memory=memory, episode_id=episode_id):
            output=execute_tool(name, args, function)
            if function is read_file:
                from agent_execution.results import ToolResult
                # File content may itself begin with Error:; it is data, not a failure.
                return ToolResult(True,str(output))
            return output
    except Exception as error:
        from agent_execution.temporal_backend.operations import _tool_operation
        from agent_execution.temporal_backend.contracts import FILE_WRITE_TOOLS
        if name in FILE_WRITE_TOOLS and _tool_operation.get() is not None:
            raise  # Unknown protected writes must reach the ledger; never become retryable text.
        return f"Error: {error}"


TOOLS_SCHEMAS = [
    {'type':'function','function':{'name':'ask_user',
        'description':load_tool_text('ask_user/description'),
        'parameters':{'type':'object','properties':{'question':{'type':'string'}},'required':['question'],'additionalProperties':False}}},
    {
        "type": "function",
        "function": {
                "name": "read_file",
                "description": load_tool_text('read_file/description'),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": load_tool_text('read_file/parameters/properties/path/description')
                        }
                    },
                    "required": ["path"]
                }
        }
    }
    ,
    {
        "type": "function",
        "function": {
                "name": "list_files",
                "description": load_tool_text('list_files/description'),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": load_tool_text('list_files/parameters/properties/path/description')
                        }
                    }
                }
        }
    },
    {
        "type": "function",
        "function": {
                "name": "write_file",
                "description": load_tool_text('write_file/description'),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": load_tool_text('write_file/parameters/properties/path/description')
                        },
                        "content": {
                            "type": "string",
                            "description": load_tool_text('write_file/parameters/properties/content/description')
                        }
                    },
                    "required": ["path", "content"]
                }
        }
    },
    {
        "type": "function",
        "function": {
                "name": "delete_file",
                "description": load_tool_text('delete_file/description'),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": load_tool_text('delete_file/parameters/properties/path/description')
                        }
                    },
                    "required": ["path"]
                }
        }
    },
    {
        "type": "function",
        "function": {
                "name": "fetch_url",
                "description": load_tool_text('fetch_url/description'),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": load_tool_text('fetch_url/parameters/properties/url/description')
                        },
                        "raw": {"type": "boolean", "description": load_tool_text('fetch_url/parameters/properties/raw/description')},
                        "contains": {"type": "string", "description": load_tool_text('fetch_url/parameters/properties/contains/description')},
                        "max_time": {
                            "type": "integer",
                            "description": load_tool_text('fetch_url/parameters/properties/max_time/description')
                        },
                        "follow_redirects": {
                            "type": "boolean",
                            "description": load_tool_text('fetch_url/parameters/properties/follow_redirects/description')
                        }
                    },
                    "required": ["url"]
                }
        }
    },
    {
        "type": "function",
        "function": {
                "name": "run_command",
                "description": load_tool_text('run_command/description'),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": load_tool_text('run_command/parameters/properties/command/description')
                        },
                        "cwd": {
                            "type": "string",
                            "description": load_tool_text('run_command/parameters/properties/cwd/description')
                        },
                        "artifacts_directory": {
                            "type": "string",
                            "description": load_tool_text('run_command/parameters/properties/artifacts_directory/description')
                        },
                        "program_directory": {"type":"string","description":load_tool_text('run_command/parameters/properties/program_directory/description')}
                    },
                    "required": ["command"]
                }
        }
    },
    {
        "type": "function",
        "function": {
                "name": "run_skill",
                "description": load_tool_text('run_skill/description'),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": load_tool_text('run_skill/parameters/properties/name/description')
                        },
                        "inputs": {
                            "type": "object",
                            "description": load_tool_text('run_skill/parameters/properties/inputs/description')
                        },
                        "program_directory": {"type":"string","description":load_tool_text('run_skill/parameters/properties/program_directory/description')}
                    },
                    "required": ["name"]
                }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "manage_memory",
            "description": load_tool_text('manage_memory/description'),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": load_tool_text('manage_memory/parameters/properties/action/description'),
                        "enum": [
                            "confirm",
                            "archive",
                            "contradict",
                            "supersede"
                        ]
                    },
                    "memory_id": {
                        "type": "integer",
                        "description": load_tool_text('manage_memory/parameters/properties/memory_id/description')
                    },
                    "confirm": {
                        "type": "boolean",
                        "description": load_tool_text('manage_memory/parameters/properties/confirm/description')
                    },
                    "approval_id": {"type": "string", "description": load_tool_text('manage_memory/parameters/properties/approval_id/description')},
                    "reason": {
                        "type": "string",
                        "description": load_tool_text('manage_memory/parameters/properties/reason/description')
                    },
                    "superseded_by": {
                        "type": "integer",
                        "description": load_tool_text('manage_memory/parameters/properties/superseded_by/description')
                    }
                },
                "required": [
                    "action",
                    "memory_id"
                ]
            }
        }
    },
    {
        "type": "function",
        "function": {
                "name": "edit_file",
                "description": load_tool_text('edit_file/description'),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": load_tool_text('edit_file/parameters/properties/path/description')
                        },
                        "old_str": {
                            "type": "string",
                            "description": load_tool_text('edit_file/parameters/properties/old_str/description')
                        },
                        "new_str": {
                            "type": "string",
                            "description": load_tool_text('edit_file/parameters/properties/new_str/description')
                        }
                    },
                    "required": ["path", "old_str", "new_str"]
                }
        }
    },
    {
        "type": "function",
        "function": {
                "name": "search_web",
                "description": load_tool_text('search_web/description'),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": load_tool_text('search_web/parameters/properties/query/description')
                        },
                        "max_results": {
                            "type": "integer",
                            "description": load_tool_text('search_web/parameters/properties/max_results/description')
                        }
                    },
                    "required": ["query"]
                }
        }
    },
    {
        "type": "function",
        "function": {
                "name": "browse_webpage",
                "description": load_tool_text('browse_webpage/description'),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": load_tool_text('browse_webpage/parameters/properties/url/description')
                        }
                    },
                    "required": ["url"]
                }
        }
    },
    {
        "type": "function",
        "function": {
                "name": "analyze_image",
                "description": load_tool_text('analyze_image/description'),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "image_path": {
                            "type": "string",
                            "description": load_tool_text('analyze_image/parameters/properties/image_path/description')
                        },
                        "prompt": {
                            "type": "string",
                            "description": load_tool_text('analyze_image/parameters/properties/prompt/description')
                        }
                    },
                    "required": ["image_path"]
                }
        }
    },
    {
        "type": "function",
        "function": {
                "name": "create_dynamic_tool",
                "description": load_tool_text('create_dynamic_tool/description'),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": load_tool_text('create_dynamic_tool/parameters/properties/name/description')
                        },
                        "code": {
                            "type": "string",
                            "description": load_tool_text('create_dynamic_tool/parameters/properties/code/description')
                        },
                        "schema_json": {
                            "type": "string",
                            "description": load_tool_text('create_dynamic_tool/parameters/properties/schema_json/description')
                        }
                    },
                    "required": ["name", "code", "schema_json"]
                }
        }
    }
]

def search_web(query: str, max_results: int = 5):
    """Search the public web once; preserve provider failures as execution facts."""
    from agent_execution.results import ToolResult
    evidence = {'kind': 'duckduckgo_search', 'provider': 'duckduckgo.com'}
    try:
        from ddgs import DDGS
        from ddgs.exceptions import RatelimitException
    except ImportError as e:
        return ToolResult(False, error=f'Search unavailable: {type(e).__name__}: {e}',
                          side_effects={'status': 'none'}, evidence=evidence)

    try:
        import json
        results = DDGS(timeout=15).text(query, max_results=max_results)
        formatted = [{
            'title': str(item.get('title') or ''),
            'href': str(item.get('href') or item.get('url') or ''),
            'body': str(item.get('body') or item.get('snippet') or ''),
        } for item in results if isinstance(item, dict)]
        return ToolResult(True, json.dumps(formatted, ensure_ascii=False, indent=2),
                          side_effects={'status': 'none'},
                          evidence=evidence)
    except RatelimitException as e:
        return ToolResult(False, error=f'Search failed: {type(e).__name__}: {e}',
                          side_effects={'status': 'none'},
                          evidence={**evidence, 'http_status': 429})
    except Exception as e:
        return ToolResult(False, error=f'Search failed: {type(e).__name__}: {e}',
                          side_effects={'status': 'none'},
                          evidence=evidence)

def browse_webpage(url: str):
    """Return rendered page text, with browser failures represented structurally.

    This function is advertised in ``TOOLS_SCHEMAS``.  Returning ``ToolResult``
    on failure is important: a plain ``"Browsing failed: ..."`` string is
    otherwise interpreted as a successful tool result by ``normalize_result``.
    """
    from urllib.parse import urlsplit
    from agent_execution.results import ToolResult
    if urlsplit(url).scheme not in {'http', 'https'}:
        return ToolResult(False, error='browse_webpage requires an HTTP(S) URL',
                          side_effects={'status': 'none'})
    try:
        from agent_execution.media.agent_browser import read_public_page
        content = read_public_page(url, timeout=15)
        return ToolResult(True, str(content)[:15000],
                              evidence={'kind': 'agent_browser_read', 'url': url})
    except Exception as e:
        return ToolResult(False, error=f'Browsing failed: {e}',
                          side_effects={'status': 'none'},
                          evidence={'kind': 'rendered_webpage', 'url': url})


# Keep every advertised tool executable by the runtime.  This registration is
# intentionally here because the browser implementation is defined below the
# initial tool map.
TOOLS['browse_webpage'] = browse_webpage

def analyze_image(image_path: str | Path, prompt: str = "Describe this image in detail.") -> str:
    try:
        from server import get_client, OLLAMA_MODEL
        import base64
        target = resolve_workspace_path(image_path)
        if not target.exists(): return f"Image not found at: {image_path}"
        with open(target, "rb") as f:
            encoded = base64.b64encode(f.read()).decode("utf-8")
        ext = target.suffix.lower()
        mime = "image/jpeg"
        if ext in [".png"]: mime = "image/png"
        elif ext in [".webp"]: mime = "image/webp"
        elif ext in [".gif"]: mime = "image/gif"
        response = get_client().chat.completions.create(
            model=OLLAMA_MODEL,
            messages=[{"role": "user", "content": [{"type": "text", "text": prompt}, {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}}]}]
        )
        return response.choices[0].message.content
    except Exception as e:
        return f"Vision analysis failed: {e}"

from agent_execution.companion.desktop import desktop_tool, SCHEMA as DESKTOP_SCHEMA
TOOLS['desktop_tool'] = desktop_tool
TOOLS_SCHEMAS.append(DESKTOP_SCHEMA)
CORE_TOOL_NAMES = frozenset(schema['function']['name'] for schema in TOOLS_SCHEMAS)

DYNAMIC_TOOLS_DIR = Path(__file__).parent / "tools"
DYNAMIC_TOOLS_DIR.mkdir(parents=True, exist_ok=True)

def load_dynamic_tools():
    import importlib.util, sys, json
    manifest=json.loads((Path(__file__).parent/'agent_execution'/'extensions'/'builtins.json').read_text(encoding='utf-8'))
    for py_file in DYNAMIC_TOOLS_DIR.glob("*.py"):
        tool_name = py_file.stem
        if tool_name not in manifest['tools']:continue
        schema_file = py_file.with_suffix(".json")
        if not schema_file.exists(): continue
        try:
            schema = json.loads(schema_file.read_text(encoding="utf-8"))
            spec = importlib.util.spec_from_file_location(tool_name, py_file)
            module = importlib.util.module_from_spec(spec)
            sys.modules[tool_name] = module
            spec.loader.exec_module(module)
            func = getattr(module, tool_name)
            from agent_execution import policy_tool
            TOOLS[tool_name] = policy_tool(tool_name)(func)
            
            if "function" not in schema:
                schema = {"type": "function", "function": schema}
                
            global TOOLS_SCHEMAS
            TOOLS_SCHEMAS = [s for s in TOOLS_SCHEMAS if s.get("function", s).get("name") != tool_name]
            TOOLS_SCHEMAS.append(schema)
        except Exception as e:
            print(f"Failed to load dynamic tool {tool_name}: {e}")
    from agent_execution.extensions.store import ExtensionStore
    for name,function,schema in ExtensionStore().tools():
        if name in TOOLS:continue
        TOOLS[name]=function
        TOOLS_SCHEMAS.append(schema)

@policy_tool("create_dynamic_tool")
def create_dynamic_tool(name: str, code: str, schema_json: str) -> str:
    try:
        import json
        schema = json.loads(schema_json)
        if "name" not in schema and ("function" not in schema or "name" not in schema["function"]):
            return "Error: Schema must contain a 'name' field either at the top level or inside 'function'."
        import re
        if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            return "Error: Tool name must be a lowercase Python identifier"
        if name in TOOLS:return 'Error: Existing tool name cannot be replaced by generated code'
        from agent_execution.extensions.store import ExtensionStore
        key=ExtensionStore().stage(name,'tool',{'tool.py':code,'schema.json':json.dumps(schema)},'model-generated')
        return json.dumps({'success':True,'version':key,'state':'staged','next':'Review source; stage a version with test_extension.py; Docker test; human approval; activate and restart.'})
    except Exception as e:
        return f"Failed to create tool: {e}"

# Initial load
load_dynamic_tools()

TOOLS.update({
    "edit_file": edit_file,
    "search_web": search_web,
    "browse_webpage": browse_webpage,
    "analyze_image": analyze_image,
    "create_dynamic_tool": create_dynamic_tool
})

try:
    from mcp_manager import get_mcp_tools
    mcp_tools = get_mcp_tools()
    if mcp_tools:
        TOOLS_SCHEMAS.extend(mcp_tools)
except Exception as e:
    print(f"Failed to load MCP tools: {e}")
