"""Bounded, non-following inspection. Mutation helpers are separate from previews."""
import hashlib
import json
import os
from pathlib import Path
import stat
import time


def identity(path):
    value = Path(path).lstat()
    if stat.S_ISLNK(value.st_mode) or getattr(value, 'st_file_attributes', 0) & 0x400:
        raise PermissionError('Links and reparse points are not managed directories')
    return [value.st_dev, value.st_ino]


def directory(root, relative):
    root = Path(root).absolute()
    value = Path(relative)
    if not relative or value.is_absolute() or any(p in {'..', '.', '.git'} for p in value.parts):
        raise PermissionError('A workspace-relative child directory is required')
    if ':' in str(value) or '\x00' in str(value):
        raise PermissionError('Invalid directory name')
    target = root / value
    if target == root or not target.resolve().is_relative_to(root.resolve()):
        raise PermissionError('Directory is outside the registered workspace')
    for item in [root, *[root.joinpath(*value.parts[:i]) for i in range(1, len(value.parts)+1)]]:
        if item.exists() or item.is_symlink():
            identity(item)
            if not item.is_dir():raise PermissionError('Directory parent is not a directory')
    return target


def snapshot(path):
    path = Path(path)
    started = time.monotonic()
    maximum = int(os.environ.get('AGENT_CLEANUP_MAX_FILES', '10000'))
    capacity = int(os.environ.get('AGENT_CLEANUP_MAX_BYTES', str(128*1024*1024)))
    records, size = [], 0
    def visit(current):
        nonlocal size
        ident = identity(current)
        info = current.lstat()
        rel = current.relative_to(path).as_posix()
        if current.name == '.git':raise PermissionError('Git directories require the worktree workflow')
        if len(records) >= maximum or time.monotonic()-started > 30:
            raise ValueError('Directory inspection limit exceeded')
        if stat.S_ISDIR(info.st_mode):
            records.append([rel, 'directory', ident])
            for item in sorted(current.iterdir(), key=lambda p: p.name):visit(item)
        elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
            size += info.st_size
            if size > capacity:raise ValueError('Directory size limit exceeded')
            digest = hashlib.sha256()
            with current.open('rb') as stream:
                for block in iter(lambda: stream.read(1024*1024), b''):digest.update(block)
            after = current.lstat()
            if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns):
                raise ValueError('File changed during preview')
            records.append([rel, 'file', ident, info.st_size, digest.hexdigest()])
        else:raise PermissionError('Unsupported file type or hard link')
    visit(path)
    digest = hashlib.sha256(json.dumps(records, sort_keys=True).encode()).hexdigest()
    return {'digest': digest, 'files': sum(r[1]=='file' for r in records), 'bytes': size, 'records': records}
