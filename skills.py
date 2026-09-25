from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import re
import subprocess
from pathlib import Path
from string import Template
from typing import Any, Callable


DEFAULT_SKILLS_DIR = Path(__file__).resolve().parent / "skills"
SKILL_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
SUPPORTED_EXECUTION_MODES = {"tool_sequence"}


class SkillError(Exception):
    pass


class SkillValidationError(SkillError):
    pass


class SkillExecutionError(SkillError):
    pass


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    triggers: list[str]
    inputs: dict[str, Any]
    allowed_tools: list[str]
    execution: dict[str, Any]
    path: Path
    instructions: str

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["path"] = str(self.path)
        return payload


@dataclass(frozen=True)
class SkillMatch:
    skill: Skill
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.skill.name,
            "description": self.skill.description,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class SkillStepResult:
    index: int
    tool: str
    args: dict[str, Any]
    success: bool
    output: Any = None
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_log_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["output"] = loggable_value(self.output)
        return payload


@dataclass(frozen=True)
class SkillResult:
    skill_name: str
    success: bool
    steps: list[SkillStepResult]
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill_name": self.skill_name,
            "success": self.success,
            "steps": [step.to_dict() for step in self.steps],
            "error": self.error,
        }


class SkillRegistry:
    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root) if root is not None else DEFAULT_SKILLS_DIR

    def list(self) -> list[Skill]:
        if not self.root.exists():
            return []
        skills = []
        seen_names: set[str] = set()
        for path in sorted(item for item in self.root.iterdir() if item.is_dir()):
            skill = load_skill(path)
            if skill.name in seen_names:
                raise SkillValidationError(f"Duplicate skill name: {skill.name}")
            seen_names.add(skill.name)
            skills.append(skill)
        return skills

    def get(self, name: str) -> Skill:
        for skill in self.list():
            if skill.name == name:
                return skill
        raise KeyError(name)


class SkillMatcher:
    def __init__(self, registry: SkillRegistry) -> None:
        self.registry = registry

    def match(self, text: str) -> list[SkillMatch]:
        normalized = text.lower()
        matches = []
        for skill in self.registry.list():
            if skill.name.lower() in normalized:
                matches.append(SkillMatch(skill, f"name:{skill.name}"))
                continue
            for trigger in skill.triggers:
                if trigger.lower() in normalized:
                    matches.append(SkillMatch(skill, f"trigger:{trigger}"))
                    break
        return matches


class SkillExecutor:
    def __init__(
        self,
        tools: dict[str, Callable[..., Any]],
        memory: Any | None = None,
        episode_id: int | None = None,
    ) -> None:
        self.tools = tools
        self.memory = memory
        self.episode_id = episode_id

    def execute(self, skill: Skill, inputs: dict[str, Any]) -> SkillResult:
        try:
            validate_inputs(skill, inputs)
        except SkillValidationError as error:
            self._log("skill_result", metadata={"skill": skill.name, "success": False, "error": str(error)})
            return SkillResult(skill.name, False, [], str(error))

        self._log("skill_selected", metadata={"skill": skill.name, "inputs": inputs})
        steps: list[SkillStepResult] = []
        for index, step in enumerate(skill.execution.get("steps", []), start=1):
            tool_name = str(step.get("tool", ""))
            try:
                if tool_name not in skill.allowed_tools:
                    raise SkillExecutionError(f"Tool is not allowed for skill {skill.name}: {tool_name}")
                if tool_name not in self.tools:
                    raise SkillExecutionError(f"Tool is not registered: {tool_name}")
                args = render_args(step.get("args", {}), inputs)
                self._log("skill_step", metadata={"skill": skill.name, "index": index, "tool": tool_name, "args": args})
                output = self.tools[tool_name](**args)
                result = SkillStepResult(index, tool_name, args, True, output=output)
                steps.append(result)
                self._log("skill_step_result", metadata={"skill": skill.name, "result": result.to_log_dict()})
            except Exception as error:
                result = SkillStepResult(index, tool_name, {}, False, error=str(error))
                steps.append(result)
                self._log("skill_step_result", metadata={"skill": skill.name, "result": result.to_log_dict()})
                self._log("skill_result", metadata={"skill": skill.name, "success": False, "error": str(error)})
                return SkillResult(skill.name, False, steps, str(error))

        self._log("skill_result", metadata={"skill": skill.name, "success": True, "step_count": len(steps)})
        return SkillResult(skill.name, True, steps)

    def _log(self, event_type: str, metadata: dict[str, Any]) -> None:
        if self.memory is None or self.episode_id is None:
            return
        self.memory.add_event(self.episode_id, event_type, metadata=metadata)


