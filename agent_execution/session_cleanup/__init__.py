"""Session workspace tracker and cleanup package.

When an AI action or program encounters an error that requires stopping execution,
all files or directories generated during the work session are completely deleted.
"""
from .tracker import (
    SessionWorkspaceTracker,
    get_current_tracker,
    session_cleanup_scope,
    track_created_file,
    track_modified_file,
    cleanup_session_artifacts,
)

__all__ = [
    "SessionWorkspaceTracker",
    "get_current_tracker",
    "session_cleanup_scope",
    "track_created_file",
    "track_modified_file",
    "cleanup_session_artifacts",
]

