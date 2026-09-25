import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from memory import MemoryStore
from skills import SkillExecutor, SkillMatcher, SkillRegistry, SkillValidationError, load_skill


def write_skill(root: Path, name: str = "read_note", metadata: dict | None = None) -> Path:
    skill_dir = root / name
    skill_dir.mkdir()
    payload = metadata or {
        "name": name,
        "description": "Read a note file.",
        "triggers": ["read note"],
        "inputs": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        "allowed_tools": ["read_file"],
        "execution": {
            "mode": "tool_sequence",
            "steps": [{"tool": "read_file", "args": {"path": "{{path}}"}}],
        },
    }
    (skill_dir / "skill.json").write_text(json.dumps(payload), encoding="utf-8")
    (skill_dir / "SKILL.md").write_text("# Read Note\n", encoding="utf-8")
    return skill_dir


class SkillTests(unittest.TestCase):
    def test_load_skill_validates_metadata(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            skill_dir = write_skill(Path(temp_dir))

            skill = load_skill(skill_dir)

        self.assertEqual(skill.name, "read_note")
        self.assertEqual(skill.allowed_tools, ["read_file"])

    def test_load_skill_rejects_invalid_name(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            skill_dir = write_skill(Path(temp_dir), metadata={
                "name": "Bad Name",
                "description": "bad",
                "triggers": [],
                "inputs": {"type": "object", "properties": {}},
                "allowed_tools": [],
                "execution": {"mode": "tool_sequence", "steps": []},
            })

            with self.assertRaises(SkillValidationError):
                load_skill(skill_dir)

    def test_registry_lists_skills_in_order(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_skill(root, "z_skill")
            write_skill(root, "a_skill")

            skills = SkillRegistry(root).list()

        self.assertEqual([skill.name for skill in skills], ["a_skill", "z_skill"])

    def test_matcher_uses_triggers_and_names(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_skill(root)
            matcher = SkillMatcher(SkillRegistry(root))

            trigger_matches = matcher.match("please read note for me")
            name_matches = matcher.match("run read_note")

        self.assertEqual(trigger_matches[0].reason, "trigger:read note")
        self.assertEqual(name_matches[0].reason, "name:read_note")

    def test_executor_runs_tool_sequence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            skill = load_skill(write_skill(Path(temp_dir)))
            calls = []

            def read_file(path):
                calls.append(path)
                return "hello"

            result = SkillExecutor({"read_file": read_file}).execute(skill, {"path": "note.txt"})

        self.assertTrue(result.success)
        self.assertEqual(calls, ["note.txt"])
        self.assertEqual(result.steps[0].output, "hello")

    def test_executor_rejects_missing_required_input(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            skill = load_skill(write_skill(Path(temp_dir)))

            result = SkillExecutor({"read_file": lambda path: ""}).execute(skill, {})

        self.assertFalse(result.success)
        self.assertIn("Missing required input", result.error)

    def test_executor_logs_memory_events(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            skill = load_skill(write_skill(root))
            store = MemoryStore(root / "memory.db")
            episode_id = store.start_episode()

            result = SkillExecutor(
                {"read_file": lambda path: "hello"},
                memory=store,
                episode_id=episode_id,
            ).execute(skill, {"path": "note.txt"})
            events = store.episode_events(episode_id)

        self.assertTrue(result.success)
        self.assertEqual(
            [event["event_type"] for event in events],
            ["skill_selected", "skill_step", "skill_step_result", "skill_result"],
        )

    def test_executor_logs_completed_process_outputs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            metadata = {
                "name": "run_echo",
                "description": "Run echo.",
                "triggers": ["echo"],
                "inputs": {"type": "object", "properties": {}},
                "allowed_tools": ["run_command"],
                "execution": {
                    "mode": "tool_sequence",
                    "steps": [{"tool": "run_command", "args": {"command": ["echo", "hello"]}}],
                },
            }
            skill = load_skill(write_skill(Path(temp_dir), "run_echo", metadata))
            store = MemoryStore(Path(temp_dir) / "memory.db")
            episode_id = store.start_episode()

            SkillExecutor(
                {
                    "run_command": lambda command: subprocess.CompletedProcess(
                        args=command,
                        returncode=0,
                        stdout="hello",
                        stderr="",
                    )
                },
                memory=store,
                episode_id=episode_id,
            ).execute(skill, {})
            event = [item for item in store.episode_events(episode_id) if item["event_type"] == "skill_step_result"][0]

        self.assertEqual(event["metadata"]["result"]["output"]["returncode"], 0)


if __name__ == "__main__":
    unittest.main()
