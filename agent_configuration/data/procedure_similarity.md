Decide whether the new reusable workflow is semantically the same as one existing workflow.
Return JSON only:
{"best_id": 12, "score": 0.86, "reason": "same test command workflow"}

Rules:
- Match when the procedures solve the same kind of task with materially equivalent tool steps.
- Ignore harmless argument differences such as filenames, paths, or wording when the workflow is reusable.
- Do not match procedures that require different tools, different ordering, or different intent.
- Return {"best_id": null, "score": 0.0, "reason": "no match"} when no existing procedure is similar enough.
