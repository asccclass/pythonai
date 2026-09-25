# Python AI Mini Agent

A small command-line agent that connects to a remote Ollama-compatible LiteLLM endpoint using the OpenAI Python client. The agent can call a few local file and command helper tools through model tool calls.

## Requirements

- Python 3.11 or newer
- `openai` Python package

Install dependencies:

```powershell
python -m pip install openai
python download_model.py  // 只需要執行一次
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

## Memory

The agent records episodic memory in a local SQLite database at `memory\memory.db`. This file is ignored by Git. Set `MEMORY_DB_PATH` to use a different database location.

The memory store also supports semantic memories as auditable subject-predicate-object facts linked back to source episode events.

Procedural memories store reusable workflows with success and failure counts linked back to source episodes.

Inspect memory from the command line:

```powershell
python .\observability.py overview
python .\observability.py episode --episode-id 1
python .\observability.py messages --limit 50
python .\observability.py messages --episode-id 1
```

## Tools

The agent exposes these local tools to the model:

- `read_file`: read a text file
- `list_files`: list files in a directory
- `write_file`: write text to a file
- `run_command`: run a command after interactive confirmation

## Tests

Run the test suite:

```powershell
python -m unittest discover -s tests -p "test_*.py"
```

Run syntax checks:

```powershell
python -m py_compile base.py server.py forgetting.py laya_guard.py memory.py memory_classifier.py memory_review.py observability.py procedure_similarity.py request_budget.py retrieval.py retrieval_ranker.py semantic_extractor.py vector_search.py working_memory.py tests\test_base.py tests\test_forgetting.py tests\test_laya_guard.py tests\test_memory.py tests\test_memory_classifier.py tests\test_memory_review.py tests\test_observability.py tests\test_procedure_similarity.py tests\test_retrieval.py tests\test_retrieval_ranker.py tests\test_semantic_extractor.py tests\test_vector_search.py tests\test_server.py tests\test_working_memory.py
```
