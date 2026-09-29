# Bot Memory Architecture Plan

## Overview

This plan adds a five-layer memory system to the bot while keeping Laya as a decision gate, not as the final source of truth. The system should remain auditable: every long-term memory must trace back to an episode, and every overwrite or forgetting action must have a reason.

The memory system must also protect the primary chat path from excessive remote LLM or embedding requests. Memory enrichment is useful only when it does not cause the assistant to hit provider protection such as HTTP 429 rate limits. The default behavior should prioritize answering the user first, then perform bounded, retry-safe memory work in the background.

## Planning Table

| Phase | Memory Layer | Goal | Main Deliverables | Laya Role | Storage | Validation |
|---|---|---|---|---|---|---|
| 1 | Layer 2: episodic memory | Record what happened in each interaction. | Add `memory.py`; create SQLite tables for events; log user, assistant, tool call, tool result, Laya decision, errors, timestamps. | Classify whether an event is routine, risky, failed, or potentially worth later extraction. | SQLite `episodes`, `episode_events`. | Unit tests for insert/query; confirm every CLI turn writes events. |
| 2 | Layer 1: working memory | Keep the active context useful when message history grows. | Add message trimming and summary injection; preserve recent turns and compact older turns. | Decide whether older context contains facts or procedures worth preserving before compaction. | In-memory `messages`; optional summary row in SQLite. | Tests for context compaction boundaries and summary placement. |
| 3 | Layer 3: semantic memory | Store durable facts, preferences, entities, and relations. | Add SQLite triple-like table: `subject`, `predicate`, `object`, `confidence`, `source_event_id`, `created_at`, `updated_at`, `expires_at`, `superseded_by`. | Gatekeeper: decide whether an episode contains durable facts, preferences, or conflicts. | SQLite `semantic_memories`. | Tests for insert, update, conflict marking, source traceability. |
| 4 | Retrieval | Use memory before answering without creating an unbounded embedding fan-out. | Add retrieval step before `run_agent`; use cheap SQLite/lexical filtering first; run vector ranking only on a small candidate set; inject concise memory context into messages. Missing memory embeddings must not be backfilled synchronously for every row. | Rank whether memories are relevant to the current user request only after cheaper filters reduce the candidate set. | SQLite queries, cached embeddings, in-memory context. | Tests that relevant memory is included, stale/superseded memory is excluded, and one turn has a fixed maximum remote embedding call count. |
| 5 | Layer 4: procedural memory | Remember reusable successful workflows without calling an LLM for every comparison. | Add `procedures` table with `task_type`, `context_pattern`, `steps`, `success_count`, `failure_count`, `last_success_at`, `confidence`; compare new candidates with lexical/exact matching first and use LLM similarity only for small ambiguous candidate sets. | Decide whether a completed episode represents a reusable procedure and whether it succeeded. | SQLite `procedures`, linked to source episodes. | Tests for procedure creation, success/failure updates, retrieval by task type, and fallback behavior when LLM similarity is skipped or rate-limited. |
| 6 | Layer 5: forgetting | Prevent stale, contradictory, or low-value memory accumulation. | Add forgetting policy: expiry, supersession, confidence decay, archival reasons, manual purge hooks. Forgetting must be deterministic by default and should not require remote LLM calls in the user-facing turn. | Candidate judge: identify conflicts, obsolete facts, one-off context, or memories that should expire. | SQLite fields: `archived_at`, `archive_reason`, `expires_at`, `superseded_by`. | Tests for conflict replacement, expiry filtering, audit trail retention, and zero remote request behavior for routine policy runs. |
| 7 | Review and observability | Make memory behavior debuggable and rate-limit aware. | Add commands or helper functions to inspect recent episodes, semantic facts, procedures, archived memories, memory queue depth, remote request budget usage, fallback counts, and 429/retry events. | Classify suspicious or low-confidence memory decisions for review. | SQLite read APIs; optional CLI helper. | Manual smoke tests and unit tests for inspection output, request budget logging, and graceful degradation after provider 429. |

## Laya Responsibility Boundary

