from agent_execution.policy import policy_tool, git_workspace
from agent_execution.worktrees import commit_files, git, selected_files
from agent_execution.results import ToolResult


@policy_tool('git_commit')
def git_commit(message: str, files: list[str] | None = None, add_all: bool = False, create_branch: str = ''):
    """Commit a file selection without consuming unrelated staged changes."""
    if add_all:return ToolResult(False,error='Broad staging is disabled; select files explicitly')
    repository=git_workspace()
    try:
        selected_files(repository,files)
        if create_branch:
            if not create_branch.startswith('codex/'):raise ValueError('Task branches must start with codex/')
            git(repository,'check-ref-format','--branch',create_branch)
            git(repository,'checkout','-b',create_branch)
        evidence=commit_files(repository,message,files)
        return ToolResult(True,output='Committed '+evidence['commit'],evidence=evidence,
            side_effects={'status':'observed','commit':evidence['commit']})
    except (ValueError,RuntimeError) as error:return ToolResult(False,error=str(error))
