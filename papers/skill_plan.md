# Bot Skill Execution Architecture Plan

## Overview

This plan adds a Skill execution system to the bot. A Skill is a reusable, named capability package that can describe when it should be used, what inputs it needs, what steps it performs, and which existing tools or local commands it may call.

The first version should be conservative: Skills should be explicit, auditable, easy to inspect, and routed through the same safety boundaries as normal tool use. The bot should not treat Skills as hidden magic. A Skill execution should be visible in episodic memory, guarded by Laya where local actions are involved, and testable without calling the remote LLM.

## Goals

- Let the bot discover available Skills from a local directory.
- Let the assistant select and execute a Skill when the user request matches a registered Skill.
- Keep Skill execution structured, logged, and reviewable.
- Reuse existing tools such as `read_file`, `list_files`, `write_file`, and `run_command`.
- Support future procedural memory integration so successful Skill runs can become reusable workflows.
- Avoid creating new unbounded LLM request paths.

## Non-Goals for the First Version

- Do not install third-party plugins or remote Skills.
- Do not run arbitrary Python modules from Skills without an explicit allowlist.
- Do not let Skills bypass Laya guard or command confirmation.
- Do not add autonomous multi-step planning beyond the declared Skill steps.
- Do not build a UI; command-line observability is enough for the first milestone.

## Proposed Directory Layout

```text
skills/
  example_skill/
    SKILL.md
    skill.json
    scripts/
    templates/
```

Recommended first-version files:

| File | Required | Purpose |
|---|---:|---|
| `skill.json` | Yes | Machine-readable metadata, input schema, allowed tools, and execution mode. |
| `SKILL.md` | Yes | Human-readable instructions and operational notes. |
| `scripts/` | Optional | Local scripts explicitly referenced by `skill.json`. |
| `templates/` | Optional | Static reusable files used by the Skill. |

## Skill Metadata Shape

Initial `skill.json` shape:

```json
{
  "name": "example_skill",
  "description": "Short user-facing description.",
  "triggers": ["keyword", "task phrase"],
  "inputs": {
    "type": "object",
    "properties": {
      "path": {"type": "string"}
    },
    "required": ["path"]
  },
  "allowed_tools": ["read_file", "list_files"],
  "execution": {
    "mode": "tool_sequence",
    "steps": [
      {"tool": "read_file", "args": {"path": "{{path}}"}}
    ]
  }
}
```

Supported first-version execution modes:

| Mode | Meaning | First-Version Support |
|---|---|---:|
| `tool_sequence` | Execute declared existing tools in order. | Yes |
| `script` | Run an allowlisted local script through `run_command`. | Optional |
| `instruction_only` | Inject Skill instructions into the assistant context. | Optional |
| `llm_plan` | Let the LLM dynamically plan Skill steps. | No |

## Architecture Components

| Component | Responsibility |
|---|---|
| `SkillRegistry` | Load, validate, and list Skills from `skills/`. |
| `SkillMatcher` | Match a user request to candidate Skills using deterministic triggers first. |
| `SkillExecutor` | Execute validated Skill steps through existing tool functions. |
| `SkillResult` | Return structured success/failure output, tool results, and audit metadata. |
| Observability helpers | List Skills, inspect Skill metadata, and inspect recent Skill runs. |
| Memory integration | Log Skill selection, steps, tool results, and failures as episode events. |

## Execution Flow

1. User sends a request.
2. Laya guard assesses the user request as it does today.
3. Memory retrieval injects relevant context.
4. Skill matching runs deterministic trigger matching against local Skill metadata.
5. If one clear Skill matches, the bot may expose the Skill to the LLM as a callable tool or execute it through an explicit `run_skill` tool call.
6. `SkillExecutor` validates inputs against the Skill schema.
7. Each Skill step is checked against `allowed_tools`.
8. Local command steps still go through existing `run_command` guard and confirmation behavior.
9. Results are returned to the assistant and logged to memory.
10. On failure, the Skill run records the failed step and error without continuing unsafe steps.

## Safety Rules