| Decision Area | Let Laya Decide? | Final Writer | Notes |
|---|---:|---|---|
| Whether an episode may contain durable facts | Yes | Memory service | Laya produces a candidate signal only. |
| Exact semantic fact content | Partially | LLM or parser plus memory service | Laya should not be the only extractor. |
| Whether a workflow is reusable | Yes | Procedure memory service | Procedure steps should come from actual episode/tool logs. |
| Whether a memory conflicts with another | Yes | Memory service | Rules decide supersession and archival. |
| Whether to permanently delete memory | No | Explicit policy or user action | Prefer archival over hard delete. |
| Whether stale memory should be ignored | Yes | Retrieval layer | Retrieval must filter expired and superseded rows. |
| Whether to spend remote LLM budget on memory enrichment | No | Request budget policy | The policy must protect the main assistant response from memory-related 429s. |

## Minimal First Milestone

| Task | Scope | Done When |
|---|---|---|
| Create `memory.py` | SQLite connection, migrations, episode insert helpers. | Tests can create a temporary database and log a full user-assistant turn. |
| Log CLI turns | Record user input, guard notice, assistant reply, exceptions. | Running the bot creates episode rows without changing the chat behavior. |
| Add memory classifier wrapper | Extend Laya usage with a separate memory question set. | Tests mock Laya and verify candidate memory decisions. |
| Add semantic table only | Store facts manually or from mocked extraction. | Facts can be inserted, superseded, and retrieved. |
| Add request budget guard | Centralize per-turn limits for remote chat completions and embeddings used by memory work. | Tests prove memory retrieval/review falls back instead of exceeding the configured budget. |

## Microsoft Agent Memory Follow-Up Roadmap

This section records the next improvements after comparing the current project with Microsoft's agent-memory lesson. The current implementation already covers the core shape: working memory compaction, episodic event logs, semantic triples, procedure memory, retrieval injection, background review, and deterministic forgetting. The next work should focus on memory quality, structured retrieval, and making stored memories easier to govern.

### Current Implementation Status

Implemented after this plan:

- Compacted working-memory summaries can enter the long-term review pipeline, with duplicate-safe semantic extraction from summaries.
- Semantic memories now have explicit `memory_type` and `scope`, and extraction/retrieval preserves those fields.
- Semantic memories can link to normalized entities and aliases through `entities`, `entity_aliases`, `subject_entity_id`, and `object_entity_id`.
- Semantic updates are first-class: new candidates can add, reinforce, supersede, contradict, or ignore existing memories, with audit events.
- Procedure memory can be retrieved into prompt context and procedure outcomes are tracked from later tool use.
- Governance commands now cover review candidates, facts, low-confidence facts, conflicts, procedures, archive/confirm/contradict/supersede, export, and import.
- Request budgets are logged for foreground retrieval and background review.
- Semantic embeddings track `embedding_updated_at`; missing embedding backfill is available as a bounded maintenance command.
- Memory observability includes `memory-health`, which summarizes request budgets, capped memory operations, 429 errors, and embedding backfill queue depth.

Remaining work:

- Add a persistent background worker for embedding and review queues instead of only explicit maintenance commands.
- Add provider-wide 429 cooldown/backoff state for memory background work, honoring `Retry-After` before the next remote attempt.
- Extend embedding backfill to refresh stale embeddings, not only missing embeddings.
- Add a small skipped-missing-embedding counter in retrieval if more detail is needed than the current budget and queue-depth summaries.
- Full repository test discovery still requires optional dependencies (`openai`, `networkx`, `ckip_transformers`) to be installed.

### Priority 1: Promote Compacted Working Memory Into Long-Term Review

Current behavior records `working_memory_preservation_candidate` when compacted context may contain durable facts or procedures, but the memory review pipeline only processes `memory_review_candidate` events. This means useful information can be noticed during compaction without being extracted into semantic or procedural memory.

| Task | Scope | Done When |
|---|---|---|
| Normalize preservation candidates | Convert `working_memory_preservation_candidate` into reviewable semantic/procedure candidates, or teach `process_memory_review_candidates` to process both event types. | A compacted summary classified as `semantic`, `procedure`, or `both` creates the same review result shape as a normal candidate. |
| Extract from summaries safely | Add extraction path that uses the compacted summary as source text while preserving traceability to the episode and summary event. | Semantic memories created from summaries point back to the `working_memory_summary` or preservation event. |
| Avoid duplicate extraction | Deduplicate summary-derived memories against memories already extracted from the same episode. | Tests prove one turn does not create duplicate facts from both raw messages and compacted summaries. |

