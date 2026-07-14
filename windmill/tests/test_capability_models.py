"""Unit tests for f/libraries/capability/models.py — not synced to Windmill (see conftest.py)."""
import json
import pathlib

import pytest
from pydantic import ValidationError

from f.libraries.capability.models import (
    ALWAYS_APPROVAL_REQUIRED,
    AutonomyAction,
    AutonomyLevel,
    AutonomyPolicy,
    CapabilityEffects,
    CapabilityLimits,
    CapabilityMaturity,
    CapabilityMetadata,
)

SCHEMAS_DIR = pathlib.Path(__file__).parent.parent.parent / "docs" / "schemas"


def make_metadata(**overrides) -> CapabilityMetadata:
    defaults = dict(
        path="f/capabilities/collection/web_fetch",
        capability_version="1.0.0",
        summary="Fetch a URL and return its raw content.",
        maturity=CapabilityMaturity.stable,
        owners=["collection-team"],
    )
    defaults.update(overrides)
    return CapabilityMetadata(**defaults)


# ── AutonomyPolicy: supports the seven lifecycle actions ────────────────────


def test_autonomy_policy_covers_all_seven_actions():
    policy = AutonomyPolicy()
    for action in AutonomyAction:
        assert policy.level_for(action) in (AutonomyLevel.automatic, AutonomyLevel.approval_required)


def test_autonomy_policy_defaults_bounded_actions_automatic():
    policy = AutonomyPolicy()
    for action in (
        AutonomyAction.discover,
        AutonomyAction.execute,
        AutonomyAction.compose,
        AutonomyAction.create_candidate,
        AutonomyAction.modify_candidate,
    ):
        assert policy.level_for(action) is AutonomyLevel.automatic


def test_autonomy_policy_defaults_promote_and_schedule_to_approval():
    policy = AutonomyPolicy()
    assert policy.promote is AutonomyLevel.approval_required
    assert policy.schedule is AutonomyLevel.approval_required


# ── Effects: network, filesystem, database, external ─────────────────────────


def test_capability_effects_covers_four_kinds():
    effects = CapabilityEffects(network=True, filesystem=True, database=True, external=True)
    assert (effects.network, effects.filesystem, effects.database, effects.external) == (
        True,
        True,
        True,
        True,
    )


def test_capability_effects_default_is_side_effect_free():
    assert CapabilityEffects().is_side_effect_free is True
    assert CapabilityEffects(network=True).is_side_effect_free is False


# ── Test requirements and dependency declarations ────────────────────────────


def test_metadata_supports_test_requirements_and_dependencies():
    meta = make_metadata(
        test_requirements=["windmill/tests/contracts/web_fetch_basic.py"],
        dependencies=["f/libraries/web/http_client"],
    )
    assert meta.test_requirements == ["windmill/tests/contracts/web_fetch_basic.py"]
    assert meta.dependencies == ["f/libraries/web/http_client"]


def test_metadata_cannot_depend_on_itself():
    with pytest.raises(ValidationError):
        make_metadata(path="f/capabilities/collection/web_fetch", dependencies=["f/capabilities/collection/web_fetch"])


# ── Missing policy / invalid values / unsafe defaults ────────────────────────


@pytest.mark.parametrize("missing_field", ["path", "capability_version", "summary", "maturity", "owners"])
def test_metadata_missing_required_field_rejected(missing_field):
    kwargs = dict(
        path="f/capabilities/collection/web_fetch",
        capability_version="1.0.0",
        summary="Fetch a URL.",
        maturity=CapabilityMaturity.stable,
        owners=["collection-team"],
    )
    del kwargs[missing_field]
    with pytest.raises(ValidationError):
        CapabilityMetadata(**kwargs)


def test_metadata_empty_owners_rejected():
    with pytest.raises(ValidationError):
        make_metadata(owners=[])


def test_metadata_invalid_maturity_value_rejected():
    with pytest.raises(ValidationError):
        make_metadata(maturity="extremely stable")  # not a CapabilityMaturity member


def test_metadata_invalid_autonomy_value_rejected():
    with pytest.raises(ValidationError):
        AutonomyPolicy(discover="whenever-i-feel-like-it")


@pytest.mark.parametrize("action", ["promote", "schedule"])
def test_unsafe_default_automatic_promotion_or_schedule_is_rejected(action):
    # An "unsafe default" attempt: explicitly trying to mark promote/schedule
    # automatic must fail validation, not silently succeed.
    with pytest.raises(ValidationError):
        AutonomyPolicy(**{action: AutonomyLevel.automatic})


def test_limits_reject_non_positive_values():
    with pytest.raises(ValidationError):
        CapabilityLimits(timeout_seconds=0)
    with pytest.raises(ValidationError):
        CapabilityLimits(max_concurrency=-1)


# ── HF-035: size/record-count/cost limits alongside pre-existing duration ──


def test_hf035_limits_default_unbounded():
    limits = CapabilityLimits()
    assert limits.max_response_bytes is None
    assert limits.max_record_count is None
    assert limits.max_cost_usd is None


