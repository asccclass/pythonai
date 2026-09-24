"""Small HTTP bridge for chatting with the ASCS Ollama endpoint."""

from __future__ import annotations

import os

from openai import APIStatusError, AuthenticationError
from openai import OpenAI

from base import TOOLS_SCHEMAS, load_dotenv, run_tool
from laya_guard import GuardDecision, LayaGuard
from memory import MemoryStore
from retrieval import build_memory_context, inject_memory_context
from working_memory import compact_messages


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


def read_user_input(prompt: str = "\nYou: ") -> str:
    try:
        return input(prompt)
    except KeyboardInterrupt:
        print()
        return "exit"


def main():
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    guard = LayaGuard()
    memory = MemoryStore()
    print("Mini agent ready. Type 'exit' to quit.")

    while True:
        user_input = read_user_input()
        if user_input.lower() in ("exit", "quit"):
            break

        episode_id = memory.start_episode()
        memory.add_event(episode_id, "message", role="user", content=user_input)

        guard_decision = guard.assess(user_input)
        guard_notice = format_guard_notice(guard_decision)
        memory.add_event(
            episode_id,
            "guard_decision",
            metadata={"guard": guard_decision},
        )
        if guard_notice:
            print(f"\n{guard_notice}")

        messages.append({"role": "user", "content": user_input})
        memory_context = build_memory_context(memory)
        if memory_context:
            memory.add_event(episode_id, "retrieval_context", content=memory_context)
        agent_messages = inject_memory_context(messages, memory_context)
        compacted_messages, working_summary = compact_messages(agent_messages)
        if working_summary is not None:
            agent_messages = compacted_messages
            memory.add_event(episode_id, "working_memory_summary", content=working_summary)
        try:
            reply = run_agent(agent_messages)
        except ValueError as e:
            memory.add_event(episode_id, "error", content=str(e), metadata={"error_type": "ValueError"})
            memory.finish_episode(episode_id, status="failed")
            print(f"\nConfiguration error: {e}")
            break
        except AuthenticationError as e:
            memory.add_event(episode_id, "error", content=str(e), metadata={"error_type": "AuthenticationError"})
            memory.finish_episode(episode_id, status="failed")
            print(f"\n{format_authentication_error(e)}")
            break
        except APIStatusError as e:
            memory.add_event(episode_id, "error", content=str(e), metadata={"error_type": "APIStatusError"})
            memory.finish_episode(episode_id, status="failed")
            print(f"\n{format_api_status_error(e)}")
            break
        messages.append({"role": "assistant", "content": reply})
        memory.add_event(episode_id, "message", role="assistant", content=reply)
        memory.finish_episode(episode_id)
        print(f"\nMiniAgent: {reply}")

if __name__ == "__main__":
    main()
    