### Priority 2: Add Explicit Memory Types and Scopes

Microsoft separates long-term memory into specialized forms such as persona, entity, workflow/episodic, and user preferences. The current `semantic_memories` table can represent these as triples, but retrieval cannot distinguish a stable user preference from a project fact or an agent persona rule.

| Task | Scope | Done When |
|---|---|---|
| Add memory type metadata | Add fields such as `memory_type` and `scope` to semantic memories, with values like `user_profile`, `project_fact`, `agent_persona`, `entity_fact`, and `task_fact`. | Existing rows migrate with a default type, and tests can filter/retrieve by type. |
| Update semantic extraction prompt | Ask the extractor to classify each triple by type and scope in addition to subject/predicate/object/confidence. | Parsed LLM output stores typed memories and falls back cleanly when type is absent. |
| Prioritize stable profile memories | Retrieval should prefer relevant `user_profile` and `project_fact` memories over transient task facts when confidence is comparable. | Prompt context groups memory by type and includes high-value profile facts first. |

### Priority 3: Introduce Entity-Aware Structured Memory

The current triple store is a good first step, but entity memory is still implicit. A lightweight entity layer would let the bot understand that multiple facts refer to the same project, provider, model, file, or user preference.

| Task | Scope | Done When |
|---|---|---|
| Add entity records | Create `entities` and optional `entity_aliases` tables for normalized names, labels, kinds, and source events. | Extracted facts can link subject/object text to stable entity IDs. |
| Link semantic triples to entities | Add nullable `subject_entity_id` and `object_entity_id` fields or a join table. | Retrieval can find facts through aliases, not only exact text matches. |
| Build entity-centered context | Add retrieval mode that groups related memories by entity before formatting prompt context. | Asking about a project/model/provider returns a compact bundle of connected facts. |

### Priority 4: Make Memory Updates First-Class

Microsoft describes memory management as an extraction plus update pipeline. The current system extracts memories and later resolves conflicts through forgetting policy. A stronger update step should decide whether a new memory adds, reinforces, supersedes, or contradicts an existing memory before insertion.

| Task | Scope | Done When |
|---|---|---|
| Add update decision service | Given a candidate triple, inspect active memories with the same subject/predicate or linked entity and choose `add`, `reinforce`, `supersede`, `contradict`, or `ignore`. | Tests cover same fact reinforcement, changed preference supersession, contradiction archival, and low-confidence ignore. |
| Use existing supersession APIs | Route changed facts through `supersede_semantic_memory` instead of only relying on later conflict cleanup. | Archived memories have clear `archive_reason` and new rows point to the source event. |
| Record update audit events | Log a `memory_update_decision` event for each candidate. | Observability can explain why a memory was added, merged, superseded, or skipped. |

### Priority 5: Retrieve Procedure Memory Before Tool Work

Workflow/episodic memory is stored today, but the active retrieval context mainly formats semantic memories. Procedure memory should help the assistant reuse known successful workflows when a new request resembles past tool work.

| Task | Scope | Done When |
|---|---|---|
| Add procedure retrieval context | Build a `build_procedure_context` function that ranks active procedures by task type, lexical similarity, success rate, and recency. | Similar tool requests inject a concise "Relevant reusable workflows" section. |
| Combine semantic and procedure context | Merge memory context sections with a strict token budget and deterministic ordering. | The final prompt contains user/project facts and reusable workflows without duplicating irrelevant history. |
| Track procedure outcomes automatically | Record `procedure_result` events after tool workflows succeed or fail, then feed them into lifecycle policy. | Procedure success/failure counts reflect actual later reuse, not only initial creation. |

### Priority 6: Improve Memory Governance and CLI Operations

The observability command already shows overviews, episodes, messages, and skill runs. To make memory auditable and correctable, add explicit review commands.

| Task | Scope | Done When |
|---|---|---|
| Add review commands | Extend `observability.py` with commands to list pending candidates, active facts by type, low-confidence facts, conflicts, and procedures. | A maintainer can inspect memory quality without opening SQLite manually. |
| Add correction commands | Support archive, confirm, contradict, and supersede operations from CLI. | Commands write audit events and never hard-delete by default. |
| Add export/import | Export semantic and procedural memory as JSON for backup or migration. | Exported memory includes source IDs, archive status, confidence, and timestamps. |

