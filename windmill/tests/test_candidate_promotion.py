"""HF-013 promotion workflow safety tests."""
import json
from dataclasses import dataclass, field

import pytest

from f.hermes_flow.candidate_ops.models import CandidateRecord, metadata_variable_path
from f.hermes_flow.candidate_ops.promote import (
    PromotionConflict,
    PromotionError,
    finalize_promotion,
    prepare_promotion,
    promotion_variable_path,
)


@dataclass
class Response:
    status_code: int
    body: object = field(default_factory=dict)

    def json(self):
        return self.body

    @property
    def text(self):
        return str(self.body)


class FakeWindmill:
    workspace = "main"

    def __init__(self):
        self.scripts = {
            "f/capabilities/base": {
                "hash": "base-v1", "content": "x = 1\n", "language": "python3",
                "schema": {}, "summary": "base", "description": "",
            },
            "f/hermes_flow/candidates/c1": {
                "hash": "candidate-v1", "content": "x = 2\n", "language": "python3",
                "schema": {}, "summary": "base", "description": "",
            },
        }
        record = CandidateRecord(
            candidate_id="c1", path="f/hermes_flow/candidates/c1", request_key="r1",
            reason="change", source_path="f/capabilities/base", base_version="base-v1",
        )
        self.variables = {metadata_variable_path("c1"): record.model_dump_json()}
        self.history = {"f/capabilities/base": ["base-v1"]}

    def get(self, path, raise_for_status=True):
        prefix = "/w/main/scripts/get/p/"
        if path.startswith(prefix):
            item = self.scripts.get(path[len(prefix):])
            return Response(200, item) if item else Response(404)
        prefix = "/w/main/variables/get/"
        if path.startswith(prefix):
            value = self.variables.get(path[len(prefix):])
            return Response(200, {"value": value}) if value else Response(404)
        raise AssertionError(path)

    def post(self, path, json, raise_for_status=True):
        if path == "/w/main/scripts/create":
            current = self.scripts[json["path"]]
            if json["parent_hash"] != current["hash"]:
                return Response(409, "lineage must be linear")
            new_hash = f"version-{len(self.history[json['path']]) + 1}"
            self.scripts[json["path"]] = {**current, **json, "hash": new_hash}
            self.history[json["path"]].append(new_hash)
            return Response(201, new_hash)
        if path == "/w/main/variables/create":
            self.variables[json["path"]] = json["value"]
            return Response(201)
        raise AssertionError(path)


CATALOGUE = """
schema_version: '1.0'
entries:
  - kind: script
    tags: []
    inputs_summary: input
    outputs_summary: output
    metadata:
      path: f/capabilities/base
      capability_version: '1.0.0'
      summary: base
      maturity: stable
      owners: [platform]
      test_requirements: [tests/base]
"""


def prepared(fake):
    return prepare_promotion(
        "c1", CATALOGUE, [{"test": "tests/base", "passed": True, "job_id": "job-1"}],
        client=fake,
    )


def test_failed_required_test_blocks_before_write():
    fake = FakeWindmill()
    with pytest.raises(PromotionError, match="failed required tests"):
        prepare_promotion("c1", CATALOGUE, [{"test": "tests/base", "passed": False}], client=fake)
    assert fake.history["f/capabilities/base"] == ["base-v1"]


def test_missing_required_test_blocks_before_write():
    with pytest.raises(PromotionError, match="missing required tests"):
        prepare_promotion("c1", CATALOGUE, [], client=FakeWindmill())


def test_unknown_capability_is_denied_by_policy():
    fake = FakeWindmill()
    with pytest.raises(PromotionError, match="denied by policy"):
        prepare_promotion("c1", "schema_version: '1.0'\nentries: []\n", [], client=fake)


def test_approval_required_path_rejects_missing_approval():
    fake = FakeWindmill()
    with pytest.raises(PromotionError, match="requires approval"):
        finalize_promotion(prepared(fake), approval_granted=False, client=fake)
    assert fake.history["f/capabilities/base"] == ["base-v1"]


def test_approval_required_path_rejects_an_unattributed_approval():
    fake = FakeWindmill()
    with pytest.raises(PromotionError, match="authenticated approver identity"):
        finalize_promotion(prepared(fake), approval_granted=True, client=fake)


def test_approval_required_path_promotes_and_records_provenance_and_rollback():
    fake = FakeWindmill()
    result = finalize_promotion(
        prepared(fake), approval_granted=True, approved_by="u/reviewer", client=fake
    )
    assert result["candidate_id"] == "c1"
    assert result["base_version"] == "base-v1"
    assert result["rollback_target"] == "base-v1"
    assert result["promoted_version"] == "version-2"
    assert result["approved_by"] == "u/reviewer"
    assert promotion_variable_path("c1") in fake.variables
    assert fake.history["f/capabilities/base"] == ["base-v1", "version-2"]
    assert "candidate=c1" in fake.scripts["f/capabilities/base"]["deployment_message"]


def test_automatic_policy_path_does_not_require_approval():
    fake = FakeWindmill()
    evidence = prepared(fake)
    evidence["policy"]["outcome"] = "automatic"
    result = finalize_promotion(evidence, approval_granted=False, client=fake)
    assert result["promoted_version"] == "version-2"


def test_stale_base_version_conflicts_without_overwrite():
    fake = FakeWindmill()
    evidence = prepared(fake)
    fake.scripts["f/capabilities/base"]["hash"] = "concurrent-v2"
    fake.history["f/capabilities/base"].append("concurrent-v2")
    with pytest.raises(PromotionConflict, match="active version changed"):
        finalize_promotion(
            evidence, approval_granted=True, approved_by="u/reviewer", client=fake
        )
    assert fake.history["f/capabilities/base"] == ["base-v1", "concurrent-v2"]


def test_persisted_provenance_is_machine_readable():
    fake = FakeWindmill()
    finalize_promotion(
        prepared(fake), approval_granted=True, approved_by="u/reviewer", client=fake
    )
    value = json.loads(fake.variables[promotion_variable_path("c1")])
    assert value["required_tests"][0]["job_id"] == "job-1"
