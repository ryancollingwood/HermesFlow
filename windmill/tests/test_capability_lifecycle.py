"""HF-014 deprecation and rollback tests."""
import json
from dataclasses import dataclass

import pytest

from f.hermes_flow.candidate_ops.lifecycle import (
    deprecate_capability,
    lifecycle_variable_path,
    rollback_capability,
)
from f.hermes_flow.candidate_ops.promote import PromotionError
from f.hermes_flow.catalogue.models import load_catalogue
from f.hermes_flow.catalogue.search import SearchQuery, search


@dataclass
class Response:
    status_code: int
    body: object = None

    def json(self): return self.body
    @property
    def text(self): return str(self.body)


class FakeWindmill:
    workspace = "main"

    def __init__(self):
        self.scripts = {
            "f/capabilities/base": {"hash": "broken-v2", "content": "broken", "language": "python3", "schema": {}},
        }
        self.versions = {
            "good-v1": {"hash": "good-v1", "content": "good", "language": "python3", "schema": {}},
        }
        self.variables = {}
        self.schedules = {
            "f/capabilities/base": [{"path": "s/base", "script_path": "f/capabilities/base"}],
            "f/workflows/consumer": [{"path": "s/consumer", "script_path": "f/workflows/consumer"}],
        }
        self.history = ["good-v1", "broken-v2"]

    def get(self, path, raise_for_status=True):
        prefix = "/w/main/scripts/get/p/"
        if path.startswith(prefix):
            value = self.scripts.get(path[len(prefix):])
            return Response(200, value) if value else Response(404)
        prefix = "/w/main/scripts/get/h/"
        if path.startswith(prefix):
            value = self.versions.get(path[len(prefix):])
            return Response(200, value) if value else Response(404)
        if path.startswith("/w/main/schedules/list?path="):
            encoded = path.split("?path=", 1)[1].split("&", 1)[0]
            from urllib.parse import unquote
            return Response(200, self.schedules.get(unquote(encoded), []))
        raise AssertionError(path)

    def post(self, path, json, raise_for_status=True):
        if path == "/w/main/scripts/create":
            current = self.scripts[json["path"]]
            if json["parent_hash"] != current["hash"]:
                return Response(409, "conflict")
            new_hash = "rollback-v3"
            self.scripts[json["path"]] = {**current, **json, "hash": new_hash}
            self.versions[new_hash] = self.scripts[json["path"]]
            self.history.append(new_hash)
            return Response(201, new_hash)
        if path == "/w/main/variables/create":
            self.variables[json["path"]] = json["value"]
            return Response(201)
        raise AssertionError(path)


CATALOGUE = """
schema_version: '1.0'
entries:
  - kind: script
    tags: [base]
    inputs_summary: input
    outputs_summary: output
    metadata:
      path: f/capabilities/base
      capability_version: '2.0.0'
      summary: base
      maturity: stable
      owners: [platform]
      test_requirements: [tests/base-smoke]
  - kind: flow
    tags: [consumer]
    inputs_summary: input
    outputs_summary: output
    metadata:
      path: f/workflows/consumer
      capability_version: '1.0.0'
      summary: consumer
      maturity: stable
      owners: [platform]
      dependencies: [f/capabilities/base]
      test_requirements: [tests/consumer-smoke]
"""


def passing_tests():
    return [
        {"test": "tests/base-smoke", "passed": True, "job_id": "test-1"},
        {"test": "tests/consumer-smoke", "passed": True, "job_id": "test-2"},
    ]


def test_deprecation_updates_catalogue_and_default_search_excludes_capability():
    fake = FakeWindmill()
    result = deprecate_capability(CATALOGUE, "f/capabilities/base", "superseded", "job-dep", client=fake)
    updated = load_catalogue(result["updated_catalogue_yaml"])
    paths = [item.entry.metadata.path for item in search(updated, SearchQuery()).results]
    assert "f/capabilities/base" not in paths
    assert lifecycle_variable_path("f/capabilities/base", "deprecations", "job-dep") in fake.variables


def test_deprecation_impact_lists_workflows_and_schedules():
    result = deprecate_capability(CATALOGUE, "f/capabilities/base", "broken", "job-dep", client=FakeWindmill())
    assert result["impact"]["workflows"] == ["f/workflows/consumer"]
    assert [s["path"] for s in result["impact"]["schedules"]] == ["s/base", "s/consumer"]


def test_rollback_restores_historical_content_as_a_new_version_and_preserves_history():
    fake = FakeWindmill()
    result = rollback_capability(
        CATALOGUE, "f/capabilities/base", "good-v1", "promotion broke", "job-rb",
        passing_tests(), expected_current_version="broken-v2", client=fake,
    )
    assert fake.scripts["f/capabilities/base"]["content"] == "good"
    assert fake.history == ["good-v1", "broken-v2", "rollback-v3"]
    assert result["record"]["failed_version"] == "broken-v2"
    assert result["record"]["rollback_version"] == "rollback-v3"
    assert result["record"]["reason"] == "promotion broke"
    assert result["record"]["initiating_job_id"] == "job-rb"


def test_rollback_requires_changed_and_consumer_smoke_tests_to_rerun():
    with pytest.raises(PromotionError, match="missing required tests"):
        rollback_capability(
            CATALOGUE, "f/capabilities/base", "good-v1", "broken", "job-rb", [], client=FakeWindmill()
        )


def test_rollback_record_retains_affected_schedules_and_test_jobs():
    fake = FakeWindmill()
    rollback_capability(
        CATALOGUE, "f/capabilities/base", "good-v1", "broken", "job-rb", passing_tests(), client=fake
    )
    raw = fake.variables[lifecycle_variable_path("f/capabilities/base", "rollbacks", "job-rb")]
    record = json.loads(raw)
    assert [s["path"] for s in record["affected_schedules"]] == ["s/base", "s/consumer"]
    assert [t["job_id"] for t in record["rerun_tests"]] == ["test-1", "test-2"]
