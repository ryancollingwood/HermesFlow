"""Unit tests for f/hermes_flow/catalogue/search.py — not synced to Windmill (see conftest.py)."""
import json
import pathlib

from f.hermes_flow.catalogue.models import (
    CapabilityKind,
    Catalogue,
    CatalogueEntry,
    load_catalogue,
)
from f.hermes_flow.catalogue.search import SearchQuery, SearchResponse, search
from f.libraries.capability.models import CapabilityEffects, CapabilityMetadata

CATALOGUE_PATH = pathlib.Path(__file__).parent.parent / "capability-index.yaml"
SCHEMAS_DIR = pathlib.Path(__file__).parent.parent.parent / "docs" / "schemas"


def make_entry(path, kind, tags, effects, summary, maturity="stable", input_kinds=None, output_kinds=None):
    return CatalogueEntry(
        kind=kind,
        tags=tags,
        inputs_summary="in",
        outputs_summary="out",
        input_kinds=input_kinds or [],
        output_kinds=output_kinds or [],
        metadata=CapabilityMetadata(
            path=path,
            capability_version="1.0.0",
            summary=summary,
            maturity=maturity,
            owners=["x"],
            effects=effects,
        ),
    )


def make_fixture_catalogue() -> Catalogue:
    """Fixed evaluation set used across most tests below — values verified against the
    real search() output before being pinned as expectations, not written blind."""
    web_fetch = make_entry(
        "f/capabilities/web/fetch",
        CapabilityKind.script,
        ["web", "fetch", "read-only"],
        CapabilityEffects(network=True),
        "Fetch a web page and return its raw HTML content.",
        input_kinds=["url"],
        output_kinds=["html"],
    )
    web_fetch_and_store = make_entry(
        "f/workflows/web/fetch_and_store",
        CapabilityKind.flow,
        ["web", "fetch", "store"],
        CapabilityEffects(network=True, filesystem=True),
        "Fetch a web page and store its content to disk.",
        input_kinds=["url"],
        output_kinds=["stored_file"],
    )
    legacy_scraper = make_entry(
        "f/capabilities/web/legacy_scraper",
        CapabilityKind.script,
        ["web", "fetch", "scrape"],
        CapabilityEffects(network=True),
        "Old scraping tool, fetch a page the old way.",
        maturity="deprecated",
    )
    db_writer = make_entry(
        "f/capabilities/db/writer",
        CapabilityKind.script,
        ["database", "write"],
        CapabilityEffects(database=True),
        "Write a record to the database.",
    )
    readonly_reporter = make_entry(
        "f/capabilities/report/generate",
        CapabilityKind.script,
        ["report"],
        CapabilityEffects(),
        "Generate a read-only summary report.",
        maturity="experimental",
        output_kinds=["report"],
    )
    return Catalogue(
        entries=[web_fetch, web_fetch_and_store, legacy_scraper, db_writer, readonly_reporter]
    )


# ── Fixed evaluation set: task description -> expected top result ───────────


def test_eval_set_fetch_a_web_page_ranks_the_primitive_first():
    catalogue = make_fixture_catalogue()
    response = search(catalogue, SearchQuery(task="fetch a web page"))
    assert response.results[0].entry.metadata.path == "f/capabilities/web/fetch"


def test_eval_set_write_a_database_record_ranks_the_writer_first():
    catalogue = make_fixture_catalogue()
    response = search(catalogue, SearchQuery(task="write a record to the database"))
    assert response.results[0].entry.metadata.path == "f/capabilities/db/writer"


def test_eval_set_no_query_terms_still_ranks_primitives_above_the_flow():
    # No task/tags at all — only the primitive-vs-workflow and effects signals apply.
    catalogue = make_fixture_catalogue()
    response = search(catalogue, SearchQuery())
    paths = [r.entry.metadata.path for r in response.results]
    assert paths.index("f/capabilities/web/fetch") < paths.index("f/workflows/web/fetch_and_store")


