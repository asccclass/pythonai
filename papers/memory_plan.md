# Bot Memory Architecture Plan

## Overview

This plan adds a five-layer memory system to the bot while keeping Laya as a decision gate, not as the final source of truth. The system should remain auditable: every long-term memory must trace back to an episode, and every overwrite or forgetting action must have a reason.

## Planning Table

| Phase | Memory Layer | Goal | Main Deliverables | Laya Role | Storage | Validation |
|---|---|---|---|---|---|---|
| 1 | Layer 2: episodic memory | Record what happened in each interaction. | Add `memory.py`; create SQLite tables for events; log user, assistant, tool call, tool result, Laya decision, errors, timestamps. | Classify whether an event is routine, risky, failed, or potentially worth later extraction. | SQLite `episodes`, `episode_events`. | Unit tests for insert/query; confirm every CLI turn writes events. |
| 2 | Layer 1: working memory | Keep the active context useful when message history grows. | Add message trimming and summary injection; preserve recent turns and compact older turns. | Decide whether older context contains facts or procedures worth preserving before compaction. | In-memory `messages`; optional summary row in SQLite. | Tests for context compaction boundaries and summary placement. |
| 3 | Layer 3: semantic memory | Store durable facts, preferences, entities, and relations. | Add SQLite triple-like table: `subject`, `predicate`, `object`, `confidence`, `source_event_id`, `created_at`, `updated_at`, `expires_at`, `superseded_by`. | Gatekeeper: decide whether an episode contains durable facts, preferences, or conflicts. | SQLite `semantic_memories`. | Tests for insert, update, conflict marking, source traceability. |
| 4 | Retrieval | Use memory before answering. | Add retrieval step before `run_agent`; fetch recent episodes and relevant semantic facts; inject concise memory context into messages. | Rank whether memories are relevant to the current user request. | SQLite queries plus in-memory context. | Tests that relevant memory is included and stale/superseded memory is excluded. |
| 5 | Layer 4: procedural memory | Remember reusable successful workflows. | Add `procedures` table with `task_type`, `context_pattern`, `steps`, `success_count`, `failure_count`, `last_success_at`, `confidence`. | Decide whether a completed episode represents a reusable procedure and whether it succeeded. | SQLite `procedures`, linked to source episodes. | Tests for procedure creation, success/failure updates, retrieval by task type. |
| 6 | Layer 5: forgetting | Prevent stale, contradictory, or low-value memory accumulation. | Add forgetting policy: expiry, supersession, confidence decay, archival reasons, manual purge hooks. | Candidate judge: identify conflicts, obsolete facts, one-off context, or memories that should expire. | SQLite fields: `archived_at`, `archive_reason`, `expires_at`, `superseded_by`. | Tests for conflict replacement, expiry filtering, and audit trail retention. |
| 7 | Review and observability | Make memory behavior debuggable. | Add commands or helper functions to inspect recent episodes, semantic facts, procedures, and archived memories. | Classify suspicious or low-confidence memory decisions for review. | SQLite read APIs; optional CLI helper. | Manual smoke tests and unit tests for inspection output. |

## Laya Responsibility Boundary

| Decision Area | Let Laya Decide? | Final Writer | Notes |
|---|---:|---|---|
| Whether an episode may contain durable facts | Yes | Memory service | Laya produces a candidate signal only. |
| Exact semantic fact content | Partially | LLM or parser plus memory service | Laya should not be the only extractor. |
| Whether a workflow is reusable | Yes | Procedure memory service | Procedure steps should come from actual episode/tool logs. |
| Whether a memory conflicts with another | Yes | Memory service | Rules decide supersession and archival. |
| Whether to permanently delete memory | No | Explicit policy or user action | Prefer archival over hard delete. |
| Whether stale memory should be ignored | Yes | Retrieval layer | Retrieval must filter expired and superseded rows. |

## Minimal First Milestone

| Task | Scope | Done When |
|---|---|---|
| Create `memory.py` | SQLite connection, migrations, episode insert helpers. | Tests can create a temporary database and log a full user-assistant turn. |
| Log CLI turns | Record user input, guard notice, assistant reply, exceptions. | Running the bot creates episode rows without changing the chat behavior. |
| Add memory classifier wrapper | Extend Laya usage with a separate memory question set. | Tests mock Laya and verify candidate memory decisions. |
| Add semantic table only | Store facts manually or from mocked extraction. | Facts can be inserted, superseded, and retrieved. |

## Design Rules

- Laya is a gatekeeper, not the database authority.
- Every semantic and procedural memory must point to a source episode.
- Newer memories do not silently overwrite old memories; they supersede or archive them with a reason.
- Retrieval should prefer active, recent, high-confidence memories.
- The bot should continue working if memory storage or Laya classification fails.
