"""HF-035 retention classes, expiry selection, and cost estimation tests."""
import json
import pathlib
from datetime import datetime, timedelta, timezone

import pytest
from f.libraries.retention.models import (
    DEFAULT_RETENTION_POLICIES,
    RetentionClass,
    RetentionPolicy,
    estimate_cost_usd,
    is_expired,
    select_expired,
)

SCHEMAS_DIR = pathlib.Path(__file__).parent.parent.parent / "docs" / "schemas"


# ── Documented retention classes for the five named data categories ─────────


@pytest.mark.parametrize(
    "category", ["job_logs", "prompts", "conversations", "raw_artifacts", "datasets"]
)
def test_every_hf035_named_category_has_a_documented_default_policy(category):
    policy = DEFAULT_RETENTION_POLICIES[category]
    assert policy.category == category
    assert isinstance(policy.retention_class, RetentionClass)
    assert policy.notes  # every default policy documents its rationale


def test_short_term_categories_have_an_age_bound_job_logs_have_no_tombstone():
    assert DEFAULT_RETENTION_POLICIES["job_logs"].retention_class is RetentionClass.short_term
    assert DEFAULT_RETENTION_POLICIES["job_logs"].requires_tombstone is False
    assert DEFAULT_RETENTION_POLICIES["raw_artifacts"].requires_tombstone is True


def test_datasets_default_to_long_term_with_no_age_bound():
    policy = DEFAULT_RETENTION_POLICIES["datasets"]
    assert policy.retention_class is RetentionClass.long_term
    assert policy.max_age_seconds is None


# ── Expiry selection ─────────────────────────────────────────────────────────


def test_is_expired_true_at_and_past_the_boundary():
    policy = RetentionPolicy(
        category="test", retention_class=RetentionClass.short_term, max_age_seconds=100
    )
    assert is_expired(policy, 100) is True
    assert is_expired(policy, 101) is True
    assert is_expired(policy, 99) is False


def test_is_expired_always_false_with_no_max_age():
    policy = RetentionPolicy(category="test", retention_class=RetentionClass.long_term)
    assert is_expired(policy, 10_000_000) is False


def test_is_expired_rejects_negative_age():
    policy = RetentionPolicy(
        category="test", retention_class=RetentionClass.short_term, max_age_seconds=100
    )
    with pytest.raises(ValueError, match="negative"):
        is_expired(policy, -1)


def test_select_expired_picks_only_items_past_the_window():
    policy = RetentionPolicy(
        category="raw_artifacts", retention_class=RetentionClass.short_term,
        max_age_seconds=30 * 86_400,
    )
    now = datetime(2026, 7, 14, tzinfo=timezone.utc)
    items = [
        ("fresh", now - timedelta(days=1)),
        ("boundary", now - timedelta(days=30)),
        ("old", now - timedelta(days=45)),
    ]
    assert select_expired(items, policy, now=now) == ["boundary", "old"]


def test_select_expired_returns_nothing_for_a_policy_with_no_age_bound():
    policy = DEFAULT_RETENTION_POLICIES["datasets"]
    items = [("ancient", datetime(2000, 1, 1, tzinfo=timezone.utc))]
    assert select_expired(items, policy, now=datetime(2026, 1, 1, tzinfo=timezone.utc)) == []


def test_select_expired_rejects_naive_datetimes():
    policy = RetentionPolicy(
        category="test", retention_class=RetentionClass.short_term, max_age_seconds=100
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        select_expired([("x", datetime(2026, 1, 1))], policy)


def test_expiry_selection_in_a_disposable_environment_end_to_end():
    """Testing-guidance scenario: seed a disposable set of (id, created_at) pairs and
    confirm only the ones outside the policy's window are selected for deletion."""
    policy = DEFAULT_RETENTION_POLICIES["job_logs"]  # 30-day short_term
    now = datetime(2026, 7, 14, tzinfo=timezone.utc)
    disposable_items = [
        (f"job-{days}d-old", now - timedelta(days=days)) for days in (1, 10, 29, 30, 31, 90)
    ]
    expired = select_expired(disposable_items, policy, now=now)
    assert expired == ["job-30d-old", "job-31d-old", "job-90d-old"]


# ── Cost estimation ──────────────────────────────────────────────────────────


def test_estimate_cost_usd_from_token_usage():
    usage = {"prompt_tokens": 2_000, "completion_tokens": 500}
    cost = estimate_cost_usd(
        usage, price_per_1k_prompt_tokens=0.01, price_per_1k_completion_tokens=0.03
    )
    assert cost == pytest.approx(2 * 0.01 + 0.5 * 0.03)


def test_estimate_cost_usd_defaults_to_zero_price():
    assert estimate_cost_usd({"prompt_tokens": 1_000, "completion_tokens": 1_000}) == 0.0


def test_estimate_cost_usd_handles_missing_usage_fields():
    assert estimate_cost_usd({}, price_per_1k_prompt_tokens=1.0) == 0.0


def test_estimate_cost_usd_rejects_negative_prices():
    with pytest.raises(ValueError, match="negative"):
        estimate_cost_usd({}, price_per_1k_prompt_tokens=-1.0)


def test_estimate_cost_usd_rejects_negative_token_counts():
    with pytest.raises(ValueError, match="negative"):
        estimate_cost_usd({"prompt_tokens": -5})


# ── docs/CI: checked-in JSON Schema export must match the model ────────────


def test_checked_in_schema_matches_model():
    schema_path = SCHEMAS_DIR / "retention_policy.schema.json"
    on_disk = json.loads(schema_path.read_text())
    current = json.loads(json.dumps(RetentionPolicy.model_json_schema(), sort_keys=True))
    assert on_disk == current


# ── main() self-test entrypoint ─────────────────────────────────────────────


def test_main_exports_schema_and_default_policy_table():
    from f.libraries.retention.models import main

    result = main()
    assert "RetentionPolicy" in result
    assert set(result["default_policies"]) == {
        "job_logs", "prompts", "conversations", "raw_artifacts", "datasets",
    }
