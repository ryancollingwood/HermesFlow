"""HF-019 schema-validated Hermes invocation with retained artifact lineage."""
from __future__ import annotations

import json
import re
from typing import Any, Optional

from jsonschema import Draft202012Validator
from pydantic import BaseModel, Field

from f.hermes.client import get_client, hermes_endpoint
from f.libraries.lineage.helpers import (
    LineageState,
    begin_lineage,
    enumerate_artifact_chain,
    require_step_context,
    write_artifact,
)
from f.libraries.lineage.models import ArtifactRef, ArtifactStage, ExecutionContext
from f.libraries.storage.artifacts import FilesystemArtifactStore

CAPABILITY_PATH = "f/libraries/ai/invoke_hermes_structured"
CAPABILITY_VERSION = "1.0.0"
_SENSITIVE_KEY = re.compile(r"(?:api[_-]?key|authorization|password|secret|token)", re.I)


class InvocationAttempt(BaseModel):
    attempt: int
    status: str
    error: Optional[str] = None
    validation_errors: list[str] = Field(default_factory=list)
    raw_artifact_id: Optional[str] = None
    usage: Optional[dict[str, Any]] = None


class StructuredInvocationResult(BaseModel):
    schema_version: str = "1.0"
    status: str
    parsed_output: Optional[Any] = None
    model: str
    parameters: dict[str, Any]
    usage: Optional[dict[str, Any]] = None
    attempts: list[InvocationAttempt]
    artifacts: list[ArtifactRef]
    lineage: LineageState


def _redact(value: Any, secrets: list[str]) -> Any:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if _SENSITIVE_KEY.search(str(key)) else _redact(item, secrets)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item, secrets) for item in value]
    if isinstance(value, tuple):
        return [_redact(item, secrets) for item in value]
    if isinstance(value, str):
        redacted = value
        for secret in sorted((secret for secret in secrets if secret), key=len, reverse=True):
            redacted = redacted.replace(secret, "[REDACTED]")
        return redacted
    return value


def build_request(
    task_prompt: str,
    conversation: list[dict],
    input_payload: Any,
    output_schema: dict,
    *,
    model: str,
    temperature: float,
    max_tokens: int,
) -> dict:
    messages = [
        {
            "role": "system",
            "content": (
                "Return only JSON matching the supplied response schema. "
                "Do not wrap it in Markdown."
            ),
        },
        *conversation,
        {
            "role": "user",
            "content": f"Task:\n{task_prompt}\n\nInput JSON:\n{json.dumps(input_payload, default=str)}",
        },
    ]
    return {
        "model": model,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "messages": messages,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "hermes_structured_output", "strict": True, "schema": output_schema},
        },
    }


def _parse_json(content: str) -> Any:
    stripped = content.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines)
    return json.loads(stripped)


def _usage_dict(response: Any) -> Optional[dict[str, Any]]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    return usage.model_dump() if hasattr(usage, "model_dump") else dict(usage)


