"""Bounded, content-based approval previews. No credentials enter a preview."""
import difflib
import hashlib
import inspect
import json
import marshal
import os
from pathlib import Path

from .persistence import canonical, digest


def tree_digest(root):
    root = Path(root).resolve()
    entries, total, visited = [],0,0
    pending = [root]
    while pending:
        path = pending.pop()
        relative = path.relative_to(root) if path != root else Path(path.name)
        if '.git' in relative.parts:
            continue
        visited += 1
        if visited > 8192:
            raise ValueError('Approval directory snapshot capacity reached')
        if path.is_symlink() or path.is_junction():
            raise PermissionError('Approval snapshot cannot follow links')
        if path.is_dir():
            stat = path.stat()
            entries.append([relative.as_posix()+'/',stat.st_mode,stat.st_mtime_ns,stat.st_ino,'directory'])
            with os.scandir(path) as children:
                for entry in children:
                    if len(pending)+visited > 8192:
                        raise ValueError('Approval directory snapshot capacity reached')
                    pending.append(Path(entry.path))
            continue
        if not path.is_file():
            continue
        stat = path.stat()
        total += stat.st_size
        if len(entries) >= 4096 or total > 128*1024*1024:
            raise ValueError('Approval workspace snapshot capacity reached')
        with path.open('rb') as stream:
            content = stream.read(stat.st_size+1)
        if len(content)!=stat.st_size:
            raise PermissionError('File changed during approval snapshot')
        after = path.stat()
        if (after.st_size,after.st_mtime_ns,after.st_ino,after.st_mode,after.st_dev) != (stat.st_size,stat.st_mtime_ns,stat.st_ino,stat.st_mode,stat.st_dev):
            raise PermissionError('File changed during approval snapshot')
        entries.append([relative.as_posix(),stat.st_size,stat.st_mtime_ns,stat.st_ino,stat.st_mode,
                        hashlib.sha256(content).hexdigest()])
    return digest(canonical(sorted(entries),4*1024*1024))


def implementation(function):
    function = inspect.unwrap(function)
    code = getattr(function,'__code__',None)
    path = inspect.getsourcefile(function) if code is not None else None
    return {'module':getattr(function,'__module__',''),
            'name':getattr(function,'__qualname__',type(function).__name__),
            'code':hashlib.sha256(marshal.dumps(code)).hexdigest() if code else '',
            'module_hash':hashlib.sha256(Path(path).read_bytes()).hexdigest() if path else ''}


def arguments_for(function,arguments):
    if 'approval_id' in arguments:
        raise PermissionError('Models cannot supply Office approval authority')
    values = dict(arguments)
    try:
        signature = inspect.signature(function)
        bound = signature.bind(**values)
        bound.apply_defaults()
        values = dict(bound.arguments)
        for name,param in signature.parameters.items():
            if param.kind == inspect.Parameter.VAR_KEYWORD:
                values.update(values.pop(name,{}))
    except (ValueError,TypeError):
        pass  # Existing dispatcher still rejects invalid signatures before effects.
    return {key:value for key,value in values.items() if key not in {'memory','episode_id'}}


def control_snapshot(name,args,function):
    from agent_execution.policy import current_context
    from agent_execution.sessions import project_workspace
    context = current_context()
    data = {'implementation':implementation(function),
            'project_root':str(project_workspace(context.project)),
            'environment':digest(canonical({k:v for k,v in os.environ.items()
                if k.startswith(('AGENT_','OLLAMA_','PYTHON')) and k != 'PYTHONHASHSEED'
                or k in {'PATH','ROLES_CONFIG','MCP_CONFIG'}},256*1024)),
            'assets':[[str(p.resolve()),tree_digest(p)] for p in context.trusted_assets]}
    if name == 'run_skill':
        from skills import SkillRegistry
        skill = SkillRegistry().get(args['name'])
        data['skill'] = digest(canonical(skill.to_dict()))
        data['skill_assets'] = tree_digest(skill.path)
    if name.startswith('mcp_'):
        import mcp_manager
        manager = mcp_manager._mcp_manager
        if manager is None:
            raise PermissionError('Office MCP requires already discovered, configured tools')
        data['mcp'] = digest(canonical({'config':manager.server_configs,
            'routes':{key:list(value) for key,value in manager.routes.items()}},1024*1024))
    if name == 'project_delivery':
        from agent_execution.patches import ArtifactStore
        data['delivery'] = digest(canonical(ArtifactStore().get(args.get('plan_id'),'delivery')))
    # Bind Git hooks/index/ref state for arbitrary commands as well as Git tools.
    root = context.workspace.resolve()
    git = root/'.git'
    if git.is_symlink() or git.is_junction():
        raise PermissionError('Git metadata link is not an approved workspace binding')
    if git.is_file():
        value = git.read_text(encoding='utf-8').strip()
        if not value.startswith('gitdir: '):
            raise PermissionError('Invalid Git workspace binding')
        git = (root/value[8:]).resolve()
    if git.is_dir():
        git_state = {}
        roots = {'worktree':git}
        if (git/'commondir').is_file():
            roots['common'] = (git/(git/'commondir').read_text(encoding='utf-8').strip()).resolve()
        for key,directory in roots.items():
            for leaf in ('HEAD','index','config','commondir','packed-refs','hooks','refs'):
                target = directory/leaf
                if target.exists(): git_state[key+':'+leaf] = tree_digest(target)
        data['git'] = git_state
    return digest(canonical(data,128*1024))


def preview(name,args):
    from agent_execution.policy import resolve_path
    result = {'tool':name,'arguments':args}
    raw = args.get('path',args.get('file_path'))
    if name in {'write_file','edit_file','delete_file','ast_edit','patch'} and isinstance(raw,str):
        path = resolve_path(raw)
        if path.exists() and (not path.is_file() or path.stat().st_size > 65536):
            result['diff'] = 'Binary/large target: inspect the exact arguments and content hashes'
        else:
            before = path.read_text(encoding='utf-8',errors='replace') if path.exists() else ''
            after = args.get('content') if name == 'write_file' else '' if name == 'delete_file' else None
            if name == 'edit_file' and isinstance(args.get('old_str'),str) and isinstance(args.get('new_str'),str):
                after = before.replace(args['old_str'],args['new_str'],1)
            result['before_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
            if isinstance(after,str):
                difference = ''.join(difflib.unified_diff(before.splitlines(True),after.splitlines(True),
                    fromfile=raw,tofile=raw))
                result['diff'] = difference[:12000]
                result['diff_truncated'] = len(difference)>12000
    return result
