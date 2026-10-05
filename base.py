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

def edit_file(path: str | Path, old_str: str, new_str: str) -> dict[str, str]:
    target = resolve_workspace_path(path)
    if not target.exists():
        if old_str == "":
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(new_str, encoding="utf-8")
            return {"path": str(target), "action": "created_file"}
        else:
            return {"path": str(target), "action": "file not found"}
    
    original = target.read_text(encoding="utf-8")
    if original.find(old_str) == -1:
        return {"path": str(target), "action": "old_str not found"}
    
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
    if os.environ.get("AGENT_USE_SANDBOX", "").lower() in ("1", "true"):
        from sandbox import SandboxExecution
        sandbox = SandboxExecution(workspace_root())
        sandbox.setup()
        try:
            return sandbox.run(resolved_command, cwd=resolved_cwd, **run_options)
        finally:
            sandbox.cleanup()
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
    "manage_memory": manage_memory,
}


def run_tool(tool_call):
    name = tool_call.function.name
    try:
        from permissions import check_tool_permission, current_role
        if not check_tool_permission(name):
            error_msg = f"Error: Permission denied. Role '{current_role()}' cannot execute {name}."
            print(f"AUDIT: {error_msg}")
            if memory and episode_id:
                try:
                    from agent_runtime import log_episode_event
                    log_episode_event(memory, episode_id, "permission_denied", content=error_msg, metadata={"tool": name, "role": current_role()})
                except Exception:
                    pass
            return error_msg
    except ImportError:
        pass
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
    try:
        from permissions import check_tool_permission, current_role
        if not check_tool_permission(name):
            error_msg = f"Error: Permission denied. Role '{current_role()}' cannot execute {name}."
            print(f"AUDIT: {error_msg}")
            if memory and episode_id:
                try:
                    from agent_runtime import log_episode_event
                    log_episode_event(memory, episode_id, "permission_denied", content=error_msg, metadata={"tool": name, "role": current_role()})
                except Exception:
                    pass
            return error_msg
    except ImportError:
        pass
    args = json.loads(tool_call.function.arguments)
    if name == "run_command":
        args.pop("trusted_asset_roots", None)
    if name in ("run_skill", "manage_memory"):
        args["memory"] = memory
        args["episode_id"] = episode_id
    if name.startswith("mcp_"):
        try:
            from mcp_manager import execute_mcp_tool
            return execute_mcp_tool(name, args)
        except Exception as e:
            return f"Error executing MCP tool: {e}"
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
    },
    {
        "type": "function",
        "function": {
            "name": "manage_memory",
            "description": "Manage long-term semantic memories. Use this to correct, archive, supersede, or confirm memories when asked by the user.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": "The action to perform: 'confirm', 'archive', 'contradict', or 'supersede'",
                        "enum": [
                            "confirm",
                            "archive",
                            "contradict",
                            "supersede"
                        ]
                    },
                    "memory_id": {
                        "type": "integer",
                        "description": "The ID of the memory to modify (e.g., 123)"
                    },
                    "confirm": {
                        "type": "boolean",
                        "description": "Set to true only if the user has explicitly confirmed this high-risk action in the chat."
                    },
                    "reason": {
                        "type": "string",
                        "description": "Reason for the modification."
                    },
                    "superseded_by": {
                        "type": "integer",
                        "description": "ID of the new memory that supersedes this one (required for 'supersede' action)."
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
                "description": "Edit an existing file by replacing a specific string. Use this carefully.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "The workspace-relative path to the file"
                        },
                        "old_str": {
                            "type": "string",
                            "description": "The exact string to be replaced"
                        },
                        "new_str": {
                            "type": "string",
                            "description": "The string to replace it with"
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
                "description": "Search the web using DuckDuckGo.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "The search query"
                        },
                        "max_results": {
                            "type": "integer",
                            "description": "Maximum number of results to return (default: 5)"
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
                "description": "Browse a webpage and return its text content.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "The URL of the webpage to browse"
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
                "description": "Analyze an image using the vision model and return a description.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "image_path": {
                            "type": "string",
                            "description": "The workspace-relative path to the image file"
                        },
                        "prompt": {
                            "type": "string",
                            "description": "Optional prompt or question about the image (default: 'Describe this image in detail.')"
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
                "description": "Create a new Python tool dynamically. The tool will be saved and hot-reloaded into the environment immediately.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "The name of the tool (must be a valid Python identifier)"
                        },
                        "code": {
                            "type": "string",
                            "description": "The complete Python code for the tool."
                        },
                        "schema_json": {
                            "type": "string",
                            "description": "The JSON string representation of the tool's schema, strictly adhering to the OpenAI tool calling format."
                        }
                    },
                    "required": ["name", "code", "schema_json"]
                }
        }
    }
]

def search_web(query: str, max_results: int = 5) -> str:
    try:
        import urllib.request
        import urllib.parse
        import json
        
        url = f"https://en.wikipedia.org/w/api.php?action=query&list=search&srsearch={urllib.parse.quote(query)}&utf8=&format=json"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read().decode())
        
        results = data.get("query", {}).get("search", [])[:max_results]
        formatted = [{"title": r["title"], "snippet": r["snippet"]} for r in results]
        return json.dumps(formatted, ensure_ascii=False, indent=2)
    except Exception as e:
        return f"Search failed: {e}"

def browse_webpage(url: str) -> str:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, wait_until="networkidle", timeout=15000)
            content = page.evaluate("() => document.body.innerText")
            browser.close()
            return content[:15000]
    except Exception as e:
        return f"Browsing failed: {e}"

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

DYNAMIC_TOOLS_DIR = Path(__file__).parent / "tools"
DYNAMIC_TOOLS_DIR.mkdir(parents=True, exist_ok=True)

def load_dynamic_tools():
    import importlib.util, sys, json
    for py_file in DYNAMIC_TOOLS_DIR.glob("*.py"):
        tool_name = py_file.stem
        schema_file = py_file.with_suffix(".json")
        if not schema_file.exists(): continue
        try:
            schema = json.loads(schema_file.read_text(encoding="utf-8"))
            spec = importlib.util.spec_from_file_location(tool_name, py_file)
            module = importlib.util.module_from_spec(spec)
            sys.modules[tool_name] = module
            spec.loader.exec_module(module)
            func = getattr(module, tool_name)
            TOOLS[tool_name] = func
            global TOOLS_SCHEMAS
            TOOLS_SCHEMAS = [s for s in TOOLS_SCHEMAS if s.get("function", s).get("name") != tool_name]
            TOOLS_SCHEMAS.append(schema)
        except Exception as e:
            print(f"Failed to load dynamic tool {tool_name}: {e}")

def create_dynamic_tool(name: str, code: str, schema_json: str) -> str:
    try:
        import json
        schema = json.loads(schema_json)
        if "function" not in schema or "name" not in schema["function"]:
            return "Error: Schema must contain 'function' and 'name' fields."
        py_file = DYNAMIC_TOOLS_DIR / f"{name}.py"
        schema_file = DYNAMIC_TOOLS_DIR / f"{name}.json"
        py_file.write_text(code, encoding="utf-8")
        schema_file.write_text(json.dumps(schema, indent=2), encoding="utf-8")
        load_dynamic_tools()
        return f"Tool '{name}' successfully created and hot-reloaded!"
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