def _add_usage(total: Optional[dict[str, Any]], current: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if current is None:
        return total
    aggregate = dict(total or {})
    for key, value in current.items():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            aggregate[key] = aggregate.get(key, 0) + value
        elif key not in aggregate:
            aggregate[key] = value
    return aggregate


def invoke_hermes_structured(
    conn: hermes_endpoint,
    task_prompt: str,
    conversation: list[dict],
    input_payload: Any,
    output_schema: dict,
    *,
    model: str = "hermes",
    temperature: float = 0.2,
    max_tokens: int = 2048,
    max_retries: int = 2,
    context: Optional[ExecutionContext] = None,
    lineage: Optional[LineageState] = None,
    store: Optional[FilesystemArtifactStore] = None,
    client=None,
    secret_values: Optional[list[str]] = None,
) -> StructuredInvocationResult:
    Draft202012Validator.check_schema(output_schema)
    if max_retries < 0 or max_retries > 10:
        raise ValueError("max_retries must be between 0 and 10")
    if lineage is None:
        lineage, context = begin_lineage(
            context,
            capability=CAPABILITY_PATH,
            capability_version=CAPABILITY_VERSION,
            initiating_actor="windmill",
        )
    else:
        context = require_step_context(context)
        if lineage.contexts.get(context.trace_id) != context:
            raise ValueError("provided context is not registered in the lineage state")
    artifact_store = store or FilesystemArtifactStore()
    secrets = [conn.get("api_key", ""), *(secret_values or [])]
    retained: list[ArtifactRef] = []

    prompt_artifact = write_artifact(
        lineage, artifact_store, context, _redact(task_prompt, secrets),
        stage=ArtifactStage.raw, media_type="text/plain; charset=utf-8",
        metadata={"kind": "task_prompt"},
    )
    conversation_artifact = write_artifact(
        lineage, artifact_store, context,
        json.dumps(_redact(conversation, secrets), sort_keys=True),
        stage=ArtifactStage.raw, media_type="application/json",
        metadata={"kind": "conversation"},
    )
    input_artifact = write_artifact(
        lineage, artifact_store, context,
        json.dumps(_redact(input_payload, secrets), sort_keys=True, default=str),
        stage=ArtifactStage.raw, media_type="application/json",
        metadata={"kind": "input_payload"},
    )
    retained.extend([prompt_artifact, conversation_artifact, input_artifact])

    request = build_request(
        task_prompt, conversation, input_payload, output_schema,
        model=model, temperature=temperature, max_tokens=max_tokens,
    )
    hermes = client or get_client(conn)
    attempts: list[InvocationAttempt] = []
    parsed_output = None
    usage = None
    actual_model = model
    success = False
    for attempt_number in range(1, max_retries + 2):
        try:
            response = hermes.chat.completions.create(**request)
            actual_model = getattr(response, "model", None) or model
            attempt_usage = _usage_dict(response)
            usage = _add_usage(usage, attempt_usage)
            raw = response.choices[0].message.content or ""
            raw_artifact = write_artifact(
                lineage, artifact_store, context, _redact(raw, secrets),
                stage=ArtifactStage.intermediate, media_type="application/json",
                inputs=[prompt_artifact, conversation_artifact, input_artifact],
                metadata={"kind": "raw_response", "attempt": attempt_number},
            )
            retained.append(raw_artifact)
            try:
                candidate = _parse_json(raw)
                errors = sorted(
                    Draft202012Validator(output_schema).iter_errors(candidate),
                    key=lambda error: list(error.path),
                )
                if errors:
                    messages = [
                        f"{'/'.join(str(part) for part in error.path) or '$'}: {error.message}"
                        for error in errors
                    ]
                    attempts.append(
                        InvocationAttempt(
                            attempt=attempt_number, status="validation_failed",
                            validation_errors=messages, raw_artifact_id=str(raw_artifact.artifact_id),
                            usage=attempt_usage,
                        )
                    )
                    continue
                parsed_output = candidate
                attempts.append(
                    InvocationAttempt(
                        attempt=attempt_number, status="passed",
                        raw_artifact_id=str(raw_artifact.artifact_id),
                        usage=attempt_usage,
                    )
                )
                parsed_artifact = write_artifact(
                    lineage, artifact_store, context,
                    json.dumps(_redact(candidate, secrets), sort_keys=True),
                    stage=ArtifactStage.final, media_type="application/json", inputs=[raw_artifact],
                    metadata={"kind": "parsed_output"},
                )
                retained.append(parsed_artifact)
                success = True
                break
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                attempts.append(
                    InvocationAttempt(
                        attempt=attempt_number, status="validation_failed", error=str(exc),
                        raw_artifact_id=str(raw_artifact.artifact_id),
                        usage=attempt_usage,
                    )
                )
        except Exception as exc:
            attempts.append(
                InvocationAttempt(attempt=attempt_number, status="request_failed", error=str(exc))
            )

    chain = enumerate_artifact_chain(lineage, [artifact.artifact_id for artifact in retained])
    return StructuredInvocationResult(
        status="success" if success else "failure",
        parsed_output=parsed_output,
        model=actual_model,
        parameters={
            "temperature": temperature,
            "max_tokens": max_tokens,
            "max_retries": max_retries,
        },
        usage=usage,
        attempts=attempts,
        artifacts=chain,
        lineage=lineage,
    )


def main(
    conn: hermes_endpoint,
    task_prompt: str,
    conversation: list[dict],
    input_payload: Any,
    output_schema: dict,
    model: str = "hermes",
    temperature: float = 0.2,
    max_tokens: int = 2048,
    max_retries: int = 2,
) -> dict:
    result = invoke_hermes_structured(
        conn, task_prompt, conversation, input_payload, output_schema,
        model=model, temperature=temperature,
        max_tokens=max_tokens, max_retries=max_retries,
    )
    return result.model_dump(mode="json")
