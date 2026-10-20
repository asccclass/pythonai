# Windows commands (from project root)
```powershell
.\venv\Scripts\python.exe .\server.py
.\venv\Scripts\python.exe .\telegram_polling_worker.py
.\venv\Scripts\python.exe .\communication_server.py
.\venv\Scripts\python.exe -m pytest -q
.\venv\Scripts\python.exe -m pytest agent_configuration/tests -q
.\venv\Scripts\python.exe -m agent_execution.operations doctor
.\venv\Scripts\python.exe -m agent_execution.operations readiness
.\venv\Scripts\python.exe -m agent_execution.operations syntax
.\venv\Scripts\python.exe -m agent_execution.operations benchmark
.\venv\Scripts\python.exe -m pip check
.\venv\Scripts\python.exe .\observability.py memory-health
.\venv\Scripts\python.exe .\schedule_cli.py list
```
- These are available commands, not an instruction to start services, contact a provider, or mutate live data during inspection.
- PowerShell opt-in flags use $env:PYTHONAI_DOCKER_TEST = '1', $env:PYTHONAI_BROWSER_TEST = '1', or $env:PYTHONAI_PROVIDER_TEST = '1'; run only relevant explicitly intended integrations.
- Read Chinese files with Get-Content -Encoding UTF8. Prefer rg for scoped search. Quote Git revspecs such as '@{u}' in PowerShell.
- VS Code extension (working directory vscode-extension): npm ci; npm test; npm run test:host. CI uses Node 24.
- Serena memory reference report: serena memories check (project root).
- If Serena CLI output fails under Windows CP950 on its Unicode checkmark, temporarily set $env:PYTHONIOENCODING = 'utf-8' before serena memories check; restore the prior environment value afterward. This affects CLI output only.
