"""Small HTTP bridge for chatting with the ASCS Ollama endpoint."""

from __future__ import annotations

import os
import time
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from openai import APIStatusError, AuthenticationError
from openai import OpenAI
import uvicorn

from base import TOOLS_SCHEMAS, load_dotenv, run_tool


load_dotenv()

OLLAMA_BASE_URL = os.environ["OLLAMA_BASE_URL"]
OLLAMA_MODEL = os.environ["OLLAMA_MODEL"]
OLLAMA_API_KEY = os.environ["OLLAMA_API_KEY"]
SERVER_HOST = os.environ.get("SERVER_HOST", "127.0.0.1")
SERVER_PORT = int(os.environ.get("SERVER_PORT", "8000"))

app = FastAPI(title="Python AI Mini Agent OpenAI Bridge")


class ChatCompletionRequest(BaseModel):
    model: str | None = None
    messages: list[dict[str, Any]] = Field(default_factory=list)
    stream: bool = False
    max_tokens: int | None = None
    temperature: float | None = None

def validate_ollama_api_key(api_key: str) -> None:
    if not api_key.startswith("sk-"):
        raise ValueError("OLLAMA_API_KEY must be a LiteLLM virtual key that starts with 'sk-'.")


def create_ollama_client() -> OpenAI:
    validate_ollama_api_key(OLLAMA_API_KEY)
    return OpenAI(
        base_url=OLLAMA_BASE_URL,
        api_key=OLLAMA_API_KEY,
    )


client: OpenAI | None = None


def get_client() -> OpenAI:
    global client
    if client is None:
        client = create_ollama_client()
    return client


def format_authentication_error(error: AuthenticationError) -> str:
    return f"Authentication failed for {OLLAMA_BASE_URL}. Check OLLAMA_API_KEY in .env. {error}"


def format_api_status_error(error: APIStatusError) -> str:
    if error.status_code in (401, 403):
        return (
            f"Request was rejected by {OLLAMA_BASE_URL} with HTTP {error.status_code}. "
            "Check OLLAMA_BASE_URL, OLLAMA_API_KEY, and whether the key can access "
            f"OLLAMA_MODEL={OLLAMA_MODEL!r}. {error}"
        )
    return f"Request to {OLLAMA_BASE_URL} failed with HTTP {error.status_code}. {error}"


SYSTEM_PROMPT = ""    

message = [
    {"role": "user", "content": "You are a helpful assistant."}
]

def run_agent(messages):
    while True:
        response = get_client().chat.completions.create(
            model = OLLAMA_MODEL,
            messages = messages,
            tools = TOOLS_SCHEMAS
        )
        assistant_message = response.choices[0].message
        messages.append(assistant_message)

        if assistant_message.tool_calls:
            for tool_call in assistant_message.tool_calls:
                result = run_tool(tool_call)
                messages.append({"role": "tool", "tool_call_id": tool_call.id, "content": str(result)})
            continue

        return assistant_message.content or ""


@app.get("/v1/models")
def list_models() -> dict[str, Any]:
    return {
        "object": "list",
        "data": [
            {
                "id": OLLAMA_MODEL,
                "object": "model",
                "created": 0,
                "owned_by": "pythonai",
            }
        ],
    }


@app.post("/v1/chat/completions")
def create_chat_completion(request: ChatCompletionRequest) -> dict[str, Any]:
    if request.stream:
        raise HTTPException(status_code=400, detail="Streaming chat completions are not supported yet.")

    messages = [dict(message) for message in request.messages]
    content = run_agent(messages)
    created = int(time.time())
    model = request.model or OLLAMA_MODEL
    return {
        "id": f"chatcmpl-pythonai-{created}",
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": content,
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
    }


def run_cli():
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    print("Mini agent ready. Type 'exit' to quit.")

    while True:
        user_input = input("\nYou: ")
        if user_input.lower() in ("exit", "quit"):
            break

        messages.append({"role": "user", "content": user_input})
        try:
            reply = run_agent(messages)
        except ValueError as e:
            print(f"\nConfiguration error: {e}")
            break
        except AuthenticationError as e:
            print(f"\n{format_authentication_error(e)}")
            break
        except APIStatusError as e:
            print(f"\n{format_api_status_error(e)}")
            break
        print(f"\nMiniAgent: {reply}")


def main():
    uvicorn.run(app, host=SERVER_HOST, port=SERVER_PORT)

if __name__ == "__main__":
    main()
    
