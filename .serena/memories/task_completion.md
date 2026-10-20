# Completion and verification
- Every completed change requires a corresponding scoped commit (AGENTS.md). Code edits require added/updated relevant regression tests and successful relevant validation; documentation-only edits do not require test code.
- Inspect git status --short --untracked-files=all before/after; stage explicit reviewed paths only, preserving unrelated work.
- Local Python commands MUST use the project venv:
```powershell
.\venv\Scripts\python.exe -m pytest <relevant-test-path> -q
.\venv\Scripts\python.exe -m pytest -q
.\venv\Scripts\python.exe -m agent_execution.operations syntax
.\venv\Scripts\python.exe -m pip check
git diff --check
git diff --cached --check
```
- pytest.ini is the source of truth for collection: tests, agent_execution/tests, user_profile/tests, turn_control/tests, temporal_backend/tests, agent_configuration/tests, harness_probe/tests; ignored temp experiments must stay excluded.
- Execution-wide changes: operations benchmark is an additional CI check. IDE edits: run npm test and applicable npm run test:host under vscode-extension.
- .github/workflows/verification.yml separates Windows unit/native, Docker/Chromium, VS Code Host and manual external-provider checks. Run optional checks only for relevant changed capabilities; report unavailable/skipped checks explicitly.
- Report unit/static results separately from real provider, Docker/browser, device, platform delivery, migration/restart and remote CI evidence. /health proves liveness only. No push/deployment is implied by a local commit requirement.