### Recommended Implementation Order

1. Connect compacted working-memory preservation to the existing review pipeline.
2. Add typed/scoped semantic memories and retrieval formatting by type.
3. Add first-class update decisions before inserting new semantic memories.
4. Add procedure retrieval context and automatic procedure outcome tracking.
5. Add entity tables and alias-aware retrieval.
6. Add CLI review/correction/export commands.

The first three steps should be completed before considering an external memory backend such as Mem0, Cognee, Azure AI Search, or a graph database. The current SQLite design is still sufficient for validating the product behavior, and the main risk is memory quality rather than storage capacity.

## Rate-Limit and Request Budget Plan

### Per-Turn Budget

Each user turn should have an explicit remote request budget. The main `run_agent` completion has priority over all memory enrichment. Memory-related work must check the budget before using remote completions or embeddings.

Recommended initial limits:

| Operation | Synchronous User-Turn Limit | Fallback |
|---|---:|---|
| Main assistant chat completion | Required, with existing retry/backoff | Surface the existing API error if all retries fail. |
| Query embedding for memory retrieval | 1 | Use lexical ranking only. |
| Missing semantic memory embedding backfill | 0 | Queue background embedding or use hashing embedding. |
| Semantic extraction after a turn | 0 in foreground, small bounded background batch | Use parser fallback and leave candidate queued. |
| Procedure similarity LLM comparison | 0 in foreground, only ambiguous background cases | Use exact or lexical matcher. |
| Forgetting policy | 0 | Use deterministic rules only. |

### Retrieval Flow

Retrieval should be staged so the number of remote embedding calls does not grow with the number of stored memories:

1. Fetch only active, non-expired, non-superseded memories.
2. Apply cheap lexical or recency/confidence filtering to select a small candidate set.
3. Embed the user query at most once when budget is available.
4. Rank only candidates with existing cached embeddings.
5. Do not synchronously generate embeddings for every memory missing an embedding.
6. Queue missing embeddings for a background worker that has its own low concurrency, retry-after handling, and backoff.

This avoids the dangerous pattern of one user turn creating `1 + N` embedding requests where `N` is the number of active memories without cached embeddings.

### Memory Review Flow

Memory review should be best-effort and decoupled from the response path:

1. Record the episode synchronously.
2. Use local Laya classification as a cheap gate.
3. Require a confidence threshold before queuing semantic extraction or procedure review.
4. Run extraction and procedure matching in a background worker by default.
5. Do not block every turn with `join()`; only drain the queue on graceful shutdown or explicit maintenance.
6. On HTTP 429, honor `Retry-After`, exponentially back off, and keep the episode queued instead of retrying tightly.
7. If budget is exhausted, use deterministic fallbacks or leave the candidate pending.

### Embedding Cache and Backfill

Semantic memories should store embeddings once generated. Backfill should be performed outside the interactive path and must be bounded:

- Use a low worker concurrency such as 1.
- Process a small batch per interval.
- Skip backfill when the provider recently returned 429.
- Prefer cached embeddings for ranking; use hashing embeddings only as a local fallback.
- Track `embedding_updated_at` or equivalent metadata so stale embeddings can be refreshed deliberately.

### Observability

The system should record enough information to explain memory behavior without guessing:

- Remote chat completion count per turn.
- Remote embedding count per turn.
- Budget-exhausted fallback count.
- Background queue depth.
- 429 count and retry-after delay.
- Number of memories skipped because embeddings were missing.

## Design Rules

- Laya is a gatekeeper, not the database authority.
- Every semantic and procedural memory must point to a source episode.
- Newer memories do not silently overwrite old memories; they supersede or archive them with a reason.
- Retrieval should prefer active, recent, high-confidence memories.
- The bot should continue working if memory storage or Laya classification fails.
- Memory features must not create unbounded remote LLM or embedding requests.
- The main assistant response has priority over semantic extraction, procedure matching, embedding backfill, and forgetting review.
- Remote 429 from memory work should degrade memory behavior, not terminate the chat session.