# ── Exact tag search ──────────────────────────────────────────────────────────


def test_exact_tag_match_ranks_the_only_matching_entry_first():
    catalogue = make_fixture_catalogue()
    response = search(catalogue, SearchQuery(tags=["store"]))
    # Only fetch_and_store carries the "store" tag.
    assert response.results[0].entry.metadata.path == "f/workflows/web/fetch_and_store"
    assert any("store" in r for r in response.results[0].rationale)


def test_tag_search_scores_multiple_matches_above_single_matches():
    catalogue = make_fixture_catalogue()
    response = search(catalogue, SearchQuery(tags=["web", "fetch"]))
    # web_fetch has both tags; fetch_and_store has both tags too but more effects —
    # web_fetch (fewer effects) must outrank it.
    paths = [r.entry.metadata.path for r in response.results]
    assert paths[0] == "f/capabilities/web/fetch"
    assert paths[1] == "f/workflows/web/fetch_and_store"


# ── Semantic-ish description search (deterministic keyword overlap) ─────────


def test_task_description_keyword_overlap_scores_relevant_entries():
    catalogue = make_fixture_catalogue()
    response = search(catalogue, SearchQuery(task="generate a report"))
    assert response.results[0].entry.metadata.path == "f/capabilities/report/generate"
    assert response.results[0].score > 0


def test_task_description_with_no_overlap_still_returns_all_results_ranked_by_other_signals():
    catalogue = make_fixture_catalogue()
    response = search(catalogue, SearchQuery(task="xyzzy plugh completely unrelated"))
    # No keyword overlap anywhere, so nothing gets a task-match rationale entry —
    # but results still come back, ranked by kind/effects alone.
    assert len(response.results) == 4  # 5 entries minus the deprecated one
    assert all(not any("keyword" in r for r in result.rationale) for result in response.results)


# ── Schema/kind-compatibility search ─────────────────────────────────────────


def test_required_input_kind_excludes_incompatible_entries():
    catalogue = make_fixture_catalogue()
    response = search(catalogue, SearchQuery(required_input_kinds=["url"]))
    paths = {r.entry.metadata.path for r in response.results}
    assert paths == {"f/capabilities/web/fetch", "f/workflows/web/fetch_and_store"}


def test_required_output_kind_excludes_incompatible_entries():
    catalogue = make_fixture_catalogue()
    response = search(catalogue, SearchQuery(required_output_kinds=["report"]))
    assert [r.entry.metadata.path for r in response.results] == ["f/capabilities/report/generate"]


def test_required_kind_with_no_matches_returns_empty_not_an_error():
    catalogue = make_fixture_catalogue()
    response = search(catalogue, SearchQuery(required_output_kinds=["nonexistent_kind"]))
    assert response.results == []


def test_input_kind_compatible_entries_still_favour_the_primitive():
    catalogue = make_fixture_catalogue()
    response = search(catalogue, SearchQuery(required_input_kinds=["url"]))
    assert response.results[0].entry.metadata.path == "f/capabilities/web/fetch"


# ── Deprecated and incompatible excluded by default ──────────────────────────


def test_deprecated_excluded_by_default():
    catalogue = make_fixture_catalogue()
    response = search(catalogue, SearchQuery(tags=["web", "fetch", "scrape"]))
    paths = {r.entry.metadata.path for r in response.results}
    assert "f/capabilities/web/legacy_scraper" not in paths


def test_deprecated_included_when_explicitly_requested():
    catalogue = make_fixture_catalogue()
    response = search(catalogue, SearchQuery(tags=["scrape"], include_deprecated=True))
    paths = {r.entry.metadata.path for r in response.results}
    assert "f/capabilities/web/legacy_scraper" in paths


def test_max_effects_excludes_anything_exceeding_the_ceiling():
    catalogue = make_fixture_catalogue()
    response = search(catalogue, SearchQuery(max_effects=CapabilityEffects()))
    assert [r.entry.metadata.path for r in response.results] == ["f/capabilities/report/generate"]


