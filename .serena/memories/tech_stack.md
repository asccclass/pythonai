# Runtime and dependencies
- Windows PowerShell development; CI baseline Python 3.12 (.github/workflows/verification.yml). All local Python tests/validation use .\venv\Scripts\python.exe, never system Python or a sibling project's venv.
- requirements.txt is the pinned full runtime; agent_execution/operations/requirements-ci.txt is the smaller offline test runtime. Do not replace the existing venv merely to match CI.
- OpenAI-compatible Chat Completions via openai SDK, FastAPI/Starlette/Uvicorn ASGI, SQLite durable stores; pytest collects both unittest.TestCase and pytest-style suites.
- Playwright Chromium, Docker, OCR/local neural models are optional capabilities with separate installation/validation boundaries. Dependencies installed do not prove an authenticated remote integration.
- Optional Temporal SDK pins live in agent_execution/temporal_backend/requirements.txt; default SQLite behavior and optional lazy imports must remain intact. See agent_execution/temporal_backend/README.md for current opt-in limitations.
- vscode-extension/ is a separate JavaScript/Node VS Code extension with npm lockfile; its tests are not covered by Python pytest.
