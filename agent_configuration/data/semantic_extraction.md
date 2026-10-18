Extract durable semantic memory triples from the user's message.
Return JSON only, with this shape:
{"triples":[{"subject":"user","predicate":"prefers","object":"TypeScript","confidence":0.9,"memory_type":"user_profile","scope":"global"}]}

Rules:
- Extract stable facts, preferences, identities, entities, relationships, constraints, and long-lived project truths.
- Do not extract transient task requests such as writing, deleting, listing, or running a command.
- Do not extract conversational greetings or pleasantries (e.g., "Good morning", "Good night").
- Use short lowercase snake_case predicates.
- Use "user" for facts about the user unless another explicit entity is the subject.
- Use memory_type values such as user_profile, project_fact, agent_persona, entity_fact, or task_fact.
- Use scope="global" for user profile facts and a concise project/entity name when the fact belongs to a narrower scope.
- Keep objects concise but complete.
- Return {"triples":[]} when there is no durable semantic memory.
