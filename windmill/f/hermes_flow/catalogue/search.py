"""
Capability search and ranking — path: f/hermes_flow/catalogue/search

Other scripts import these directly:

    from f.hermes_flow.catalogue.search import SearchQuery, search

(same import pattern as `f.hermes_flow.catalogue.models`.) Deterministic —
no LLM, no embeddings. `search()` takes an already-loaded `Catalogue`
(`f.hermes_flow.catalogue.models.load_catalogue`) and a `SearchQuery`, and
returns ranked `SearchResult`s, each carrying the matched `CatalogueEntry`
(so callers get compatibility/policy metadata — effects, autonomy, maturity
— without a second lookup) and a `rationale` list explaining why it ranked
where it did.

Ranking implements SKILL.md's Rule 2 (primitives before workflows before
generation): `kind=script` ("primitive") capabilities get a fixed bonus
over `kind=flow` ("workflow") capabilities at equal relevance, so a
one-call primitive beats a many-step flow that happens to match just as
well, and a close workflow match still beats an irrelevant primitive.
Effects act as a tie-breaking penalty, not a hard filter (except via
`max_effects`, see below): between two equally relevant results, the one
with fewer declared side effects ranks first — this is what stops an
unsafe side-effect capability from being silently preferred purely because
it happens to match the query text slightly better.

Filtering (hard excludes, applied before scoring) vs. scoring (soft
ranking signal) are deliberately different mechanisms:
- `include_deprecated=False` (default) excludes `maturity=deprecated`
  entries entirely — they never appear, not just rank low.
- `required_input_kinds`/`required_output_kinds`, when non-empty, exclude
  any entry with no overlapping kind — an entry with zero relevant
  input/output compatibility isn't "a worse match," it's not a match.
- `max_effects`, when given, excludes any entry whose effects exceed it
  (e.g. `max_effects=CapabilityEffects()` — the default, all-False — only
  ever returns side-effect-free entries).
- `kind`, when given, restricts to just scripts or just flows.

Everything else (`tags`, `task` free text) contributes to `score` instead
of filtering, because there's no principled cutoff for "close enough" on a
fuzzy match — better to rank it low and let the caller/rationale explain
why than to silently drop a plausible result.

Running THIS script directly runs a self-test search against
`capability-index.yaml`'s content (passed as the `catalogue_yaml`
argument, same reasoning as `f.hermes_flow.catalogue.models` — Windmill
jobs don't have git-repo filesystem access) and returns the ranked
results, doubling as an integration test that this module's ranking logic
runs correctly inside Windmill's actual Python environment.
"""
import re

from f.hermes_flow.catalogue.models import (
    CapabilityKind,
    Catalogue,
    CatalogueEntry,
    load_catalogue,
)
from f.libraries.capability.models import CapabilityEffects, CapabilityMaturity
from pydantic import BaseModel, Field

SCHEMA_VERSION = "1.0"

_TAG_MATCH_WEIGHT = 4.0
_TASK_TOKEN_WEIGHT = 1.0
_KIND_COMPAT_WEIGHT = 5.0
_PRIMITIVE_BONUS = 2.0
_EFFECT_PENALTY = 0.5

_WORD_RE = re.compile(r"[a-z0-9]+")
# Deliberately minimal — not a real stopword list, just short filler words
# ("a", "an", "to", ...) that would otherwise match almost every entry's
# text and drown out meaningful keyword overlap. Anything length >= 3 not
# in this set is treated as a real content word.
_STOPWORDS = frozenset(
    {"a", "an", "the", "and", "or", "of", "to", "in", "on", "for", "is", "it", "its", "no", "not"}
)


def _tokenize(text: str) -> set[str]:
    return {w for w in _WORD_RE.findall(text.lower()) if w not in _STOPWORDS and len(w) >= 2}


class SearchQuery(BaseModel):
    """What to search for. See module docstring for filter vs. score semantics."""

    schema_version: str = Field(default=SCHEMA_VERSION)
    task: str | None = Field(
        default=None,
        description="Free-text task description, matched by keyword overlap against "
        "each entry's summary/inputs_summary/outputs_summary/tags. Deterministic "
        "token overlap, not real semantic search — no embeddings, no LLM.",
    )
    tags: list[str] = Field(default_factory=list, description="Exact-match tags, scored not filtered.")
    required_input_kinds: list[str] = Field(default_factory=list)
    required_output_kinds: list[str] = Field(default_factory=list)
    max_effects: CapabilityEffects | None = Field(
        default=None,
        description="Ceiling on acceptable effects — an entry with any effect True "
        "where max_effects has it False is excluded entirely, not just ranked lower.",
    )
    kind: CapabilityKind | None = Field(default=None, description="Restrict to script or flow only.")
    include_deprecated: bool = Field(default=False)


