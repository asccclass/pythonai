import os
import json
import platform
import subprocess
from pathlib import Path


def read_file(path: str | Path) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        print(f"File not found: {path}")
    except Exception as e:
        print(f"Error reading file: {e}")
    return ""


def list_files(path: str | Path = ".") -> list[str]:
    return [item.name for item in Path(path).iterdir()]


def write_file(path: str | Path, content: str) -> None:
    Path(path).write_text(content, encoding="utf-8")


def delete_file(path: str | Path) -> subprocess.CompletedProcess[str]:
    target = Path(path)
    if target.is_dir():
        return subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr=f"Refusing to delete directory: {target}",
        )

    command = ["cmd", "/c", "del", "/f", "/q", str(target)] if platform.system() == "Windows" else ["rm", "-f", str(target)]
    return subprocess.run(
        command,
        capture_output=True,
        check=False,
        text=True,
    )


def run_command(command: list[str], cwd: str | Path | None = None) -> subprocess.CompletedProcess[str]:
    answer = input(f" Run '{command}'? [y/N]: ")
    if answer.lower() != "y":
        return subprocess.CompletedProcess(
            args=command,
            returncode=1,
            stdout="",
            stderr="User cancelled",
        )
    return subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        check=False,
        text=True,
    )


def load_dotenv(path: str | Path = ".env") -> None:
    """Load simple KEY=value pairs into the process environment."""

    env_path = Path(path)
    if not env_path.exists():
        return

    for raw_line in read_file(env_path).splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


TOOLS = {
    "read_file": read_file,
    "list_files": list_files,
    "write_file": write_file,
    "delete_file": delete_file,
    "run_command": run_command,
}


def run_tool(tool_call):
    name = tool_call.function.name
    args = json.loads(tool_call.function.arguments)
    if name not in TOOLS:
        return f"Error: Tool '{name}' not found"
    try:
        result = TOOLS[name](**args)
        return result
    except Exception as e:
        return f"Error: {e}"

TOOLS_SCHEMAS = [
    {
        "type": "function",
        "function": {
                "name": "read_file",
                "description": "Read a text file and return its contents.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "The path to the file to read"
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
                "description": "List files and directories inside a path.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "The directory path to list"
                        }
                    }
                }
        }
    },
    {
        "type": "function",
        "function": {
                "name": "write_file",
                "description": "Write text content to a file.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "The path to the file to write"
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
                "description": "Delete a single file using the appropriate operating system command.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "The path to the file to delete"
                        }
                    },
                    "required": ["path"]
                }
        }
    },
    {
        "type": "function",
        "function": {
                "name": "run_command",
                "description": "Run a command and return its completed process result.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "The command and arguments to run"
                        },
                        "cwd": {
                            "type": "string",
                            "description": "Optional working directory"
                        }
                    },
                    "required": ["command"]
                }
        }
    }
]