- Skills must be local files under the configured `skills/` root.
- Skill names must be simple identifiers such as `lower_snake_case`.
- A Skill may only call tools listed in its own `allowed_tools`.
- A Skill step may not call `delete_file` or `run_command` unless the metadata explicitly allows it.
- `run_command` Skill steps must use fixed command arrays or validated template variables.
- Path inputs must be normalized and must not silently escape intended directories when a Skill declares a workspace boundary.
- Skill execution must log all selected Skills, inputs, tool calls, results, and errors.
- Skill failure should not terminate the whole chat session unless the main assistant cannot continue.

## Laya Boundary

| Decision Area | Let Laya Decide? | Final Writer |
|---|---:|---|
| Whether the user request is risky | Yes | Main loop |
| Whether a local command needs confirmation | Yes | `run_command` |
| Whether a Skill can bypass guard checks | No | Never allowed |
| Whether a Skill is relevant | Optional signal later | Deterministic matcher first |
| Whether Skill results should become procedural memory | Yes, as a candidate signal | Memory service |

## Memory Integration

Add new episode event types:

| Event Type | Content |
|---|---|
| `skill_candidates` | Candidate Skill names and match reasons. |
| `skill_selected` | Selected Skill name, version/hash, and inputs. |
| `skill_step` | Step index, tool name, and sanitized arguments. |
| `skill_step_result` | Step index, success flag, output summary, and error if any. |
| `skill_result` | Final status and summary. |

Skill runs should also become candidates for procedural memory when:

- The Skill completed successfully.
- The task is likely reusable.
- The Skill involved multiple local tool steps.

## Observability

Add command-line inspection commands:

```powershell
python .\observability.py skills
python .\observability.py skill --name example_skill
python .\observability.py skill-runs --limit 20
```

The first version can print JSON like existing memory inspection commands.

## Implementation Phases

| Phase | Goal | Main Deliverables | Validation |
|---|---|---|---|
| 1 | Define local Skill format | Add `skills/` conventions, `skill.json` schema, and one fixture Skill for tests. | Unit tests validate good and bad metadata. |
| 2 | Build registry | Add `SkillRegistry` to load Skills from disk and expose list/get APIs. | Tests cover missing files, invalid JSON, duplicate names, and deterministic ordering. |
| 3 | Add deterministic matcher | Match user input against Skill triggers and names. | Tests cover exact trigger, no match, and multiple candidates. |
| 4 | Add executor | Execute `tool_sequence` Skills through existing tool functions. | Tests mock tools and verify step order, input templating, allowlist enforcement, and failure handling. |
| 5 | Add bot tool surface | Add a `run_skill` tool schema or explicit pre-agent Skill execution path. | Tests verify the assistant can call a Skill without bypassing tool validation. |
| 6 | Add memory logging | Record candidates, selection, steps, results, and errors as episode events. | Tests verify audit events are written in order. |
| 7 | Add observability | Add CLI commands for Skills and Skill runs. | Tests verify JSON output and filtering. |
| 8 | Add docs and examples | Document Skill authoring and add a minimal example Skill. | README or paper examples are runnable in tests. |

## First Milestone Recommendation

The first milestone should avoid dynamic LLM planning. Implement only:

1. `skill.json` validation.
2. Registry loading from `skills/`.
3. Deterministic trigger matching.
4. `tool_sequence` execution.
5. Memory event logging.
6. Observability commands.

This gives the bot useful Skill execution without adding a large new attack surface or another source of frequent remote LLM calls.

## Open Questions

- Should Skill matching happen before the main LLM call, or should the LLM call a `run_skill` tool after seeing available Skill summaries?
- Should Skills be allowed to ask follow-up questions when required inputs are missing, or should the main assistant handle that first?
- Should Skill directories be versioned with a content hash in memory events?
- Should `script` mode be delayed until there is a stronger sandbox and allowlist policy?
- Should successful Skill runs update procedural memory automatically, or only create review candidates?

## Design Rules

- Prefer deterministic Skill loading, matching, and execution over LLM-driven hidden behavior.
- Keep Skills auditable: every run should have source metadata and episode events.
- Reuse existing tools and guardrails instead of creating a parallel execution path.
- Make unsupported or unsafe Skill metadata fail closed.
- Keep remote LLM usage optional and budgeted; Skill execution itself should not require extra remote requests.