class SearchResult(BaseModel):
    entry: CatalogueEntry
    score: float
    rationale: list[str] = Field(default_factory=list)


class SearchResponse(BaseModel):
    schema_version: str = Field(default=SCHEMA_VERSION)
    query: SearchQuery
    results: list[SearchResult] = Field(default_factory=list)


def _effects_exceed(entry_effects: CapabilityEffects, ceiling: CapabilityEffects) -> bool:
    for field_name in CapabilityEffects.model_fields:
        if getattr(entry_effects, field_name) and not getattr(ceiling, field_name):
            return True
    return False


def _passes_filters(entry: CatalogueEntry, query: SearchQuery) -> bool:
    if not query.include_deprecated and entry.metadata.maturity is CapabilityMaturity.deprecated:
        return False
    if query.kind is not None and entry.kind is not query.kind:
        return False
    if query.max_effects is not None and _effects_exceed(entry.metadata.effects, query.max_effects):
        return False
    if query.required_input_kinds and not set(query.required_input_kinds) & set(entry.input_kinds):
        return False
    if query.required_output_kinds and not set(query.required_output_kinds) & set(entry.output_kinds):
        return False
    return True


def _score(entry: CatalogueEntry, query: SearchQuery) -> tuple[float, list[str]]:
    score = 0.0
    rationale: list[str] = []

    matched_tags = set(query.tags) & set(entry.tags)
    if matched_tags:
        contribution = _TAG_MATCH_WEIGHT * len(matched_tags)
        score += contribution
        rationale.append(f"matched tag(s) {sorted(matched_tags)} (+{contribution:g})")

    if query.task:
        query_tokens = _tokenize(query.task)
        entry_tokens = _tokenize(
            " ".join([entry.metadata.summary, entry.inputs_summary, entry.outputs_summary, *entry.tags])
        )
        matched_tokens = query_tokens & entry_tokens
        if matched_tokens:
            contribution = _TASK_TOKEN_WEIGHT * len(matched_tokens)
            score += contribution
            rationale.append(f"matched task keyword(s) {sorted(matched_tokens)} (+{contribution:g})")

    matched_in = set(query.required_input_kinds) & set(entry.input_kinds)
    matched_out = set(query.required_output_kinds) & set(entry.output_kinds)
    if matched_in or matched_out:
        contribution = _KIND_COMPAT_WEIGHT * (len(matched_in) + len(matched_out))
        score += contribution
        if matched_in:
            rationale.append(f"input_kinds compatible: {sorted(matched_in)} (+{_KIND_COMPAT_WEIGHT * len(matched_in):g})")
        if matched_out:
            rationale.append(f"output_kinds compatible: {sorted(matched_out)} (+{_KIND_COMPAT_WEIGHT * len(matched_out):g})")

    if entry.kind is CapabilityKind.script:
        score += _PRIMITIVE_BONUS
        rationale.append(f"primitive (script) — favoured over flows per HF-005 Rule 2 (+{_PRIMITIVE_BONUS:g})")

    effect_count = sum(
        1 for field_name in CapabilityEffects.model_fields if getattr(entry.metadata.effects, field_name)
    )
    if effect_count:
        penalty = _EFFECT_PENALTY * effect_count
        score -= penalty
        rationale.append(f"{effect_count} declared side effect(s) — ranked down (-{penalty:g})")

    return score, rationale


def search(catalogue: Catalogue, query: SearchQuery) -> SearchResponse:
    """Filter catalogue.entries by query's hard constraints, score and rank the rest."""
    candidates = [e for e in catalogue.entries if _passes_filters(e, query)]
    scored = [(*_score(e, query), e) for e in candidates]
    # Sort by score desc, then path asc for a fully deterministic order among ties.
    scored.sort(key=lambda t: (-t[0], t[2].metadata.path))
    results = [SearchResult(entry=e, score=score, rationale=rationale) for score, rationale, e in scored]
    return SearchResponse(query=query, results=results)


def main(catalogue_yaml: str, task: str = "", tags: list[str] | None = None) -> dict:
    """Self-test / integration check: search the given catalogue text."""
    catalogue = load_catalogue(catalogue_yaml)
    query = SearchQuery(task=task or None, tags=tags or [])
    response = search(catalogue, query)
    return {
        "query": query.model_dump(),
        "results": [
            {"path": r.entry.metadata.path, "score": r.score, "rationale": r.rationale}
            for r in response.results
        ],
    }
