"""Unit tests for f/hermes_flow/policies/evaluator.py — not synced to Windmill (see conftest.py)."""
import json
import pathlib

import pytest

from f.hermes_flow.policies.evaluator import PolicyContext, PolicyOutcome, evaluate_policy
from f.libraries.capability.models import (
    AutonomyAction,
    AutonomyLevel,
    AutonomyPolicy,
    CapabilityLimits,
    CapabilityMaturity,
    CapabilityMetadata,
)

SCHEMAS_DIR = pathlib.Path(__file__).parent.parent.parent / "docs" / "schemas"

ALL_ACTIONS = list(AutonomyAction)
NON_DISCOVER_ACTIONS = [a for a in ALL_ACTIONS if a is not AutonomyAction.discover]
BOUNDED_ACTIONS = [
    a
    for a in NON_DISCOVER_ACTIONS
    if a not in (AutonomyAction.promote, AutonomyAction.schedule)
]


def make_capability(**overrides) -> CapabilityMetadata:
    defaults = dict(
        path="f/capabilities/example/thing",
        capability_version="1.0.0",
        summary="an example capability",
        maturity=CapabilityMaturity.stable,
        owners=["x"],
    )
    defaults.update(overrides)
    return CapabilityMetadata(**defaults)


# ── discover is always automatic ─────────────────────────────────────────────


def test_discover_is_automatic_with_no_capability():
    decision = evaluate_policy(PolicyContext(action=AutonomyAction.discover))
    assert decision.outcome is PolicyOutcome.automatic


def test_discover_is_automatic_even_with_a_deprecated_capability():
    cap = make_capability(maturity=CapabilityMaturity.deprecated)
    decision = evaluate_policy(PolicyContext(action=AutonomyAction.discover, capability=cap))
    assert decision.outcome is PolicyOutcome.automatic


# ── Unknown capability fails closed for every action except discover ────────


@pytest.mark.parametrize("action", NON_DISCOVER_ACTIONS)
def test_unknown_capability_is_denied_for_every_non_discover_action(action):
    decision = evaluate_policy(PolicyContext(action=action, capability=None))
    assert decision.outcome is PolicyOutcome.denied
    assert "no CapabilityMetadata" in decision.reason


# ── Promote and schedule always require approval ─────────────────────────────


@pytest.mark.parametrize("action", [AutonomyAction.promote, AutonomyAction.schedule])
def test_promote_and_schedule_always_require_approval(action):
    cap = make_capability()
    decision = evaluate_policy(PolicyContext(action=action, capability=cap))
    assert decision.outcome is PolicyOutcome.approval_required


@pytest.mark.parametrize("action", [AutonomyAction.promote, AutonomyAction.schedule])
def test_promote_and_schedule_require_approval_regardless_of_destructive_flag(action):
    cap = make_capability()
    decision = evaluate_policy(PolicyContext(action=action, capability=cap, destructive=True))
    assert decision.outcome is PolicyOutcome.approval_required


# ── Bounded read-only candidate creation and execution can be automatic ─────


@pytest.mark.parametrize(
    "action",
    [
        AutonomyAction.execute,
        AutonomyAction.compose,
        AutonomyAction.create_candidate,
        AutonomyAction.modify_candidate,
    ],
)
def test_bounded_actions_are_automatic_by_default_for_a_known_capability(action):
    cap = make_capability()  # default AutonomyPolicy: these four are automatic
    decision = evaluate_policy(PolicyContext(action=action, capability=cap))
    assert decision.outcome is PolicyOutcome.automatic


# ── Destructive flag escalates automatic -> approval_required, never denies ─


@pytest.mark.parametrize("action", BOUNDED_ACTIONS)
def test_destructive_flag_escalates_automatic_to_approval_required(action):
    cap = make_capability()
    decision = evaluate_policy(PolicyContext(action=action, capability=cap, destructive=True))
    assert decision.outcome is PolicyOutcome.approval_required


def test_destructive_flag_does_not_further_escalate_an_already_gated_action():
    cap = make_capability(
        autonomy=AutonomyPolicy(execute=AutonomyLevel.approval_required)
    )
    decision = evaluate_policy(PolicyContext(action=AutonomyAction.execute, capability=cap, destructive=True))
    assert decision.outcome is PolicyOutcome.approval_required


# ── Excessive limits are denied, not routed to approval ──────────────────────