def test_kind_filter_restricts_to_scripts_or_flows_only():
    catalogue = make_fixture_catalogue()
    scripts_only = search(catalogue, SearchQuery(kind=CapabilityKind.script))
    assert all(r.entry.kind is CapabilityKind.script for r in scripts_only.results)
    flows_only = search(catalogue, SearchQuery(kind=CapabilityKind.flow))
    assert [r.entry.metadata.path for r in flows_only.results] == ["f/workflows/web/fetch_and_store"]


# ── Unsafe side-effect capabilities are not silently preferred ──────────────


def test_side_effect_free_entry_outranks_otherwise_identical_side_effecting_one():
    clean = make_entry(
        "f/capabilities/write/clean", CapabilityKind.script, ["write"], CapabilityEffects(), "write a thing"
    )
    risky = make_entry(
        "f/capabilities/write/risky",
        CapabilityKind.script,
        ["write"],
        CapabilityEffects(database=True),
        "write a thing",
    )
    catalogue = Catalogue(entries=[clean, risky])
    response = search(catalogue, SearchQuery(task="write a thing", tags=["write"]))
    assert [r.entry.metadata.path for r in response.results] == [
        "f/capabilities/write/clean",
        "f/capabilities/write/risky",
    ]
    assert response.results[0].score > response.results[1].score


# ── Search returns both primitives and workflows ─────────────────────────────


def test_search_can_return_both_scripts_and_flows_together():
    catalogue = make_fixture_catalogue()
    response = search(catalogue, SearchQuery(tags=["web"]))
    kinds = {r.entry.kind for r in response.results}
    assert kinds == {CapabilityKind.script, CapabilityKind.flow}


# ── Results carry compatibility and policy metadata ──────────────────────────


def test_results_carry_full_capability_metadata_not_just_a_path():
    catalogue = make_fixture_catalogue()
    response = search(catalogue, SearchQuery(tags=["database"]))
    result = response.results[0]
    assert result.entry.metadata.effects.database is True
    assert result.entry.metadata.autonomy.promote.value == "approval_required"
    assert result.entry.metadata.maturity.value == "stable"


def test_rationale_is_present_and_human_readable_for_scored_results():
    catalogue = make_fixture_catalogue()
    response = search(catalogue, SearchQuery(tags=["web", "fetch"]))
    assert all(result.rationale for result in response.results if result.score > 0)


# ── Integration against the real capability-index.yaml ──────────────────────


def test_search_against_real_capability_index():
    catalogue = load_catalogue(CATALOGUE_PATH.read_text())
    response = search(catalogue, SearchQuery(task="extract hacker news stories"))
    assert isinstance(response, SearchResponse)
    assert response.results
    assert response.results[0].entry.metadata.path == "f/data_platform/extract_hn_stories"


def test_search_against_real_capability_index_by_output_kind():
    catalogue = load_catalogue(CATALOGUE_PATH.read_text())
    response = search(catalogue, SearchQuery(required_output_kinds=["model_list"]))
    assert [r.entry.metadata.path for r in response.results] == ["f/hermes/client"]


# ── docs/CI: checked-in JSON Schema exports must match the models ───────────


def test_checked_in_json_schema_matches_model():
    schema_path = SCHEMAS_DIR / "search_response.schema.json"
    assert schema_path.exists(), (
        f"{schema_path} is missing — export it: "
        "python -c \"import json; from f.hermes_flow.catalogue.search import SearchResponse; "
        'print(json.dumps(SearchResponse.model_json_schema(), indent=2, sort_keys=True))" '
        f"> {schema_path}"
    )
    on_disk = json.loads(schema_path.read_text())
    current = json.loads(json.dumps(SearchResponse.model_json_schema(), sort_keys=True))
    assert on_disk == current, (
        f"{schema_path} is stale relative to SearchResponse — regenerate it (see this test's "
        "docstring command above) and commit the update"
    )
