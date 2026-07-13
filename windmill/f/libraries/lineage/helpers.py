"""HF-018 context propagation and artifact-lineage helpers for Windmill flows."""
from __future__ import annotations

import json
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field

from f.libraries.lineage.models import ArtifactRef, ArtifactStage, ExecutionContext
from f.libraries.storage.artifacts import FilesystemArtifactStore


class LineageError(ValueError):
    pass


class LineageState(BaseModel):
    """Serializable, content-free state passed between Windmill flow steps."""

    schema_version: str = "1.0"
    root_trace_id: UUID
    contexts: dict[UUID, ExecutionContext] = Field(default_factory=dict)
    artifacts: dict[UUID, ArtifactRef] = Field(default_factory=dict)

    def to_json(self) -> str:
        return self.model_dump_json()

    @classmethod
    def from_json(cls, value: str) -> "LineageState":
        return cls.model_validate_json(value)


def begin_lineage(
    context: Optional[ExecutionContext] = None,
    *,
    capability: Optional[str] = None,
    capability_version: Optional[str] = None,
    initiating_actor: Optional[str] = None,
    conversation_id: Optional[str] = None,
    request_id: Optional[str] = None,
) -> tuple[LineageState, ExecutionContext]:
    """Create missing context exactly once, at the workflow boundary."""
    if context is None:
        missing = [
            name
            for name, value in (
                ("capability", capability),
                ("capability_version", capability_version),
                ("initiating_actor", initiating_actor),
            )
            if not value
        ]
        if missing:
            raise LineageError(f"workflow boundary is missing required fields: {missing}")
        context = ExecutionContext(
            capability=capability,
            capability_version=capability_version,
            initiating_actor=initiating_actor,
            conversation_id=conversation_id,
            request_id=request_id,
        )
    state = LineageState(root_trace_id=context.trace_id, contexts={context.trace_id: context})
    return state, context


def require_step_context(context: Optional[ExecutionContext]) -> ExecutionContext:
    """Steps fail closed instead of independently inventing a new trace."""
    if context is None:
        raise LineageError(
            "execution context is missing inside a workflow step; create it at the boundary"
        )
    return context


def child_context(
    state: LineageState,
    parent: ExecutionContext,
    *,
    capability: str,
    capability_version: str,
    initiating_actor: Optional[str] = None,
) -> ExecutionContext:
    require_step_context(parent)
    registered = state.contexts.get(parent.trace_id)
    if registered != parent:
        raise LineageError("parent context is not registered in this lineage run")
    child = ExecutionContext(
        parent_trace_id=parent.trace_id,
        conversation_id=parent.conversation_id,
        request_id=parent.request_id,
        capability=capability,
        capability_version=capability_version,
        initiating_actor=initiating_actor or parent.initiating_actor,
    )
    state.contexts[child.trace_id] = child
    return child


def record_artifact(
    state: LineageState,
    context: ExecutionContext,
    artifact: ArtifactRef,
) -> ArtifactRef:
    if state.contexts.get(context.trace_id) != context:
        raise LineageError("artifact context is not registered in this lineage run")
    if artifact.trace_id != context.trace_id:
        raise LineageError("artifact trace_id does not match its producing context")
    missing_inputs = [artifact_id for artifact_id in artifact.derived_from if artifact_id not in state.artifacts]
    if missing_inputs:
        raise LineageError(f"derived artifact references inputs outside this lineage run: {missing_inputs}")
    if artifact.artifact_id in state.artifacts and state.artifacts[artifact.artifact_id] != artifact:
        raise LineageError("artifact_id is already registered with different metadata")
    state.artifacts[artifact.artifact_id] = artifact
    return artifact


def write_artifact(
    state: LineageState,
    store: FilesystemArtifactStore,
    context: ExecutionContext,
    content: bytes | str,
    *,
    stage: ArtifactStage,
    media_type: Optional[str] = None,
    inputs: Optional[list[ArtifactRef]] = None,
    metadata: Optional[dict] = None,
) -> ArtifactRef:
    upstream = inputs or []
    for artifact in upstream:
        if state.artifacts.get(artifact.artifact_id) != artifact:
            raise LineageError("derived input is not registered in this lineage run")
    artifact = store.write(
        content,
        trace_id=context.trace_id,
        stage=stage,
        creator_capability=context.capability,
        creator_capability_version=context.capability_version,
        media_type=media_type,
        derived_from=[item.artifact_id for item in upstream],
        metadata=metadata,
    )
    return record_artifact(state, context, artifact)


def enumerate_artifact_chain(
    state: LineageState,
    final_artifact_ids: list[UUID],
) -> list[ArtifactRef]:
    """Return inputs-before-outputs order, deduplicated and cycle-safe."""
    ordered: list[ArtifactRef] = []
    visited: set[UUID] = set()
    visiting: set[UUID] = set()

    def visit(artifact_id: UUID) -> None:
        if artifact_id in visited:
            return
        if artifact_id in visiting:
            raise LineageError(f"artifact lineage cycle detected at {artifact_id}")
        artifact = state.artifacts.get(artifact_id)
        if artifact is None:
            raise LineageError(f"artifact {artifact_id} is not registered in this lineage run")
        if artifact.trace_id not in state.contexts:
            raise LineageError(f"artifact {artifact_id} belongs to an unknown execution trace")
        visiting.add(artifact_id)
        for parent_id in artifact.derived_from:
            visit(parent_id)
        visiting.remove(artifact_id)
        visited.add(artifact_id)
        ordered.append(artifact)

    for final_id in final_artifact_ids:
        visit(final_id)
    return ordered


def main(action: str, payload_json: str) -> dict:
    """Windmill boundary/child transport helper; artifact writes import this module directly."""
    payload = json.loads(payload_json)
    if action == "boundary":
        existing = ExecutionContext.model_validate(payload["context"]) if payload.get("context") else None
        state, context = begin_lineage(existing, **payload.get("boundary", {}))
        return {
            "state": state.model_dump(mode="json"),
            "context": context.model_dump(mode="json"),
        }
    if action == "child":
        state = LineageState.model_validate(payload["state"])
        parent = require_step_context(ExecutionContext.model_validate(payload.get("context")))
        child = child_context(state, parent, **payload["child"])
        return {
            "state": state.model_dump(mode="json"),
            "context": child.model_dump(mode="json"),
        }
    raise LineageError("action must be 'boundary' or 'child'")
