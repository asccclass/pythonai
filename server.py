"""Small HTTP bridge for chatting with the ASCS Ollama endpoint."""

from __future__ import annotations

import json
import os

from openai import OpenAI

from base import list_files, load_dotenv, read_file, run_command, write_file


load_dotenv()

OLLAMA_BASE_URL = os.environ["OLLAMA_BASE_URL"]
OLLAMA_MODEL = os.environ["OLLAMA_MODEL"]
OLLAMA_API_KEY = os.environ["OLLAMA_API_KEY"]
SERVER_HOST = os.getenv("SERVER_HOST", "127.0.0.1")
SERVER_PORT = int(os.getenv("SERVER_PORT", "8000"))


def create_ollama_client() -> OpenAI:
    return OpenAI(
        base_url=OLLAMA_BASE_URL,
        api_key=OLLAMA_API_KEY,
    )


client = create_ollama_client()

TOOLS = {
    "read_file": read_file,
    "list_files": list_files,
    "write_file": write_file,
    "run_command": run_command,
}

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

SYSTEM_PROMPT = ""    

message = [
    {"role": "user", "content": "You are a helpful assistant."}
]

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

def run_agent(messages):
    while True:
        response = client.chat.completions.create(
            model = OLLAMA_MODEL,
            messages = messages,
            tools = TOOLS_SCHEMAS
        )
        message = response.choices[0].message
        message.append(message)

        while message.tool_calls:
            for tool_call in message.tool_calls:
                result = run_tool(tool_call)
                message.append({"role": "tool", "tool_call_id": tool_call.id, "content": result})

def main():
    message = [{"role": "system", "content": SYSTEM_PROMPT}]
    print("Mini agent ready. Type 'exit' to quit.")

    while True:
        user_input = input("\nYou: ")
        if user_input.lower() in ("exit", "quit"):
            break

        message.append({"role": "user", "content": user_input})
        reply = run_agen(messages)
        print(f"\nMiniAgent: {reply}")

if __name__ == "__main__":
    main()
    
