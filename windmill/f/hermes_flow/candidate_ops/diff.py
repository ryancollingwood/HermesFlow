"""Candidate diff and dependency-impact analysis (HF-012).

The pure ``analyse_candidate`` function compares normalized candidate and active
snapshots. ``main`` is the Windmill entrypoint: it fetches the candidate record
created by HF-011, the candidate/active scripts, and accepts the versioned
catalogue YAML used for consumer traversal.
"""
from __future__ import annotations

import difflib
from collections import deque
from typing import Any, Protocol

import wmill
from f.hermes_flow.candidate_ops.models import CandidateRecord, metadata_variable_path
from f.hermes_flow.catalogue.models import Catalogue, load_catalogue


class CandidateAnalysisError(ValueError):
    pass


class _Response(Protocol):
    status_code: int

    def json(self) -> dict: ...


class WindmillReadClient(Protocol):
    workspace: str

    def get(self, path: str, raise_for_status: bool = True) -> _Response: ...


_SCRIPT_METADATA_FIELDS = ("summary", "description", "language", "tag")


def _json_changes(before: Any, after: Any, prefix: str = "") -> list[dict]:
    """Return stable leaf-level changes suitable for humans and machines."""
    if before == after:
        return []
    if isinstance(before, dict) and isinstance(after, dict):
        changes = []
        for key in sorted(set(before) | set(after)):
            path = f"{prefix}.{key}" if prefix else key
            if key not in before:
                changes.append({"path": path, "change": "added", "before": None, "after": after[key]})
            elif key not in after:
                changes.append({"path": path, "change": "removed", "before": before[key], "after": None})
            else:
                changes.extend(_json_changes(before[key], after[key], path))
        return changes
    if isinstance(before, list) and isinstance(after, list):
        # Schemas and metadata often use ordered lists (required, owners, enums),
        # so replacing a list is more truthful than pretending it is a set.
        return [{"path": prefix or "$", "change": "modified", "before": before, "after": after}]
    return [{"path": prefix or "$", "change": "modified", "before": before, "after": after}]


def _code_diff(before: str, after: str, active_path: str, candidate_path: str) -> dict:
    changed = before != after
    unified = ""
    if changed:
        unified = "".join(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile=active_path,
                tofile=candidate_path,
            )
        )
    return {"changed": changed, "unified_diff": unified}


def _consumer_impact(catalogue: Catalogue, changed_path: str) -> list[dict]:
    """Traverse reverse dependencies breadth-first; visited makes cycles safe."""
    reverse: dict[str, set[str]] = {}
    for entry in catalogue.entries:
        for dependency in entry.metadata.dependencies:
            reverse.setdefault(dependency, set()).add(entry.metadata.path)

    impacted: list[dict] = []
    visited = {changed_path}
    queue = deque((consumer, 1, changed_path) for consumer in sorted(reverse.get(changed_path, set())))
    while queue:
        path, depth, via = queue.popleft()
        if path in visited:
            continue
        visited.add(path)
        entry = catalogue.get(path)
        impacted.append(
            {
                "path": path,
                "distance": depth,
                "impact": "direct" if depth == 1 else "transitive",
                "via": via,
                "tests": sorted(entry.metadata.test_requirements) if entry else [],
            }
        )
        for consumer in sorted(reverse.get(path, set())):
            if consumer not in visited:
                queue.append((consumer, depth + 1, path))
    return impacted