def test_requested_concurrency_exceeding_limit_is_denied():
    cap = make_capability(limits=CapabilityLimits(max_concurrency=4))
    decision = evaluate_policy(
        PolicyContext(action=AutonomyAction.execute, capability=cap, requested_concurrency=10)
    )
    assert decision.outcome is PolicyOutcome.denied
    assert "concurrency" in decision.reason


def test_requested_concurrency_within_limit_is_unaffected():
    cap = make_capability(limits=CapabilityLimits(max_concurrency=4))
    decision = evaluate_policy(
        PolicyContext(action=AutonomyAction.execute, capability=cap, requested_concurrency=2)
    )
    assert decision.outcome is PolicyOutcome.automatic


def test_requested_rate_exceeding_limit_is_denied():
    cap = make_capability(limits=CapabilityLimits(rate_limit_per_minute=60))
    decision = evaluate_policy(
        PolicyContext(action=AutonomyAction.execute, capability=cap, requested_rate_per_minute=120)
    )
    assert decision.outcome is PolicyOutcome.denied
    assert "rate" in decision.reason


def test_no_requested_limits_means_no_limit_check_applies():
    # Capability declares a limit, but the request doesn't say how much it needs —
    # nothing to compare, so this isn't a violation.
    cap = make_capability(limits=CapabilityLimits(max_concurrency=4))
    decision = evaluate_policy(PolicyContext(action=AutonomyAction.execute, capability=cap))
    assert decision.outcome is PolicyOutcome.automatic


def test_capability_with_no_declared_limit_cannot_be_exceeded():
    cap = make_capability()  # limits all None (unbounded)
    decision = evaluate_policy(
        PolicyContext(action=AutonomyAction.execute, capability=cap, requested_concurrency=1000)
    )
    assert decision.outcome is PolicyOutcome.automatic


# ── HF-035: duration/size/record-count/cost limits, same deny-not-approval rule ──


def test_requested_duration_exceeding_timeout_is_denied():
    cap = make_capability(limits=CapabilityLimits(timeout_seconds=30))
    decision = evaluate_policy(
        PolicyContext(action=AutonomyAction.execute, capability=cap, requested_duration_seconds=60)
    )
    assert decision.outcome is PolicyOutcome.denied
    assert "duration" in decision.reason


def test_requested_response_bytes_exceeding_limit_is_denied():
    cap = make_capability(limits=CapabilityLimits(max_response_bytes=1_000))
    decision = evaluate_policy(
        PolicyContext(action=AutonomyAction.execute, capability=cap, requested_response_bytes=5_000)
    )
    assert decision.outcome is PolicyOutcome.denied
    assert "response size" in decision.reason


def test_requested_record_count_exceeding_limit_is_denied():
    cap = make_capability(limits=CapabilityLimits(max_record_count=100))
    decision = evaluate_policy(
        PolicyContext(action=AutonomyAction.execute, capability=cap, requested_record_count=500)
    )
    assert decision.outcome is PolicyOutcome.denied
    assert "record count" in decision.reason


def test_requested_cost_exceeding_limit_is_denied():
    cap = make_capability(limits=CapabilityLimits(max_cost_usd=1.0))
    decision = evaluate_policy(
        PolicyContext(action=AutonomyAction.execute, capability=cap, requested_cost_usd=5.0)
    )
    assert decision.outcome is PolicyOutcome.denied
    assert "cost" in decision.reason


@pytest.mark.parametrize(
    "field,limit_field,limit_value,requested_value",
    [
        ("requested_duration_seconds", "timeout_seconds", 30, 10),
        ("requested_response_bytes", "max_response_bytes", 1_000, 500),
        ("requested_record_count", "max_record_count", 100, 50),
        ("requested_cost_usd", "max_cost_usd", 1.0, 0.5),
    ],
)
def test_hf035_requested_value_within_limit_is_unaffected(
    field, limit_field, limit_value, requested_value
):
    cap = make_capability(limits=CapabilityLimits(**{limit_field: limit_value}))
    decision = evaluate_policy(
        PolicyContext(action=AutonomyAction.execute, capability=cap, **{field: requested_value})
    )
    assert decision.outcome is PolicyOutcome.automatic


@pytest.mark.parametrize(
    "field",
    ["requested_duration_seconds", "requested_response_bytes", "requested_record_count", "requested_cost_usd"],
)
def test_hf035_declared_limit_without_a_matching_request_is_not_a_violation(field):
    cap = make_capability(limits=CapabilityLimits(
        timeout_seconds=30, max_response_bytes=1_000, max_record_count=100, max_cost_usd=1.0,
    ))
    decision = evaluate_policy(PolicyContext(action=AutonomyAction.execute, capability=cap))
    assert decision.outcome is PolicyOutcome.automatic


