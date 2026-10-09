from agent_execution import policy_tool


@policy_tool("manage_memory")
def manage_memory(
    action: str,
    memory_id: int,
    confirm: bool = False,
    memory=None,
    episode_id=None,
    reason: str = "",
    superseded_by: int | None = None
) -> str:
    if memory is None or episode_id is None:
        return "Error: Memory store is not available."
    
    # High-risk decisions are enforced by the common policy; confirm is legacy metadata.
    if action == "confirm":
        memory.add_event(episode_id, "semantic_memory_confirmation", metadata={"semantic_memory_id": memory_id, "reason": reason})
        return f"Success: Memory #{memory_id} confirmation recorded."
    elif action == "contradict":
        memory.add_event(episode_id, "semantic_memory_contradiction", metadata={"semantic_memory_id": memory_id, "reason": reason})
        return f"Success: Memory #{memory_id} contradiction recorded."
    elif action == "archive":
        memory.archive_semantic_memory(memory_id, reason=reason)
        return f"Success: Memory #{memory_id} archived."
    elif action == "supersede":
        if superseded_by is None:
            return "Error: supersede requires superseded_by argument."
        memory.supersede_semantic_memory(memory_id, superseded_by, reason=reason)
        return f"Success: Memory #{memory_id} superseded by #{superseded_by}."
    else:
        return f"Error: Unknown action '{action}'."
