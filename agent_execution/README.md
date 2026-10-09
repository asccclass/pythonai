# Shared execution policy

T01 common policy for model tools, Skill steps, registered Python tools, MCP routing,
subagents and scheduled commands. The context carries source, identity and audit
store through ContextVars. Worker threads must copy the caller context.

Roles are checked on each nested tool call. Skills may grant their own resolved
asset directory only to run_command, never a root supplied in Skill inputs.
Path arguments are resolved against AGENT_WORKSPACE_ROOT (including symlinks).

This is application policy, not an OS sandbox: arbitrary Python, shell programs
and third-party MCP servers still require T02 isolation. Tests live in
agent_execution/tests and are collected by the main test suite.
