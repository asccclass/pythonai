# Python AI Mini Agent

A small command-line agent that connects to a remote Ollama-compatible LiteLLM endpoint using the OpenAI Python client. The agent can call a few local file and command helper tools through model tool calls.

## Requirements

- Python 3.11 or newer
- `fastapi`, `openai`, and `uvicorn` Python packages

Install dependencies:

```powershell
python -m pip install -r requirements.txt
python download_model.py  // 只需要執行一次
```

## Configuration

Create a local `.env` file in the project root:

```env
OLLAMA_BASE_URL=https://api.example.com/
OLLAMA_MODEL=your-model-name
OLLAMA_API_KEY=sk-your-litellm-virtual-key
SERVER_HOST=127.0.0.1
SERVER_PORT=8000
```

`OLLAMA_API_KEY` must be a LiteLLM virtual key that starts with `sk-`. The `.env` file is ignored by Git and should not be committed.

## Usage

Run the OpenAI-compatible HTTP bridge:

```powershell
python .\server.py
```

By default it listens on `http://127.0.0.1:8000`.

Laya custom provider settings:

```json
{
  "id": "pythonai-agent",
  "provider_type": "openai_compatible",
  "base_url": "http://127.0.0.1:8000",
  "capabilities_override": {
    "supports_tool_calling": false,
    "supports_structured_output": false
  }
}
```

If you want the original terminal chat loop from Python, run:

```powershell
python -c "import server; server.run_cli()"
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
python -m unittest
```

Run syntax checks:

```powershell
python -m py_compile base.py server.py test_base.py test_server.py
```
