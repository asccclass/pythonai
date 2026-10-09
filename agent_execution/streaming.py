"""Assemble actual Chat Completions deltas, including fragmented function calls."""
import threading
from types import SimpleNamespace


def streamed_completion(client, model, messages, tools, emit, cancelled=None):
    from .process import command_timeout
    from openai import OpenAI
    if isinstance(client, OpenAI):
        client = client.with_options(max_retries=0)
    stream = client.chat.completions.create(model=model, messages=messages, tools=tools,
                                             stream=True, timeout=min(command_timeout(), 60))
    stop = threading.Event()
    def monitor():
        while not stop.wait(.05):
            if cancelled and cancelled():
                try:
                    stream.close()
                except Exception:
                    pass
                return
    watcher = threading.Thread(target=monitor, daemon=True)
    watcher.start()
    content, calls, finish, size = [], {}, None, 0
    try:
        for chunk in stream:
            if cancelled and cancelled():
                raise InterruptedError("Model stream cancelled")
            for choice in chunk.choices:
                if choice.index != 0:
                    continue
                delta = choice.delta
                size += len((delta.content or "").encode())
                if delta.content:
                    content.append(delta.content)
                    emit("model.delta", {"text": delta.content})
                for call in delta.tool_calls or []:
                    target = calls.setdefault(call.index, {"id": "", "name": "", "arguments": ""})
                    if call.id:
                        if target["id"] and target["id"] != call.id:
                            raise RuntimeError("Streamed tool call ID changed")
                        target["id"] = call.id
                    if call.function:
                        target["name"] += call.function.name or ""
                        target["arguments"] += call.function.arguments or ""
                        size += len((call.function.arguments or "").encode())
                if size > 2 * 1024 * 1024:
                    raise RuntimeError("Model stream exceeded 2 MiB output limit")
                if choice.finish_reason:
                    finish = choice.finish_reason
        if cancelled and cancelled():
            raise InterruptedError("Model stream cancelled")
        if finish not in {"stop", "tool_calls"}:
            raise RuntimeError(f"Incomplete model stream ({finish or 'missing finish_reason'})")
        tool_calls = [SimpleNamespace(id=value["id"], type="function",
                      function=SimpleNamespace(name=value["name"], arguments=value["arguments"]))
                      for _, value in sorted(calls.items())]
        if any(not call.id or not call.function.name for call in tool_calls):
            raise RuntimeError("Incomplete streamed tool call")
        from openai.types.chat import ChatCompletionMessage
        return ChatCompletionMessage(role="assistant", content="".join(content) or None,
            tool_calls=[{"id": call.id, "type": "function", "function": {"name": call.function.name, "arguments": call.function.arguments}}
                        for call in tool_calls] or None)
    except Exception as error:
        if cancelled and cancelled():
            raise InterruptedError("Model stream cancelled") from error
        raise
    finally:
        stop.set()
        stream.close()
        watcher.join(.2)
