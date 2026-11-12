"""Session workspace tracker and cleanup for error recovery.

When an AI action or program execution encounters an error and execution must stop,
all files and directories created during that work session are completely deleted,
and modified pre-existing files are restored.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import os
from pathlib import Path
import shutil
import stat
from typing import Any, Callable

_current_tracker: ContextVar[SessionWorkspaceTracker | None] = ContextVar(
    "_current_tracker", default=None
)

IGNORED_DIR_NAMES = {
    ".git",
    "venv",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".pythonai-workspace-trash",
    "node_modules",
}


def _safe_remove_readonly(func: Callable, path: str, exc: Any) -> None:
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except Exception:
        pass


class SessionWorkspaceTracker:
    def __init__(
        self,
        workspace_root: str | Path | None = None,
        auto_cleanup_on_error: bool = True,
        parent: SessionWorkspaceTracker | None = None,
    ) -> None:
        if workspace_root is None:
            from base import workspace_root as get_workspace_root

            self.workspace_root = get_workspace_root().resolve()
        else:
            self.workspace_root = Path(workspace_root).resolve()

        self.auto_cleanup_on_error = auto_cleanup_on_error
        self.parent = parent

        self._initial_files: set[str] = set()
        self._initial_dirs: set[str] = set()
        self._created_paths: set[Path] = set()
        self._modified_files: dict[Path, str | bytes] = {}
        self._is_error: bool = False
        self._error_reason: str = ""
        self._cleaned_up: bool = False
        self._token = None

        self._capture_initial_state()

    def _capture_initial_state(self) -> None:
        if not self.workspace_root.exists():
            return
        try:
            for root, dirs, files in os.walk(self.workspace_root):
                dirs[:] = [d for d in dirs if d not in IGNORED_DIR_NAMES]
                root_path = Path(root)
                rel_root = root_path.relative_to(self.workspace_root).as_posix()
                if rel_root != ".":
                    self._initial_dirs.add(rel_root)
                for f in files:
                    rel_file = (root_path / f).relative_to(self.workspace_root).as_posix()
                    self._initial_files.add(rel_file)
        except Exception:
            pass

    @property
    def is_error(self) -> bool:
        return self._is_error

    @property
    def error_reason(self) -> str:
        return self._error_reason

    def mark_error(self, reason: str = "") -> None:
        self._is_error = True
        if reason and not self._error_reason:
            self._error_reason = str(reason)

    def record_created(self, path: str | Path) -> None:
        p = Path(path).resolve()
        try:
            if not p.is_relative_to(self.workspace_root):
                return
        except ValueError:
            return
        self._created_paths.add(p)
        # Also track newly created ancestor directories up to workspace_root
        parent = p.parent
        while parent != self.workspace_root and parent.is_relative_to(self.workspace_root):
            rel_dir = parent.relative_to(self.workspace_root).as_posix()
            if rel_dir not in self._initial_dirs:
                self._created_paths.add(parent)
            parent = parent.parent

    def record_modified(self, path: str | Path, original_content: str | bytes) -> None:
        p = Path(path).resolve()
        try:
            if not p.is_relative_to(self.workspace_root):
                return
        except ValueError:
            return
        rel = p.relative_to(self.workspace_root).as_posix()
        # Only record pre-existing files; newly created files will just be deleted.
        if rel in self._initial_files and p not in self._modified_files:
            self._modified_files[p] = original_content

    def scan_new_items(self) -> tuple[set[Path], set[Path]]:
        """Scan workspace against initial snapshot to find all newly generated files and dirs."""
        new_files: set[Path] = set()
        new_dirs: set[Path] = set()

        if not self.workspace_root.exists():
            return new_files, new_dirs

        for p in self._created_paths:
            if p.exists():
                if p.is_dir():
                    new_dirs.add(p)
                else:
                    new_files.add(p)

        try:
            for root, dirs, files in os.walk(self.workspace_root):
                dirs[:] = [d for d in dirs if d not in IGNORED_DIR_NAMES]
                root_path = Path(root)
                rel_root = root_path.relative_to(self.workspace_root).as_posix()
                if rel_root != "." and rel_root not in self._initial_dirs:
                    new_dirs.add(root_path)

                for f in files:
                    file_path = root_path / f
                    rel_file = file_path.relative_to(self.workspace_root).as_posix()
                    if rel_file not in self._initial_files:
                        new_files.add(file_path)
        except Exception:
            pass

        return new_files, new_dirs

    def cleanup(self, reason: str = "") -> dict[str, Any]:
        """Completely delete all files and directories created during this session."""
        if reason:
            self.mark_error(reason)

        if self._cleaned_up:
            return {
                "deleted_files": [],
                "deleted_directories": [],
                "reverted_files": [],
                "already_cleaned": True,
            }

        deleted_files: list[str] = []
        deleted_dirs: list[str] = []
        reverted_files: list[str] = []

        new_files, new_dirs = self.scan_new_items()

        # 1. Restore modified pre-existing files
        for mod_path, orig_content in self._modified_files.items():
            try:
                if mod_path.exists():
                    if isinstance(orig_content, bytes):
                        mod_path.write_bytes(orig_content)
                    else:
                        mod_path.write_text(orig_content, encoding="utf-8")
                    reverted_files.append(str(mod_path))
            except Exception:
                pass

        # 2. Delete newly generated files
        for f in new_files:
            try:
                if f.is_file() or f.is_symlink():
                    try:
                        os.chmod(f, stat.S_IWRITE)
                    except Exception:
                        pass
                    f.unlink(missing_ok=True)
                    deleted_files.append(str(f))
            except Exception:
                pass

        # 3. Clean up database records for deleted files (companion outputs)
        self._cleanup_database_records(deleted_files, deleted_dirs)

        # 4. Delete newly generated directories (deepest first)
        sorted_dirs = sorted(
            new_dirs,
            key=lambda d: (len(d.parts), len(str(d))),
            reverse=True,
        )

        for d in sorted_dirs:
            try:
                if d.exists() and d.is_dir():
                    try:
                        shutil.rmtree(d, onexc=_safe_remove_readonly)
                    except TypeError:
                        shutil.rmtree(d, onerror=_safe_remove_readonly)
                    deleted_dirs.append(str(d))
            except Exception:
                try:
                    d.rmdir()
                    deleted_dirs.append(str(d))
                except Exception:
                    pass

        self._cleaned_up = True

        return {
            "deleted_files": deleted_files,
            "deleted_directories": deleted_dirs,
            "reverted_files": reverted_files,
            "error": self._error_reason,
        }

    def _cleanup_database_records(self, file_paths: list[str], dir_paths: list[str]) -> None:
        try:
            from contextlib import closing
            from agent_execution.workspace_lifecycle import LifecycleStore

            store = LifecycleStore()
            with closing(store.connect()) as conn, conn:
                tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if "companion_outputs" in tables:
                    for fp in file_paths:
                        p = Path(fp)
                        try:
                            rel = p.relative_to(self.workspace_root).as_posix()
                            conn.execute(
                                "UPDATE companion_outputs SET state='deleted' WHERE path=? AND root=?",
                                (rel, str(self.workspace_root)),
                            )
                        except Exception:
                            pass
                if "wl_directories" in tables:
                    for dp in dir_paths:
                        p = Path(dp)
                        try:
                            rel = p.relative_to(self.workspace_root).as_posix()
                            conn.execute(
                                "UPDATE wl_directories SET state='purged' WHERE relative=?",
                                (rel,),
                            )
                        except Exception:
                            pass
        except Exception:
            pass

    def __enter__(self) -> SessionWorkspaceTracker:
        self.parent = _current_tracker.get()
        self._token = _current_tracker.set(self)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        try:
            if exc_type is not None:
                self.mark_error(str(exc_val or exc_type.__name__))
                if self.auto_cleanup_on_error:
                    self.cleanup()
            elif self.is_error and self.auto_cleanup_on_error:
                self.cleanup()
            elif not self.is_error and self.parent is not None:
                # Propagate created items to parent session tracker
                self.parent._created_paths.update(self._created_paths)
                for p, c in self._modified_files.items():
                    if p not in self.parent._modified_files:
                        self.parent._modified_files[p] = c
        finally:
            if self._token is not None:
                _current_tracker.reset(self._token)
                self._token = None
        return False


def get_current_tracker() -> SessionWorkspaceTracker | None:
    return _current_tracker.get()


def track_created_file(path: str | Path) -> None:
    tracker = _current_tracker.get()
    if tracker is not None:
        tracker.record_created(path)


def track_modified_file(path: str | Path, original_content: str | bytes) -> None:
    tracker = _current_tracker.get()
    if tracker is not None:
        tracker.record_modified(path, original_content)


def cleanup_session_artifacts(reason: str = "") -> dict[str, Any]:
    tracker = _current_tracker.get()
    if tracker is not None:
        return tracker.cleanup(reason=reason)
    return {"deleted_files": [], "deleted_directories": [], "reverted_files": [], "no_tracker": True}


@contextmanager
def session_cleanup_scope(
    workspace_root: str | Path | None = None,
    auto_cleanup_on_error: bool = True,
    enabled: bool = True,
):
    if not enabled:
        yield None
        return
    tracker = SessionWorkspaceTracker(
        workspace_root=workspace_root,
        auto_cleanup_on_error=auto_cleanup_on_error,
    )
    with tracker:
        yield tracker
