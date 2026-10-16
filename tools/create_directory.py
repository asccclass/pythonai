"""Cross-platform batch directory creation under existing workspace lifecycle rules."""
from contextlib import nullcontext
from agent_execution import policy_tool, resolve_path


@policy_tool('create_directory')
def create_directory(paths: list[str]):
    from agent_execution.workspace_lifecycle import prepare_write, program_build, LifecycleStore
    if not paths or len(paths) > 100:
        raise ValueError('Provide 1..100 workspace-relative directory paths')
    targets = [resolve_path(path) for path in paths]
    for target in targets:
        LifecycleStore().assert_writable(target)
    created = []
    for target in targets:
        key = prepare_write(target / '.directory-admission')
        with program_build(LifecycleStore(), key) if key else nullcontext():
            target.mkdir(parents=True, exist_ok=True)
        created.append(str(target))
    return {'success': True, 'directories': created}
