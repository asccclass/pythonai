from agent_execution.policy import policy_tool, git_workspace
from agent_execution.worktrees import rollback_files
from agent_execution.results import ToolResult

@policy_tool("git_rollback")
def git_rollback(files: list[str] | None = None):
    """Restore approved tracked files, retaining an archive before discarding edits."""
    repository=git_workspace()
    try:
        evidence=rollback_files(repository,files)
        return ToolResult(True,output='Restored selected files',artifacts=[evidence['archive']],
            evidence=evidence,side_effects={'status':'observed','restored_files':evidence['files']})
    except (ValueError,RuntimeError) as error:return ToolResult(False,error=str(error))
