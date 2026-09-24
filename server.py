"""Small HTTP bridge for chatting with the ASCS Ollama endpoint."""

from __future__ import annotations

import os

from openai import APIStatusError, AuthenticationError
from openai import OpenAI

from base import TOOLS_SCHEMAS, load_dotenv, run_tool
from laya_guard import GuardDecision, LayaGuard


load_dotenv()

OLLAMA_BASE_URL = os.environ["OLLAMA_BASE_URL"]
OLLAMA_MODEL = os.environ["OLLAMA_MODEL"]
OLLAMA_API_KEY = os.environ["OLLAMA_API_KEY"]

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


def format_guard_notice(decision: GuardDecision) -> str:
    if not decision.available:
        return f"Laya guard unavailable; continuing without guard. {decision.reason}"
    if decision.needs_confirmation or decision.risk >= 1.5:
        return (
            "Laya guard: "
            f"intent={decision.intent}, risk={decision.risk:.2f}, "
            f"needs_confirmation={decision.needs_confirmation}"
        )
    return ""


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

def main():
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    guard = LayaGuard()
    print("Mini agent ready. Type 'exit' to quit.")

    while True:
        user_input = input("\nYou: ")
        if user_input.lower() in ("exit", "quit"):
            break

        guard_notice = format_guard_notice(guard.assess(user_input))
        if guard_notice:
            print(f"\n{guard_notice}")

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

if __name__ == "__main__":
    main()
    
