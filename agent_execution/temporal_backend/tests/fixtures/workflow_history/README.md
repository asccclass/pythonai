These checked-in histories are generated only by the isolated Temporal development Service tests,
with controlled providers and temporary SQLite/workspaces. They contain opaque IDs and workflow
protocol events only; fixture generation replaces the submission ID with a stable synthetic value.
Do not add histories captured from user or production namespaces.

Regenerate locally by setting `PYTHONAI_WRITE_TEMPORAL_REPLAY_FIXTURES=1` together with the verified
`PYTHONAI_TEMPORAL_TEST_CLI` path, then run the four named fixture source cases documented in
`agent_execution/temporal_backend/README.md`.
