# Python AI Mini Agent

A small command-line agent that connects to a remote Ollama-compatible LiteLLM endpoint using the OpenAI Python client. The agent can call a few local file and command helper tools through model tool calls.

## Requirements

- Python 3.11 or newer
- `openai` Python package

Install dependencies:

```powershell
python -m pip install openai
python download_model.py
```

Optional Laya guard layer:

```powershell
python -m pip install laya
```

When installed, Laya classifies each user request before it is sent to the agent and prints a warning for risky or confirmation-worthy requests. By default, the guard loads the local checkpoint from `models/laya-english`. Set `LAYA_MODEL_DIR` to use a different local model path. If Laya is not installed or cannot load, the agent continues without the guard.

## Configuration

Create a local `.env` file in the project root:

```env
OLLAMA_BASE_URL=https://api.example.com/
OLLAMA_MODEL=your-model-name
OLLAMA_EMBEDDING_MODEL=your-embedding-model-name
OLLAMA_API_KEY=sk-your-litellm-virtual-key
SERVER_HOST=127.0.0.1
SERVER_PORT=8000
```

`OLLAMA_API_KEY` must be a LiteLLM virtual key that starts with `sk-`. `OLLAMA_EMBEDDING_MODEL` is optional and defaults to `OLLAMA_MODEL`. The `.env` file is ignored by Git and should not be committed.

## Usage

Run the mini agent:

```powershell
python .\server.py
```

Type a message at the `You:` prompt. Type `exit` or `quit` to stop.

## Entry Points

Use `server.py` when you want to chat with the agent directly from the local command line. It starts the interactive `You:` prompt, builds the shared agent runtime, loads Skills and memory, and runs the background memory review worker when this process can acquire the worker lease.

Use `telegram_polling_worker.py` when you want Telegram integration during local development or on a machine without a public HTTPS URL. It calls Telegram `getUpdates` from your machine, queues incoming text messages, runs them through the shared agent runtime, and sends replies back to the Telegram chat. This mode does not require ngrok, a public IP, or webhook registration.

Use `communication_server.py` when Telegram should deliver messages to the agent through HTTP webhooks. It starts a small HTTP server with these endpoints:

- `POST /webhooks/telegram`: verifies and queues Telegram webhook updates, then lets the communication worker process pending commands.
- `GET /health`: returns a lightweight health response for local checks or tunnel monitoring.

`communication_server.py` creates the same agent runtime used by `server.py`, starts background processing for queued Telegram messages, and should be used together with a public tunnel or public host when Telegram webhook mode is enabled.

In short:

- Local terminal chat: run `python .\server.py`.
- Telegram long polling without a public URL: run `python .\telegram_polling_worker.py`.
- Telegram webhook with a public URL or tunnel: run `python .\communication_server.py`.

## Telegram Webhook

Create a Telegram bot with `@BotFather`, then put the bot token in `.env`. For local testing without a public URL, use long polling:

```env
COMMUNICATION_DB_PATH=communication/communication.db
TELEGRAM_BOT_TOKEN=123456789:your-bot-token
TELEGRAM_POLL_TIMEOUT=30
TELEGRAM_POLL_IDLE_SLEEP=0.2
COMM_ALLOWED_SENDERS=telegram:123456789
```

`COMM_ALLOWED_SENDERS` is optional for local testing, but should be set before regular use. Use Telegram's numeric user id, formatted as `telegram:<sender_id>`.

Start the long polling worker:

```powershell
python .\telegram_polling_worker.py
```

This mode does not require ngrok, a public IP, or webhook registration because the worker calls Telegram `getUpdates` from your machine.

If you prefer webhook mode, also set a webhook secret and host/port:

```env
COMMUNICATION_DB_PATH=communication/communication.db
COMMUNICATION_HOST=127.0.0.1
COMMUNICATION_PORT=8000
TELEGRAM_BOT_TOKEN=123456789:your-bot-token
TELEGRAM_WEBHOOK_SECRET=your-random-secret
COMM_ALLOWED_SENDERS=telegram:123456789
```

Start the communication server:

```powershell
python .\communication_server.py
```

Expose it with a tunnel such as ngrok:

```powershell
ngrok http 8000
```

Register the Telegram webhook with the public HTTPS URL:

```powershell
curl.exe -X POST "https://api.telegram.org/bot$env:TELEGRAM_BOT_TOKEN/setWebhook" `
  -H "Content-Type: application/json" `
  -d "{\"url\":\"https://your-tunnel.example/webhooks/telegram\",\"secret_token\":\"$env:TELEGRAM_WEBHOOK_SECRET\"}"
```

Check webhook status:

```powershell
curl.exe "https://api.telegram.org/bot$env:TELEGRAM_BOT_TOKEN/getWebhookInfo"
```

If you need your Telegram sender id before enabling `COMM_ALLOWED_SENDERS`, temporarily leave it empty, send a message to the bot, then inspect the communication database or recent inbound rows. After confirming the id, set `COMM_ALLOWED_SENDERS` and restart the server.


## New Features (v0.2+)

