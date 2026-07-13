"""HF-028 Hermes skill, prompt evaluation, policy, and opt-in live E2E tests."""
from __future__ import annotations

import os
import re
import subprocess
import importlib.util
import sys
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import yaml

from f.hermes_flow.catalogue.models import load_catalogue
from f.hermes_flow.catalogue.search import SearchQuery, search
from f.hermes_flow.policies.evaluator import PolicyContext, PolicyOutcome, evaluate_policy
from f.libraries.capability.models import AutonomyAction


ROOT = Path(__file__).parents[2]
SKILL_DIR = ROOT / "hermes" / "skills" / "workflow-orchestration" / "hermesflow"
SKILL = SKILL_DIR / "SKILL.md"
EXEMPLAR = SKILL_DIR / "references" / "product-collection-exemplar.md"
EVALUATIONS = Path(__file__).parent / "fixtures" / "hermesflow_conversation_prompts.yaml"
CATALOGUE = ROOT / "windmill" / "capability-index.yaml"
WORKFLOW_PATH = "f/workflows/product_collection"
MCP_SCRIPT = SKILL_DIR / "scripts" / "product_collection_mcp.py"


def load_mcp_module():
    spec = importlib.util.spec_from_file_location("product_collection_mcp", MCP_SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def frontmatter(path: Path) -> dict:
    text = path.read_text()
    assert text.startswith("---\n")
    header, _body = text[4:].split("\n---\n", 1)
    return yaml.safe_load(header)


def evaluation_cases() -> list[dict]:
    payload = yaml.safe_load(EVALUATIONS.read_text())
    assert payload["schema_version"] == "1.0"
    return payload["cases"]


def search_case(case: dict):
    expected = case["expected"]["search"]
    catalogue = load_catalogue(CATALOGUE.read_text())
    response = search(catalogue, SearchQuery(
        task=case["prompt"],
        tags=expected["tags"],
        required_output_kinds=expected["required_output_kinds"],
    ))
    return catalogue, response


def test_skill_metadata_routes_product_collection_requests():
    metadata = frontmatter(SKILL)
    assert metadata["name"] == "hermesflow"
    assert metadata["version"] == "0.3.0"
    description = metadata["description"].lower()
    assert all(term in description for term in ("product collection", "comparison", "windmill"))


def test_skill_contains_search_policy_execution_and_presentation_contract():
    skill = SKILL.read_text()
    exemplar = EXEMPLAR.read_text()
    assert "references/product-collection-exemplar.md" in skill
    assert "Always search Windmill" in skill
    assert all(tool in skill for tool in (
        "listFlows", "getFlowByPath", "run_product_collection", "getJob"
    ))
    assert "Ask one focused clarification" in skill
    assert re.search(r"do not recite all\s+internal primitives", skill, re.IGNORECASE)
    assert all(field in exemplar for field in (
        "f/workflows/product_collection",
        '"db": "$res:f/collection/collection_db"',
        '"enable_ai_fallback": false',
        "workflow version",
        "Windmill job ID",
        "artifact references",
    ))


def test_prompt_evaluation_set_covers_required_scenarios():
    cases = evaluation_cases()
    assert [case["id"] for case in cases] == [
        "direct-match", "varied-sources", "ambiguous-request", "unsupported-write"
    ]
    assert {case["expected"]["intent"] for case in cases} == {
        "supported_read", "ambiguous", "unsupported_write"
    }
    assert all("search" in case["expected"] for case in cases)


@pytest.mark.parametrize("case", evaluation_cases(), ids=lambda case: case["id"])
def test_prompt_search_and_policy_decisions(case):
    catalogue, response = search_case(case)
    expected = case["expected"]
    expected_path = expected["search"]["top_path"]
    actual_path = response.results[0].entry.metadata.path if response.results else None
    assert actual_path == expected_path

    if expected["action"] == "execute":
        selected = catalogue.get(actual_path)
        decision = evaluate_policy(PolicyContext(
            action=AutonomyAction.execute,
            capability=selected.metadata,
            requested_concurrency=expected["requested_concurrency"],
            destructive=False,
        ))
        assert decision.outcome.value == expected["policy"] == "automatic"
        assert expected["response_fields"] == [
            "workflow_path", "capability_version", "job", "artifacts"
        ]
    elif expected["action"] == "deny":
        decision = evaluate_policy(PolicyContext(
            action=AutonomyAction.execute,
            capability=None,
            destructive=True,
        ))
        assert decision.outcome is PolicyOutcome.denied
        assert expected["policy"] == "denied"
    else:
        assert expected["action"] == "clarify"
        assert expected["policy"] == "not_evaluated"


@pytest.mark.parametrize(
    "case",
    [case for case in evaluation_cases() if case["expected"]["action"] == "execute"],
    ids=lambda case: case["id"],
)
def test_supported_prompts_have_explicit_narrow_source_domains(case):
    urls = re.findall(r"https?://[^\s,]+", case["prompt"])
    domains = [urlsplit(url.rstrip(".")).hostname for url in urls]
    assert domains == case["expected"]["source_domains"]
    assert len(domains) == case["expected"]["requested_concurrency"]
    assert len(domains) <= 20


def test_product_collection_transport_fixes_sensitive_arguments():
    transport = load_mcp_module()
    request = transport.ProductCollectionRequest(sources=[{
        "source_id": "example",
        "label": "Example",
        "url": "https://example.com/products",
        "allowed_domains": ["example.com"],
    }])
    arguments = transport.flow_arguments(request)
    assert arguments["db"] == "$res:f/collection/collection_db"
    assert arguments["enable_ai_fallback"] is False
    assert transport.FLOW_PATH == WORKFLOW_PATH


@pytest.mark.parametrize("source", [
    {
        "source_id": "mismatch", "label": "Mismatch",
        "url": "https://example.com/", "allowed_domains": ["other.example"],
    },
    {
        "source_id": "scheme", "label": "Scheme",
        "url": "file:///etc/passwd", "allowed_domains": ["example.com"],
    },
])
def test_product_collection_transport_rejects_unsafe_sources(source):
    transport = load_mcp_module()
    with pytest.raises(ValueError):
        transport.ProductCollectionRequest(sources=[source])


def test_product_collection_transport_has_no_arbitrary_target_or_schedule():
    source = MCP_SCRIPT.read_text()
    assert 'FLOW_PATH = "f/workflows/product_collection"' in source
    assert "schedule" not in transport_parameter_names(load_mcp_module())
    assert "path" not in transport_parameter_names(load_mcp_module())


def transport_parameter_names(transport) -> set[str]:
    import inspect

    return set(inspect.signature(transport.run_product_collection).parameters)


@pytest.mark.skipif(
    os.environ.get("HF_RUN_HERMES_LIVE") != "1",
    reason="set HF_RUN_HERMES_LIVE=1 after deploying the skill and flow to run live E2E",
)
def test_live_hermes_to_windmill_and_back():
    prompt = (
        "Compare product information from https://example.com/ in a one-off read-only "
        "run, with example.com as the exact and only allowed source hostname. Search Windmill "
        "first, inspect and execute the existing product collection flow automatically if "
        "policy permits, wait for completion, then report the exact workflow path and version, "
        "Windmill job ID, and artifact references."
    )
    completed = subprocess.run(
        [
            "docker", "exec", "hermes", "hermes", "chat", "-Q",
            "-t", "windmill,hermesflow,clarify", "-s", "hermesflow", "-q", prompt,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=300,
    )
    output = re.sub(
        r"\x1b\[[0-?]*[ -/]*[@-~]", "", completed.stdout + completed.stderr
    )
    assert WORKFLOW_PATH in output
    assert re.search(r"\b1\.0\.0\b", output)
    assert re.search(r"\b[0-9a-f]{8}-[0-9a-f-]{27,}\b", output, re.IGNORECASE)
    assert "artifact" in output.lower()
