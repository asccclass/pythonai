import os
import json
import platform
import subprocess
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from laya_guard import GuardDecision, LayaGuard
from skills import SkillExecutor, SkillRegistry


_approved_commands: set[tuple[tuple[str, ...], str | None]] = set()
_command_guard: LayaGuard | None = None
_auto_approve_commands: ContextVar[bool] = ContextVar("auto_approve_commands", default=False)


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_WORKSPACE_DIR = PROJECT_ROOT / "workspace"


def workspace_root() -> Path:
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
    for item in command:
        if not isinstance(item, str) or not looks_like_path_argument(item):
            continue
        resolve_workspace_or_trusted_path(item, trusted_asset_roots)


def looks_like_path_argument(value: str) -> bool:
    if "://" in value:
        return False
    path = Path(value)
    return path.is_absolute() or value.startswith(("..", ".", "~")) or "/" in value or "\\" in value


def read_file(path: str | Path) -> str:
    try:
        with open(resolve_workspace_path(path), "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        print(f"File not found: {path}")
    except Exception as e:
        print(f"Error reading file: {e}")
    return ""


def list_files(path: str | Path = ".") -> list[str]:
    return [item.name for item in resolve_workspace_path(path).iterdir()]


def write_file(path: str | Path, content: str) -> None:
    target = resolve_workspace_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


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
    except PermissionError as error:
        return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr=str(error))
    if target.is_dir():
        return subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr=f"Refusing to delete directory: {target}",
        )

    command = ["cmd", "/c", "del", "/f", "/q", str(target)] if platform.system() == "Windows" else ["rm", "-f", str(target)]
    return subprocess.run(command, **subprocess_text_options())


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


def fetch_url(url: str, max_time: int = 20, follow_redirects: bool = True) -> subprocess.CompletedProcess[str]:
    command = curl_command(url, max_time=max_time, follow_redirects=follow_redirects)
    return subprocess.run(command, **subprocess_text_options())


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
) -> subprocess.CompletedProcess[str]:
    try:
        resolved_cwd = resolve_command_cwd(cwd, trusted_asset_roots)
        resolved_env_file = (
            resolve_workspace_or_trusted_path(env_file, trusted_asset_roots) if env_file is not None else None
        )
        validate_command_paths(command, trusted_asset_roots)
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
    return subprocess.run(resolved_command, cwd=resolved_cwd, **run_options)


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


def run_skill(name: str, inputs: dict | None = None, memory=None, episode_id: int | None = None) -> dict:
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


TOOLS = {
    "read_file": read_file,
    "list_files": list_files,
    "write_file": write_file,
    "delete_file": delete_file,
    "fetch_url": fetch_url,
    "run_command": run_command,
    "run_skill": run_skill,
}


def run_tool(tool_call):
    name = tool_call.function.name
    args = json.loads(tool_call.function.arguments)
    if name == "run_command":
        args.pop("trusted_asset_roots", None)
    if name not in TOOLS:
        return f"Error: Tool '{name}' not found"
    try:
        result = TOOLS[name](**args)
        return result
    except Exception as e:
        return f"Error: {e}"


def run_tool_with_context(tool_call, memory=None, episode_id: int | None = None):
    name = tool_call.function.name
    args = json.loads(tool_call.function.arguments)
    if name == "run_command":
        args.pop("trusted_asset_roots", None)
    if name == "run_skill":
        args["memory"] = memory
        args["episode_id"] = episode_id
    if name not in TOOLS:
        return f"Error: Tool '{name}' not found"
    try:
        return TOOLS[name](**args)
    except Exception as e:
        return f"Error: {e}"

TOOLS_SCHEMAS = [
    {
        "type": "function",
        "function": {
                "name": "read_file",
                "description": "Read a text file inside the Agent workspace and return its contents.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "The workspace-relative path to the file to read"
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
                "description": "List files and directories inside the Agent workspace.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "The workspace-relative directory path to list"
                        }
                    }
                }
        }
    },
    {
        "type": "function",
        "function": {
                "name": "write_file",
                "description": "Write text content to a file inside the Agent workspace.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "The workspace-relative path to the file to write"
                        },
                        "content": {
                            "type": "string",
                            "description": "The text content to write"
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
                "description": "Delete a single file inside the Agent workspace using the appropriate operating system command.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "The workspace-relative path to the file to delete"
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
                "description": "Fetch a URL over the network using curl and return the completed process result.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "The URL to fetch"
                        },
                        "max_time": {
                            "type": "integer",
                            "description": "Maximum curl runtime in seconds"
                        },
                        "follow_redirects": {
                            "type": "boolean",
                            "description": "Whether curl should follow redirects"
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
                "description": "Run a command inside the Agent workspace and return its completed process result. cwd is workspace-relative; paths outside the workspace are rejected.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "The command and arguments to run. Path arguments must stay inside the Agent workspace."
                        },
                        "cwd": {
                            "type": "string",
                            "description": "Optional workspace-relative working directory"
                        }
                    },
                    "required": ["command"]
                }
        }
    },
    {
        "type": "function",
        "function": {
                "name": "run_skill",
                "description": "Run a local registered Skill by name with structured inputs.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "The local Skill name"
                        },
                        "inputs": {
                            "type": "object",
                            "description": "Skill inputs matching the Skill input schema"
                        }
                    },
                    "required": ["name"]
                }
        }
    }
]
