from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any, Callable, Protocol


@dataclass(frozen=True)
class ProcedureCandidate:
    task_type: str
    context_pattern: str
    steps: list[str]


@dataclass(frozen=True)
class ProcedureMatch:
    procedure_id: int
    score: float
    reason: str = ""


class ProcedureSimilarityMatcher(Protocol):
    def find_match(
        self,
        candidate: ProcedureCandidate,
        procedures: list[dict[str, Any]],
        threshold: float = 0.72,
    ) -> ProcedureMatch | None:
        ...


class LLMProcedureSimilarityMatcher:
    def __init__(
        self,
        client_factory: Callable[[], Any],
        model: str,
        fallback_matcher: ProcedureSimilarityMatcher | None = None,
        allow_remote: Callable[[str], bool] | None = None,
    ) -> None:
        self.client_factory = client_factory
        self.model = model
        self.fallback_matcher = fallback_matcher or LexicalProcedureSimilarityMatcher()
        self.allow_remote = allow_remote

    def find_match(
        self,
        candidate: ProcedureCandidate,
        procedures: list[dict[str, Any]],
        threshold: float = 0.72,
    ) -> ProcedureMatch | None:
        if not procedures:
            return None
        fallback_match = self.fallback_matcher.find_match(candidate, procedures, threshold=threshold)
        if fallback_match is not None:
            return fallback_match
        if self.allow_remote is not None and not self.allow_remote("procedure_similarity"):
            return None
        try:
            response = self.client_factory().chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": PROCEDURE_SIMILARITY_PROMPT},
                    {"role": "user", "content": json.dumps(_similarity_payload(candidate, procedures), ensure_ascii=False)},
                ],
                temperature=0,
            )
            content = response.choices[0].message.content or ""
            match = parse_procedure_match(content)
            valid_ids = {int(procedure["id"]) for procedure in procedures}
            if match is not None and match.procedure_id in valid_ids and match.score >= threshold:
                return match
        except Exception:
            pass
        return self.fallback_matcher.find_match(candidate, procedures, threshold=threshold)


class LexicalProcedureSimilarityMatcher:
    def find_match(
        self,
        candidate: ProcedureCandidate,
        procedures: list[dict[str, Any]],
        threshold: float = 0.72,
    ) -> ProcedureMatch | None:
        candidate_text = procedure_text(candidate.task_type, candidate.context_pattern, candidate.steps)
        scored = []
        for procedure in procedures:
            procedure_score = token_similarity(
                candidate_text,
                procedure_text(procedure["task_type"], procedure["context_pattern"], procedure["steps"]),
            )
            scored.append((procedure_score, procedure))
        if not scored:
            return None
        score, procedure = sorted(scored, key=lambda item: (item[0], int(item[1]["success_count"]), int(item[1]["id"])), reverse=True)[0]
        if score < threshold:
            return None
        return ProcedureMatch(int(procedure["id"]), score, "lexical_similarity")


PROCEDURE_SIMILARITY_PROMPT = """Decide whether the new reusable workflow is semantically the same as one existing workflow.
Return JSON only:
{"best_id": 12, "score": 0.86, "reason": "same test command workflow"}

Rules:
- Match when the procedures solve the same kind of task with materially equivalent tool steps.
- Ignore harmless argument differences such as filenames, paths, or wording when the workflow is reusable.
- Do not match procedures that require different tools, different ordering, or different intent.
- Return {"best_id": null, "score": 0.0, "reason": "no match"} when no existing procedure is similar enough.
"""


def parse_procedure_match(content: str) -> ProcedureMatch | None:
    payload = _load_json_object(content)
    if not isinstance(payload, dict):
        return None
    procedure_id = payload.get("best_id", payload.get("procedure_id"))
    if procedure_id is None:
        return None
    try:
        parsed_id = int(procedure_id)
        score = float(payload.get("score", 0.0))
    except (TypeError, ValueError):
        return None
    return ProcedureMatch(parsed_id, min(1.0, max(0.0, score)), str(payload.get("reason", "")))


def procedure_text(task_type: str, context_pattern: str, steps: list[str]) -> str:
    return " ".join([task_type, context_pattern, " ".join(steps)])


def token_similarity(first: str, second: str) -> float:
    first_tokens = set(_tokens(first))
    second_tokens = set(_tokens(second))
    if not first_tokens or not second_tokens:
        return 0.0
    return len(first_tokens & second_tokens) / len(first_tokens | second_tokens)


def _similarity_payload(candidate: ProcedureCandidate, procedures: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "candidate": {
            "task_type": candidate.task_type,
            "context_pattern": candidate.context_pattern,
            "steps": candidate.steps,
        },
        "existing_procedures": [
            {
                "id": procedure["id"],
                "task_type": procedure["task_type"],
                "context_pattern": procedure["context_pattern"],
                "steps": procedure["steps"],
                "confidence": procedure["confidence"],
                "success_count": procedure["success_count"],
                "failure_count": procedure["failure_count"],
            }
            for procedure in procedures
        ],
    }


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-zA-Z0-9_]+", text.lower())


def _load_json_object(content: str) -> Any:
    stripped = content.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped, flags=re.IGNORECASE)
        stripped = re.sub(r"\s*```$", "", stripped)
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", stripped, flags=re.DOTALL)
        if match is None:
            return {}
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return {}
