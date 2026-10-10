# Incremental code intelligence (T15)

The existing root build_codebase_index.py is a compatibility CLI. Runtime code,
protocol worker, four-language fixtures and the reproducible benchmark live here.
CodeIndex uses the calling task's configured workspace. Plain workspaces retain
codebase_index.db, ignored by Git; task worktrees automatically use an external
workspace-hashed cache beside TaskStore, so read tools cannot dirty a delivery
worktree. Operators can set AGENT_CODE_INDEX_DIR to an absolute external directory
for other repositories; workspace hashes prevent mixing projects. Existing unversioned code_index rows
are discarded on first synchronization, then rebuilt from current allowed files.

refresh hashes source bytes, prunes deleted/ignored/secret files, replaces all
declarations/references for changed files and commits a consistent snapshot.
File hashes detect edits even with unchanged mtimes. Configuration, workspace,
grammar versions and embedding revision participate in the index version.
Unchanged symbols reuse vectors; failed remote vectors remain pending. A changed
workspace during synchronization aborts the transaction and returns no result.

Tree-sitter extracts Python, Go, JavaScript/JSX and TypeScript/TSX declarations,
qualified names, receiver methods and identifier occurrences. Byte offsets are
converted to Unicode columns without accessing native Point attributes (Windows
0.26.0 Point access crashed in the 700-function regression case). Syntax records
are candidates, not semantic symbol bindings. Syntax errors remove prior symbols,
while literal text search remains available for otherwise permitted source files.

Tools: refresh_code_index; code_symbols(file_path, backend="syntax"|"lsp");
search_codebase(query, limit, mode="semantic"|"text"|"symbol"); and the existing
go_to_definition / find_references. Positions use 1-based lines and 0-based Unicode
columns. Python defaults to Jedi in a bounded child process. Configured LSP servers
use stdio initialize, initialized, didOpen, request, shutdown and exit, with UTF-16
position conversion. Other languages default to explicitly unresolved same-name
syntax candidates within their language family. Ignored/outside locations are
filtered and concurrent workspace changes reject navigation results.

LSP configuration is trusted deployment JSON, never model-supplied argv:

```json
{"go":{"command":["gopls"],"timeout":10},"typescript":{"command":["typescript-language-server","--stdio"],"timeout":10}}
```

Set AGENT_CODE_LSP_SERVERS; use language keys python/go/javascript/typescript/tsx.
Install servers separately. Default worker timeout is 10 seconds, maximum 30;
Jedi is 10 seconds. Responses are limited to 1 MiB/frame, 2 MiB/64 frames and 1000
locations. Workers use the shared process runner's cancellation/process-tree
cleanup. Configured servers are trusted host programs under the T02 boundary;
the client does not execute workspace/configuration edits supplied by the server.
The fixture server validates the protocol, not real gopls/TypeScript semantics.

Default embedding configuration is offline hashing, 64 dimensions, revision 1.
This provides reproducibility without promising trained-model semantic quality.
For remote vectors, set AGENT_CODE_EMBEDDING_CONFIG, for example:

```json
{"kind":"openai","model":"YOUR_EMBEDDING_MODEL","dimensions":768,"revision":"deployment-v1","max_requests":16}
```

Remote mode uses the existing server client/authentication. Dimensions must match
the service's actual output; 8..2048 dimensions are supported. Endpoint, model,
revision and dimensions identify vectors. Operators must change revision for
model changes behind a stable alias. Remote refresh makes at most 32 requests,
with 10-second timeouts and no retries, stopping on the first error. Search never
backfills remote vectors: run refresh_code_index or the build CLI explicitly.
Semantic queries use at most one remote request when valid vectors exist.
Provider failure or invalid vectors cannot silently become local fallback vectors.
The shared task request/token/cost budget is applied to remote calls.

Search uses exact normalized cosine up to 256 vectors; larger indexes use six
8-bit random-sign LSH tables, one-bit neighbor probing and at most 512 candidates.
Persistent bucket postings avoid loading every vector at query time. Approximate
results are labeled; absent candidates are not presented as exhaustive search.
Exact case-sensitive literal text and exact symbol names remain independent modes.

Nested .gitignore/.agentignore rules use pathspec's GitIgnoreSpec, with hard
exclusions for repository metadata, environments, dependencies, generated output,
symlinks/junctions, .env*, key/credential files and recognized secret content.
These rules can have false positives and are not a complete secret detector;
operators should add project-specific .agentignore patterns. Only bounded UTF-8
text is indexed: 1 MiB/file, 64 MiB/10000 files, 20000 inventory paths, 20000 symbols,
200000 occurrences, 256 MiB database. Oversized/invalid files are excluded; aggregate
limits reject the operation. The database stores permitted source text/snippets
locally. No background watcher is required: every query refreshes file hashes.

Run `python -m agent_execution.code_index.benchmark --symbols 1000 --queries 10`.
benchmark_baseline.json records the current Windows offline baseline: 1017 symbols,
symbol recall 1.0, ANN recall@5 1.0, build 1301.994 ms, unchanged refresh 35.907 ms,
query median 59.504 ms, maximum 81.722 ms. Queries use full synthetic chunks and
compare against exhaustive cosine; this is a regression baseline, not natural
language retrieval quality or a production latency guarantee. Tests are in
agent_execution/tests/test_code_index.py, including actual grammars, Jedi, protocol
worker timeout/cancellation and a quality/latency benchmark assertion.

References: [Tree-sitter Python bindings](https://tree-sitter.github.io/py-tree-sitter/),
[LSP 3.17](https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/),
[Git ignore rules](https://git-scm.com/docs/gitignore).
