---
name: mybrain_query_cli
description: Query the local MyBrain knowledge base by running mybrain.exe directly with --query. Use when the user asks Codex/Agent to answer from "MyBrain", "my local knowledge base", "我的筆記", "知識庫", "wiki", "過往筆記", or wants command-line access to MyBrain without using the browser UI or MCP.
---

# MyBrain CLI Query

Use the bundled `scripts/mybrain.exe` directly as a command-line knowledge query tool.

## Query Workflow

1. Change to this Skill's `scripts` directory, then run the bundled `mybrain.exe` directly so the executable can read the adjacent `envfile`:

   ```shell
   ./mybrain.exe --query "問題"
   ```

   On Windows PowerShell, use:

   ```powershell
   Set-Location -LiteralPath .\scripts
   .\mybrain.exe --query "問題"
   ```

2. Read the command output as the answer. The binary prints progress logs to stderr and the final answer to stdout.

3. If the command fails because paths are missing, inspect `scripts/envfile`. It must define working `WIKI_PATH`, `RAW_PATH`, `OLLAMA_URL`, and `OLLAMA_MODEL` values. Do not invent answers from model memory when MyBrain query fails.

## Direct Command

The required command form is:

```shell
./mybrain.exe --query "問題"
```

Do not route queries through any helper script, wrapper, package script, or other program. Invoke the bundled `scripts/mybrain.exe` itself from the `scripts` directory.

## Expected Deployment Files

This Skill is self-contained for CLI-only querying. Keep these files together in the Skill directory when copying it to another Agent:

```text
SKILL.md
agents/
scripts/
  mybrain.exe
  envfile
```

The `WIKI_PATH` and `RAW_PATH` in `scripts/envfile` can point to an external data repo such as `mybraindata/wiki` and `mybraindata/raw`.
When moving this Skill to another machine, update `scripts/envfile` so `WIKI_PATH`, `RAW_PATH`, `OLLAMA_URL`, and `OLLAMA_MODEL` match that environment.

For full web/MCP use, also keep:

```text
static/
templates/
favicon.ico
```

## Answering Rules

- Treat MyBrain output as the source of truth for user questions about their notes or knowledge base.
- Preserve `[[wikilink]]` citations returned by the tool.
- If the tool says the knowledge base has no record, say that clearly before adding any general background.
- Do not expose or guess credentials. If sensitive values are returned, relay only what the user explicitly asked for and avoid broad dumps.