- **Web Management Interface**: A beautiful, modern Agent Observatory dashboard is served at the root / of communication_server.py. It displays system health, active memories, and task queues.
- **Cronjob Scheduling**: Background tasks are handled by scheduler.py with an SQLite backend. It includes a built-in maintenance.summary job that generates a markdown report using the local LLM.
- **Memory Natural Language Correction**: The agent has a manage_memory tool to directly correct, supersede, contradict, or archive memories when instructed. High-risk memory operations require a two-stage user confirmation.
- **Skill Management CLI**: Use python .\skills_cli.py to list, inspect, enable, disable, and validate local skills.
- **Multi-Platform Communication**: In addition to Telegram, the communication_server.py now supports Discord and LINE webhook integrations.
- **Role-Based Access Control (RBAC)**: Support for iewer, operator, and dmin roles. Set the ROLES_CONFIG environment variable to point to a JSON file mapping sender IDs to roles to restrict access to tools and skills.

## Memory

The agent records episodic memory in a local SQLite database at `memory\memory.db`. This file is ignored by Git. Set `MEMORY_DB_PATH` to use a different database location.

The memory store also supports semantic memories as auditable subject-predicate-object facts linked back to source episode events.

Procedural memories store reusable workflows with success and failure counts linked back to source episodes.

Memory review and embedding backfill run as bounded background jobs. The job queue is stored in SQLite, so pending review and embedding work can resume after restarting the agent. Provider-wide 429 cooldown state is also stored in SQLite and honors `Retry-After` before retrying background memory work.

When multiple entry points run at the same time, such as `server.py` and `telegram_polling_worker.py`, only one process acquires the background memory worker lease. Other processes continue to handle Agent turns and enqueue review jobs, but leave background memory review and embedding backfill work to the lease owner.

Inspect memory from the command line:

```powershell
python .\observability.py overview
python .\observability.py episode --episode-id 1
python .\observability.py messages --limit 50
python .\observability.py messages --episode-id 1
python .\observability.py review-candidates --limit 20
python .\observability.py procedures
python .\observability.py procedures --name run_command
python .\observability.py facts --memory-type user_profile
python .\observability.py low-confidence --threshold 0.35
python .\observability.py conflicts
python .\observability.py memory-health
python .\observability.py embedding-candidates --limit 20
python .\observability.py embedding-candidates --stale-before "2026-02-01 00:00:00"
python .\observability.py backfill-embeddings --limit 20
python .\observability.py backfill-embeddings --stale-before "2026-02-01 00:00:00"
python .\observability.py archive-fact --memory-id 1 --reason obsolete
python .\observability.py confirm-fact --memory-id 1
python .\observability.py contradict-fact --memory-id 1
python .\observability.py supersede-fact --memory-id 1 --subject user --predicate prefers --object TypeScript --memory-type user_profile
python .\observability.py export
python .\observability.py export-md --path .\memory.md
python .\observability.py import --path .\memory-export.json
```

Useful health fields:

- `memory_review_queue_depth`: pending persisted review jobs ready to run.
- `embedding_backfill_queue_depth`: pending persisted embedding backfill jobs ready to run.
- `embedding_backfill_candidate_depth`: active semantic memories missing embeddings.
- `skipped_missing_embedding_backfills`: retrieval candidates that used local fallback embeddings because foreground remote backfill budget was capped.
- `rate_limit_429_errors`: recent provider 429 errors observed by memory-related operations.

## Cronjob Scheduling

The system includes a cron-like scheduler backed by SQLite. You can manage jobs using the CLI and execute them with the background worker.

To add a new scheduled job:
```powershell
python .\schedule_cli.py add --name "test_job" --cron "*/5 * * * *" --type "command" --payload '{"command": "echo Hello"}' --desc "Test command job"
```

To list all scheduled jobs:
```powershell
python .\schedule_cli.py list
```

*Note: The background scheduler worker runs automatically when you start `server.py`, `communication_server.py`, or `telegram_polling_worker.py`.*

To set up the default daily summary job:
```powershell
python .\add_default_jobs.py
```

## Tools

The agent exposes these local tools to the model:

- `read_file`: read a text file
- `list_files`: list files in a directory
- `write_file`: write text to a file
- `run_command`: run a command after interactive confirmation
- `fetch_url`: fetch a URL using `curl`
- `run_skill`: run a local registered Skill

## Skills

Local Skills live under `skills\<skill_name>\` with a `skill.json` metadata file and a `SKILL.md` instruction file. The first implementation supports deterministic `tool_sequence` Skills that call existing tools through an explicit allowlist.

Inspect Skills and Skill runs from the command line:

```powershell
python .\observability.py skills
python .\observability.py skill --name example_skill
python .\observability.py skill-runs --limit 20
```

## Tests

Run the test suite:

```powershell
python -m unittest discover -s tests -p "test_*.py"
```

Run syntax checks:

```powershell
python -m py_compile base.py server.py forgetting.py laya_guard.py memory.py memory_classifier.py memory_review.py observability.py procedure_similarity.py request_budget.py retrieval.py retrieval_ranker.py semantic_extractor.py skills.py vector_search.py working_memory.py tests\test_base.py tests\test_forgetting.py tests\test_laya_guard.py tests\test_memory.py tests\test_memory_classifier.py tests\test_memory_review.py tests\test_observability.py tests\test_procedure_similarity.py tests\test_retrieval.py tests\test_retrieval_ranker.py tests\test_semantic_extractor.py tests\test_skills.py tests\test_vector_search.py tests\test_server.py tests\test_working_memory.py
```