# ── Policy result includes decision, reason, and relevant metadata ──────────


def test_decision_includes_action_path_outcome_and_reason():
    cap = make_capability(path="f/capabilities/x/y")
    decision = evaluate_policy(PolicyContext(action=AutonomyAction.execute, capability=cap))
    assert decision.action is AutonomyAction.execute
    assert decision.capability_path == "f/capabilities/x/y"
    assert decision.outcome is PolicyOutcome.automatic
    assert decision.reason


def test_denied_decision_still_reports_the_action_even_without_a_path():
    decision = evaluate_policy(PolicyContext(action=AutonomyAction.execute))
    assert decision.action is AutonomyAction.execute
    assert decision.capability_path is None
    assert decision.reason


# ── Determinism: repeated evaluation of the same context is stable ──────────


def test_evaluation_is_deterministic_across_repeated_calls():
    cap = make_capability(limits=CapabilityLimits(max_concurrency=4))
    context = PolicyContext(action=AutonomyAction.execute, capability=cap, requested_concurrency=2)
    decisions = [evaluate_policy(context) for _ in range(5)]
    assert len({d.outcome for d in decisions}) == 1
    assert len({d.reason for d in decisions}) == 1


# ── Table-driven: every action x a representative context set ───────────────

TABLE = [
    # (action, has_capability, destructive, requested_concurrency, limit, expected_outcome)
    (AutonomyAction.discover, False, False, None, None, PolicyOutcome.automatic),
    (AutonomyAction.discover, True, False, None, None, PolicyOutcome.automatic),
    (AutonomyAction.execute, False, False, None, None, PolicyOutcome.denied),
    (AutonomyAction.execute, True, False, None, None, PolicyOutcome.automatic),
    (AutonomyAction.execute, True, True, None, None, PolicyOutcome.approval_required),
    (AutonomyAction.execute, True, False, 10, 4, PolicyOutcome.denied),
    (AutonomyAction.compose, True, False, None, None, PolicyOutcome.automatic),
    (AutonomyAction.create_candidate, True, False, None, None, PolicyOutcome.automatic),
    (AutonomyAction.create_candidate, True, True, None, None, PolicyOutcome.approval_required),
    (AutonomyAction.modify_candidate, True, False, None, None, PolicyOutcome.automatic),
    (AutonomyAction.promote, True, False, None, None, PolicyOutcome.approval_required),
    (AutonomyAction.promote, False, False, None, None, PolicyOutcome.denied),
    (AutonomyAction.schedule, True, False, None, None, PolicyOutcome.approval_required),
    (AutonomyAction.schedule, False, False, None, None, PolicyOutcome.denied),
]


@pytest.mark.parametrize(
    "action,has_capability,destructive,requested_concurrency,limit,expected", TABLE
)
def test_policy_table(action, has_capability, destructive, requested_concurrency, limit, expected):
    cap = None
    if has_capability:
        cap = make_capability(
            limits=CapabilityLimits(max_concurrency=limit) if limit is not None else CapabilityLimits()
        )
    context = PolicyContext(
        action=action,
        capability=cap,
        destructive=destructive,
        requested_concurrency=requested_concurrency,
    )
    assert evaluate_policy(context).outcome is expected


# ── Integration: main() self-test entrypoint ─────────────────────────────────


def test_main_evaluates_a_json_context():
    from f.hermes_flow.policies.evaluator import main

    cap = make_capability()
    context_json = json.dumps({"action": "execute", "capability": cap.model_dump(mode="json")})
    result = main(context_json)
    assert result["outcome"] == "automatic"
    assert result["capability_path"] == cap.path


# ── docs/CI: checked-in JSON Schema export must match the model ─────────────


def test_checked_in_json_schema_matches_model():
    from f.hermes_flow.policies.evaluator import PolicyDecision

    schema_path = SCHEMAS_DIR / "policy_decision.schema.json"
    assert schema_path.exists(), (
        f"{schema_path} is missing — export it: "
        "python -c \"import json; from f.hermes_flow.policies.evaluator import PolicyDecision; "
        'print(json.dumps(PolicyDecision.model_json_schema(), indent=2, sort_keys=True))" '
        f"> {schema_path}"
    )
    on_disk = json.loads(schema_path.read_text())
    current = json.loads(json.dumps(PolicyDecision.model_json_schema(), sort_keys=True))
    assert on_disk == current, (
        f"{schema_path} is stale relative to PolicyDecision — regenerate it (see this test's "
        "docstring command above) and commit the update"
    )
