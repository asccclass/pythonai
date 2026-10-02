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
    
    # 1. Ask for confirmation for high-risk actions if not confirmed
    if not confirm and action in ('archive', 'contradict', 'supersede'):
        return f"Warning: {action} is a high-risk operation. You must ask the user for confirmation first. Do not call this tool again with confirm=True until the user has explicitly agreed to {action} memory #{memory_id}."

    # 2. Perform the action
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