def analyse_candidate(
    *,
    candidate: dict,
    active: dict,
    candidate_path: str,
    active_path: str,
    catalogue: Catalogue,
    candidate_capability_metadata: dict | None = None,
) -> dict:
    """Compare snapshots and return the HF-012 promotion evidence envelope."""
    active_entry = catalogue.get(active_path)
    active_capability_metadata = (
        active_entry.metadata.model_dump(mode="json") if active_entry is not None else {}
    )
    proposed_metadata = (
        active_capability_metadata
        if candidate_capability_metadata is None
        else candidate_capability_metadata
    )

    code = _code_diff(active.get("content", ""), candidate.get("content", ""), active_path, candidate_path)
    schema_changes = _json_changes(active.get("schema", {}), candidate.get("schema", {}))
    script_metadata_changes = _json_changes(
        {key: active.get(key) for key in _SCRIPT_METADATA_FIELDS},
        {key: candidate.get(key) for key in _SCRIPT_METADATA_FIELDS},
    )
    # Dependencies have their own first-class category/report below.
    active_non_dependency_metadata = {
        key: value for key, value in active_capability_metadata.items() if key != "dependencies"
    }
    proposed_non_dependency_metadata = {
        key: value for key, value in proposed_metadata.items() if key != "dependencies"
    }
    capability_changes = _json_changes(
        active_non_dependency_metadata, proposed_non_dependency_metadata
    )
    metadata_changes = script_metadata_changes + [
        {**change, "path": f"capability.{change['path']}"} for change in capability_changes
    ]

    old_dependencies = set(active_capability_metadata.get("dependencies", []))
    new_dependencies = set(proposed_metadata.get("dependencies", []))
    dependency_changes = {
        "changed": old_dependencies != new_dependencies,
        "added": sorted(new_dependencies - old_dependencies),
        "removed": sorted(old_dependencies - new_dependencies),
    }
    impacts = _consumer_impact(catalogue, active_path)
    changed_tests = sorted(proposed_metadata.get("test_requirements", []))
    required_tests = sorted(set(changed_tests).union(*(item["tests"] for item in impacts)))
    categories = {
        "code": code["changed"],
        "schema": bool(schema_changes),
        "metadata": bool(metadata_changes),
        "dependencies": dependency_changes["changed"],
    }
    no_changes = not any(categories.values())
    changed_names = [name for name, changed in categories.items() if changed]
    summary = (
        f"No changes detected between {candidate_path} and {active_path}."
        if no_changes
        else f"Changes detected in {', '.join(changed_names)}; {len(impacts)} consumer(s) impacted "
        f"and {len(required_tests)} test(s) required."
    )
    return {
        "candidate_path": candidate_path,
        "active_path": active_path,
        "no_changes": no_changes,
        "change_categories": categories,
        "diff": {
            "code": code,
            "schema": {"changed": bool(schema_changes), "changes": schema_changes},
            "metadata": {"changed": bool(metadata_changes), "changes": metadata_changes},
            "dependencies": dependency_changes,
        },
        "impact": {"consumers": impacts, "affected_count": len(impacts)},
        "promotion_summary": {"text": summary, "required_tests": required_tests},
    }


def _get_script(client: WindmillReadClient, path: str) -> dict:
    response = client.get(f"/w/{client.workspace}/scripts/get/p/{path}", raise_for_status=False)
    if response.status_code != 200:
        raise CandidateAnalysisError(f"script {path!r} was not found")
    return response.json()


def analyse_candidate_from_windmill(
    candidate_id: str,
    catalogue_yaml: str,
    candidate_capability_metadata: dict | None = None,
    client: WindmillReadClient | None = None,
) -> dict:
    w = client or wmill.Windmill()
    response = w.get(
        f"/w/{w.workspace}/variables/get/{metadata_variable_path(candidate_id)}",
        raise_for_status=False,
    )
    if response.status_code != 200:
        raise CandidateAnalysisError(f"candidate metadata for {candidate_id!r} was not found")
    record = CandidateRecord.model_validate_json(response.json()["value"])
    if not record.source_path:
        raise CandidateAnalysisError("candidate has no source_path, so there is no active version to diff")
    return analyse_candidate(
        candidate=_get_script(w, record.path),
        active=_get_script(w, record.source_path),
        candidate_path=record.path,
        active_path=record.source_path,
        catalogue=load_catalogue(catalogue_yaml),
        candidate_capability_metadata=candidate_capability_metadata,
    )


def main(candidate_id: str, catalogue_yaml: str, candidate_capability_metadata: dict | None = None) -> dict:
    return analyse_candidate_from_windmill(
        candidate_id, catalogue_yaml, candidate_capability_metadata=candidate_capability_metadata
    )