def load_skill(path: str | Path) -> Skill:
    skill_path = Path(path)
    metadata_path = skill_path / "skill.json"
    instructions_path = skill_path / "SKILL.md"
    if not metadata_path.exists():
        raise SkillValidationError(f"Missing skill.json: {skill_path}")
    if not instructions_path.exists():
        raise SkillValidationError(f"Missing SKILL.md: {skill_path}")
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise SkillValidationError(f"Invalid skill.json: {error}") from error
    instructions = instructions_path.read_text(encoding="utf-8")
    return validate_skill_metadata(metadata, skill_path, instructions)


def validate_skill_metadata(metadata: dict[str, Any], path: Path, instructions: str) -> Skill:
    name = str(metadata.get("name", "")).strip()
    if not SKILL_NAME_PATTERN.match(name):
        raise SkillValidationError(f"Invalid skill name: {name}")
    description = str(metadata.get("description", "")).strip()
    if not description:
        raise SkillValidationError(f"Skill {name} must have a description")
    triggers = metadata.get("triggers", [])
    if not isinstance(triggers, list) or not all(isinstance(item, str) and item.strip() for item in triggers):
        raise SkillValidationError(f"Skill {name} triggers must be non-empty strings")
    inputs = metadata.get("inputs", {"type": "object", "properties": {}})
    validate_input_schema(name, inputs)
    allowed_tools = metadata.get("allowed_tools", [])
    if not isinstance(allowed_tools, list) or not all(isinstance(item, str) and item for item in allowed_tools):
        raise SkillValidationError(f"Skill {name} allowed_tools must be strings")
    execution = metadata.get("execution")
    if not isinstance(execution, dict):
        raise SkillValidationError(f"Skill {name} must define execution")
    if execution.get("mode") not in SUPPORTED_EXECUTION_MODES:
        raise SkillValidationError(f"Unsupported execution mode for {name}: {execution.get('mode')}")
    steps = execution.get("steps")
    if not isinstance(steps, list):
        raise SkillValidationError(f"Skill {name} execution.steps must be a list")
    for step in steps:
        if not isinstance(step, dict) or not isinstance(step.get("tool"), str):
            raise SkillValidationError(f"Skill {name} has an invalid step")
        if step["tool"] not in allowed_tools:
            raise SkillValidationError(f"Skill {name} step uses non-allowed tool: {step['tool']}")
        if not isinstance(step.get("args", {}), dict):
            raise SkillValidationError(f"Skill {name} step args must be an object")
    return Skill(name, description, triggers, inputs, allowed_tools, execution, path, instructions)


def validate_input_schema(name: str, schema: Any) -> None:
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise SkillValidationError(f"Skill {name} inputs must be an object schema")
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    if not isinstance(properties, dict):
        raise SkillValidationError(f"Skill {name} input properties must be an object")
    if not isinstance(required, list):
        raise SkillValidationError(f"Skill {name} required inputs must be a list")
    for field in required:
        if field not in properties:
            raise SkillValidationError(f"Skill {name} required field is not defined: {field}")


def validate_inputs(skill: Skill, inputs: dict[str, Any]) -> None:
    schema = skill.inputs
    required = schema.get("required", [])
    for field in required:
        if field not in inputs:
            raise SkillValidationError(f"Missing required input for {skill.name}: {field}")
    properties = schema.get("properties", {})
    for key, value in inputs.items():
        expected = properties.get(key, {}).get("type")
        if expected == "string" and not isinstance(value, str):
            raise SkillValidationError(f"Input {key} must be a string")
        if expected == "array" and not isinstance(value, list):
            raise SkillValidationError(f"Input {key} must be an array")
        if expected == "object" and not isinstance(value, dict):
            raise SkillValidationError(f"Input {key} must be an object")


def render_args(value: Any, inputs: dict[str, Any]) -> Any:
    if isinstance(value, str):
        return Template(value.replace("{{", "${").replace("}}", "}")).safe_substitute(inputs)
    if isinstance(value, list):
        return [render_args(item, inputs) for item in value]
    if isinstance(value, dict):
        return {key: render_args(item, inputs) for key, item in value.items()}
    return value


def loggable_value(value: Any) -> Any:
    if isinstance(value, subprocess.CompletedProcess):
        return {
            "args": value.args,
            "returncode": value.returncode,
            "stdout": value.stdout,
            "stderr": value.stderr,
        }
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)
