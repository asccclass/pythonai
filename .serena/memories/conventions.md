# Repository-specific conventions
- AGENTS.md: put new programs, requirements, config, tests and resources in a dedicated directory; extend the same existing directory for follow-up work. Legacy root files do not authorize scattering new standalone programs there.
- Python uses snake_case functions/modules, CapWords classes, type annotations/dataclasses in newer runtime contracts. Existing tests mix unittest.TestCase/mock and pytest; follow the touched module instead of bulk reformatting older compact code.
- Model-facing text lives in agent_configuration/data UTF-8 Markdown/JSON. AGENT_CONFIG_DIR overrides the entire directory; no implicit partial fallback. Paths resolve against package location, not current working directory.
- Configuration reloads at task/checkpoint/model-description boundaries. Templates use string.Template ${name}, with literal replacement content. Text edits must not grant roles, tools, approval or workspace privileges; those remain code-enforced. Details: agent_configuration/README.md.
- Test state uses temporary directories/SQLite plus patched environment/provider doubles; tests must not consume real updates, modify real conversations, or call configured providers accidentally.
- Never commit .env, credentials, local databases, downloaded binaries or model/cache artifacts. .env.example documents names with safe placeholders only.