@pytest.mark.parametrize("field", ["max_response_bytes", "max_record_count", "max_cost_usd"])
def test_hf035_limits_reject_non_positive_values(field):
    with pytest.raises(ValidationError):
        CapabilityLimits(**{field: 0})
    with pytest.raises(ValidationError):
        CapabilityLimits(**{field: -1})


def test_hf035_limits_accept_positive_values():
    limits = CapabilityLimits(max_response_bytes=1_000_000, max_record_count=500, max_cost_usd=2.5)
    assert limits.max_response_bytes == 1_000_000
    assert limits.max_record_count == 500
    assert limits.max_cost_usd == 2.5


# ── A low-risk label alone cannot imply promotion/scheduling permission ─────


def test_low_risk_maturity_and_effects_do_not_unlock_promotion_or_schedule():
    # Most conservative-looking capability possible: stable maturity, zero
    # declared effects. Its autonomy policy must still default to (and only
    # accept) approval_required for promote/schedule — nothing about being
    # "low risk" changes that.
    meta = make_metadata(
        maturity=CapabilityMaturity.stable,
        effects=CapabilityEffects(),  # no network/filesystem/database/external
    )
    assert meta.effects.is_side_effect_free
    for action in ALWAYS_APPROVAL_REQUIRED:
        assert meta.autonomy.level_for(action) is AutonomyLevel.approval_required


def test_no_field_combination_can_construct_an_automatic_promote_policy():
    # Sweep every maturity value alongside an all-automatic-except-required
    # attempt; the promote/schedule validator must reject every one.
    for maturity in CapabilityMaturity:
        for action in ("promote", "schedule"):
            with pytest.raises(ValidationError):
                make_metadata(
                    maturity=maturity,
                    effects=CapabilityEffects(),
                    autonomy=AutonomyPolicy(**{action: AutonomyLevel.automatic}),
                )


# ── Worked examples: a read-only web capability and a write capability ──────


def test_example_read_only_web_capability():
    web_fetch = CapabilityMetadata(
        path="f/capabilities/collection/web_fetch",
        capability_version="1.0.0",
        summary="Fetch a URL from an allow-listed domain and return the raw response body.",
        maturity=CapabilityMaturity.stable,
        owners=["collection-team"],
        effects=CapabilityEffects(network=True),
        limits=CapabilityLimits(timeout_seconds=30, rate_limit_per_minute=60),
        test_requirements=["windmill/tests/contracts/web_fetch_ssrf.py"],
    )
    assert web_fetch.effects.network is True
    assert web_fetch.effects.database is False
    assert web_fetch.autonomy.execute is AutonomyLevel.automatic
    assert web_fetch.autonomy.promote is AutonomyLevel.approval_required


def test_example_write_capability():
    snapshot_write = CapabilityMetadata(
        path="f/capabilities/collection/product_snapshot_write",
        capability_version="1.0.0",
        summary="Upsert a product snapshot row into collection_db, keyed by execution/source/product.",
        maturity=CapabilityMaturity.experimental,
        owners=["collection-team"],
        effects=CapabilityEffects(database=True),
        limits=CapabilityLimits(timeout_seconds=15, max_concurrency=4),
        dependencies=["f/libraries/lineage/models"],
        test_requirements=[
            "windmill/tests/contracts/product_snapshot_idempotent_upsert.py",
        ],
    )
    assert snapshot_write.effects.database is True
    assert snapshot_write.effects.is_side_effect_free is False
    # Even a write capability's execute action is bounded-automatic (it's the
    # *promotion* of new/changed code to active that's gated, not routine
    # execution of an already-active, already-reviewed capability).
    assert snapshot_write.autonomy.execute is AutonomyLevel.automatic
    assert snapshot_write.autonomy.promote is AutonomyLevel.approval_required
    assert snapshot_write.autonomy.schedule is AutonomyLevel.approval_required


# ── docs/CI: checked-in JSON Schema exports must match the models ───────────


@pytest.mark.parametrize(
    "model,filename",
    [
        (CapabilityMetadata, "capability_metadata.schema.json"),
        (AutonomyPolicy, "autonomy_policy.schema.json"),
    ],
)
def test_checked_in_json_schema_matches_model(model, filename):
    schema_path = SCHEMAS_DIR / filename
    assert schema_path.exists(), (
        f"{schema_path} is missing — export it: "
        f"python -c \"import json; from f.libraries.capability.models import {model.__name__}; "
        f"print(json.dumps({model.__name__}.model_json_schema(), indent=2, sort_keys=True))\" "
        f"> {schema_path}"
    )
    on_disk = json.loads(schema_path.read_text())
    current = json.loads(json.dumps(model.model_json_schema(), sort_keys=True))
    assert on_disk == current, (
        f"{schema_path} is stale relative to {model.__name__} — regenerate it (see this test's "
        "docstring command above) and commit the update"
    )
