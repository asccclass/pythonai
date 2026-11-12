"""Comprehensive unit and integration tests for session workspace cleanup on error."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from agent_execution.session_cleanup import (
    SessionWorkspaceTracker,
    session_cleanup_scope,
    get_current_tracker,
    track_created_file,
    track_modified_file,
    cleanup_session_artifacts,
)


class SessionCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name).resolve()

        # Create some pre-existing files and directories
        (self.workspace / "existing_dir").mkdir()
        self.pre_file = self.workspace / "existing_dir" / "pre.txt"
        self.pre_file.write_text("initial content", encoding="utf-8")

        self.root_file = self.workspace / "root_keep.txt"
        self.root_file.write_text("keep this root file", encoding="utf-8")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_cleanup_deletes_newly_created_files_on_error(self):
        with self.assertRaises(RuntimeError):
            with session_cleanup_scope(workspace_root=self.workspace, auto_cleanup_on_error=True):
                new_file = self.workspace / "created.txt"
                new_file.write_text("temporary content", encoding="utf-8")
                track_created_file(new_file)
                self.assertTrue(new_file.exists())
                raise RuntimeError("Simulated execution failure to stop action")

        self.assertFalse((self.workspace / "created.txt").exists())
        self.assertTrue(self.pre_file.exists())
        self.assertTrue(self.root_file.exists())

    def test_cleanup_deletes_newly_created_directories_on_error(self):
        with self.assertRaises(ValueError):
            with session_cleanup_scope(workspace_root=self.workspace, auto_cleanup_on_error=True):
                new_dir = self.workspace / "new_project" / "sub"
                new_dir.mkdir(parents=True)
                new_file = new_dir / "app.py"
                new_file.write_text("print('hello')", encoding="utf-8")
                track_created_file(new_file)
                self.assertTrue(new_file.exists())
                raise ValueError("Simulated program failure")

        self.assertFalse((self.workspace / "new_project").exists())
        self.assertTrue(self.pre_file.exists())

    def test_cleanup_preserves_preexisting_files_and_directories(self):
        tracker = SessionWorkspaceTracker(workspace_root=self.workspace)
        with tracker:
            new_file = self.workspace / "existing_dir" / "new_child.txt"
            new_file.write_text("child content", encoding="utf-8")
            track_created_file(new_file)

            tracker.cleanup(reason="Manual stop on error")

        self.assertFalse(new_file.exists())
        self.assertTrue((self.workspace / "existing_dir").exists())
        self.assertTrue(self.pre_file.exists())
        self.assertEqual(self.pre_file.read_text(encoding="utf-8"), "initial content")

    def test_cleanup_reverts_modified_preexisting_files(self):
        with self.assertRaises(Exception):
            with session_cleanup_scope(workspace_root=self.workspace, auto_cleanup_on_error=True):
                track_modified_file(self.pre_file, self.pre_file.read_text(encoding="utf-8"))
                self.pre_file.write_text("corrupted content", encoding="utf-8")

                new_file = self.workspace / "temp.txt"
                new_file.write_text("temp", encoding="utf-8")
                track_created_file(new_file)

                raise Exception("Stop on error")

        self.assertTrue(self.pre_file.exists())
        self.assertEqual(self.pre_file.read_text(encoding="utf-8"), "initial content")
        self.assertFalse((self.workspace / "temp.txt").exists())

    def test_no_cleanup_on_success(self):
        with session_cleanup_scope(workspace_root=self.workspace, auto_cleanup_on_error=True):
            new_file = self.workspace / "success.txt"
            new_file.write_text("success content", encoding="utf-8")
            track_created_file(new_file)

        self.assertTrue((self.workspace / "success.txt").exists())
        self.assertEqual((self.workspace / "success.txt").read_text(encoding="utf-8"), "success content")

    def test_external_command_created_files_cleaned_up(self):
        """Files created directly without explicit tracking are caught by snapshot diff."""
        tracker = SessionWorkspaceTracker(workspace_root=self.workspace)
        with tracker:
            untracked_dir = self.workspace / "untracked_project"
            untracked_dir.mkdir()
            untracked_file = untracked_dir / "output.bin"
            untracked_file.write_bytes(b"binary data")

            self.assertTrue(untracked_file.exists())
            tracker.cleanup(reason="External command failed")

        self.assertFalse(untracked_dir.exists())
        self.assertFalse(untracked_file.exists())

    def test_nested_session_scopes(self):
        outer_file = self.workspace / "outer.txt"
        inner_file = self.workspace / "inner.txt"

        # Inner scope errors and cleans up its own files; outer continues
        with session_cleanup_scope(workspace_root=self.workspace, auto_cleanup_on_error=True):
            outer_file.write_text("outer", encoding="utf-8")
            track_created_file(outer_file)

            try:
                with session_cleanup_scope(workspace_root=self.workspace, auto_cleanup_on_error=True):
                    inner_file.write_text("inner", encoding="utf-8")
                    track_created_file(inner_file)
                    raise RuntimeError("Inner failed")
            except RuntimeError:
                pass

            self.assertFalse(inner_file.exists())
            self.assertTrue(outer_file.exists())

        self.assertTrue(outer_file.exists())

    def test_idempotent_cleanup(self):
        tracker = SessionWorkspaceTracker(workspace_root=self.workspace)
        with tracker:
            new_file = self.workspace / "once.txt"
            new_file.write_text("once", encoding="utf-8")
            track_created_file(new_file)

            res1 = tracker.cleanup(reason="first")
            self.assertIn(str(new_file), res1["deleted_files"])

            res2 = tracker.cleanup(reason="second")
            self.assertTrue(res2.get("already_cleaned"))
            self.assertEqual(res2["deleted_files"], [])

    def test_write_file_and_edit_file_integrated_cleanup(self):
        from agent_execution.policy import execution_scope
        from base import write_file, edit_file

        with execution_scope(workspace=self.workspace):
            with self.assertRaises(RuntimeError):
                with session_cleanup_scope(workspace_root=self.workspace, auto_cleanup_on_error=True):
                    res = write_file("generated.txt", "content from write_file")
                    self.assertTrue(res.get("success"))
                    self.assertTrue((self.workspace / "generated.txt").exists())

                    # edit pre-existing file
                    edit_res = edit_file("existing_dir/pre.txt", "initial content", "modified content")
                    self.assertEqual(edit_res.get("action"), "edited")
                    self.assertEqual(self.pre_file.read_text(encoding="utf-8"), "modified content")

                    raise RuntimeError("Action failed; stop execution")

            # Newly written file must be deleted
            self.assertFalse((self.workspace / "generated.txt").exists())
            # Pre-existing file must be reverted
            self.assertTrue(self.pre_file.exists())
            self.assertEqual(self.pre_file.read_text(encoding="utf-8"), "initial content")

    def test_run_command_stop_on_error_triggers_cleanup(self):
        from agent_execution.policy import execution_scope
        from base import run_command, write_file, auto_approve_command_runs

        with execution_scope(workspace=self.workspace), auto_approve_command_runs():
            tracker = SessionWorkspaceTracker(workspace_root=self.workspace)
            with tracker:
                write_file("before_cmd.txt", "temp data")
                self.assertTrue((self.workspace / "before_cmd.txt").exists())

                # Run a failing command with stop_on_error=True
                res = run_command(["cmd.exe", "/c", "exit 1"], stop_on_error=True)
                self.assertEqual(res.returncode, 1)

            # File created before failing command must be deleted by session cleanup
            self.assertFalse((self.workspace / "before_cmd.txt").exists())
            self.assertTrue(self.pre_file.exists())

    def test_run_agent_error_triggers_cleanup(self):
        from agent_execution.policy import execution_scope
        from base import write_file
        from server import run_agent

        def failing_impl(*args, **kwargs):
            write_file("agent_generated.txt", "agent created this")
            return "Error: Agent execution limit reached."

        with execution_scope(workspace=self.workspace):
            with patch("server._run_agent_impl", side_effect=failing_impl):
                reply = run_agent([{"role": "user", "content": "do task"}])
                self.assertTrue(reply.startswith("Error:"))

            # Must be deleted because run_agent returned error
            self.assertFalse((self.workspace / "agent_generated.txt").exists())
            self.assertTrue(self.pre_file.exists())
