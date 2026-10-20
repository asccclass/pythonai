# Agent memory subsystem
- memory.py owns SQLite episodic/semantic data; semantic_extractor.py and memory_classifier.py produce candidate knowledge; memory_review.py/procedure_similarity.py manage reusable workflows.
- retrieval.py/retrieval_ranker.py/vector_search.py combine bounded retrieval/embedding with fallbacks; working_memory.py and agent_execution/context.py handle model-input limits. Authoritative task checkpoint history is distinct from trimmed model input.
- memory_worker.py/request_budget.py enforce background queues, rate budgets and a worker lease across entrypoints. A background embedding/review failure is not automatically a foreground Agent task failure.
- observability.py and memory_tools.py expose management; write operations must retain policy/approval boundaries. Original conversations/checkpoints are private data, not sanitized public logs.
- agent_execution/user_profile/ stores identity-scoped profile information. Profiles are advisory context, never a source of authority or credentials.
