from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any, Callable, Protocol


@dataclass(frozen=True)
class SemanticTriple:
    subject: str
    predicate: str
    object_value: str
    confidence: float = 0.5


class SemanticExtractor(Protocol):
    def extract(self, text: str) -> list[SemanticTriple]:
        ...


class LLMSemanticExtractor:
    def __init__(
        self,
        client_factory: Callable[[], Any],
        model: str,
        fallback_extractor: Callable[[str], list[SemanticTriple]] | None = None,
    ) -> None:
        self.client_factory = client_factory
        self.model = model
        self.fallback_extractor = fallback_extractor or fallback_semantic_triples

    def extract(self, text: str) -> list[SemanticTriple]:
        normalized = text.strip()
        if not normalized:
            return []
        try:
            response = self.client_factory().chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": SEMANTIC_EXTRACTION_PROMPT},
                    {"role": "user", "content": normalized},
                ],
                temperature=0,
            )
            content = response.choices[0].message.content or ""
            triples = parse_semantic_triples(content)
            return triples or self.fallback_extractor(normalized)
        except Exception:
            return self.fallback_extractor(normalized)


SEMANTIC_EXTRACTION_PROMPT = """Extract durable semantic memory triples from the user's message.
Return JSON only, with this shape:
{"triples":[{"subject":"user","predicate":"prefers","object":"TypeScript","confidence":0.9}]}

Rules:
- Extract stable facts, preferences, identities, entities, relationships, constraints, and long-lived project truths.
- Do not extract transient task requests such as writing, deleting, listing, or running a command.
- Use short lowercase snake_case predicates.
- Use "user" for facts about the user unless another explicit entity is the subject.
- Keep objects concise but complete.
- Return {"triples":[]} when there is no durable semantic memory.
"""


def parse_semantic_triples(content: str) -> list[SemanticTriple]:
    payload = _load_json_object(content)
    raw_triples = payload.get("triples", []) if isinstance(payload, dict) else []
    if not isinstance(raw_triples, list):
        return []

    triples: list[SemanticTriple] = []
    for item in raw_triples:
        if not isinstance(item, dict):
            continue
        subject = str(item.get("subject", "")).strip()
        predicate = _normalize_predicate(str(item.get("predicate", "")).strip())
        object_value = str(item.get("object", item.get("object_value", ""))).strip()
        if not subject or not predicate or not object_value:
            continue
        triples.append(
            SemanticTriple(
                subject=subject,
                predicate=predicate,
                object_value=object_value,
                confidence=_clamp_confidence(item.get("confidence", 0.5)),
            )
        )
    return triples


def fallback_semantic_triples(text: str) -> list[SemanticTriple]:
    subject, predicate, object_value = extract_semantic_triple(text)
    if not object_value:
        return []
    return [SemanticTriple(subject, predicate, object_value, 0.5)]


def extract_semantic_triple(text: str) -> tuple[str, str, str]:
    normalized = text.strip()
    patterns = [
        (r"(?i)\bI prefer ([\w .+-]+)", "user", "prefers", 1),
        (r"(?i)\bI like ([\w .+-]+)", "user", "likes", 1),
        (r"(?i)\bI live in ([\w .+-]+)", "user", "lives_in", 1),
        (r"(?i)\bmy preferred ([\w_ -]+) is ([\w .+-]+)", "user", "preferred_{0}", 2),
    ]
    for pattern, subject, predicate, group in patterns:
        match = re.search(pattern, normalized)
        if not match:
            continue
        if "{0}" in predicate:
            return subject, predicate.format(match.group(1).strip().lower().replace(" ", "_")), match.group(group).strip()
        return subject, predicate, match.group(group).strip()
    return "episode", "user_statement", normalized


def _load_json_object(content: str) -> Any:
    stripped = content.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped, flags=re.IGNORECASE)
        stripped = re.sub(r"\s*```$", "", stripped)
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", stripped, flags=re.DOTALL)
        if not match:
            return {}
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return {}


def _normalize_predicate(predicate: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9]+", "_", predicate.lower()).strip("_")
    return normalized or predicate


def _clamp_confidence(value: Any) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return 0.5
    return min(1.0, max(0.0, confidence))
