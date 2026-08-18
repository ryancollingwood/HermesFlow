"""HF-031: promote a failed source artifact into a sanitised regression fixture."""
from __future__ import annotations

import hashlib
import html
import json
import re
from html.parser import HTMLParser
from typing import Any, Literal
from uuid import UUID, uuid4

from f.libraries.lineage.models import ArtifactRef, ArtifactStage
from f.libraries.storage.artifacts import FilesystemArtifactStore
from pydantic import BaseModel, ConfigDict, Field, model_validator

CAPABILITY_PATH = "f/hermes_flow/repair/promote_fixture"
CAPABILITY_VERSION = "1.0.0"
_SENSITIVE_FIELD = re.compile(
    r"(?:^|[_-])(?:api[_-]?key|secret|password|passwd|token|authorization|cookie|"
    r"credential|private[_-]?key|client[_-]?secret|csrf)(?:$|[_-])",
    re.IGNORECASE,
)
_TEXT_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/-]+=*"),
    re.compile(
        r"(?i)(api[_-]?key|secret|password|passwd|token|authorization|"
        r"client[_-]?secret)\s*[:=]\s*([^\s,;]+)"
    ),
    re.compile(r"\b(?:gh[opsu]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{16,})\b"),
    re.compile(
        r"(?i)(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|https?)://"
        r"([^\s/@:]+):([^\s/@]+)@"
    ),
)
_JSON_SECRET_VALUE = re.compile(
    r'''(?i)(["'](?:api[_-]?key|secret|password|passwd|token|authorization|cookie|'''
    r'''credential|private[_-]?key|client[_-]?secret|csrf)["']\s*:\s*["'])'''
    r'''([^"']*)(["'])'''
)


class FixturePromotionError(ValueError):
    pass


class FixtureExpectedBehavior(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str = Field(..., min_length=1, max_length=4000)
    required_paths: list[str] = Field(default_factory=list, max_length=100)
    expected_values: dict[str, Any] = Field(default_factory=dict)
    minimum_item_counts: dict[str, int] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _has_machine_checkable_expectation(self) -> FixtureExpectedBehavior:
        if not (self.required_paths or self.expected_values or self.minimum_item_counts):
            raise ValueError("expected behavior must declare at least one assertion")
        if any(value < 0 for value in self.minimum_item_counts.values()):
            raise ValueError("minimum item counts must be non-negative")
        assertion_paths = [
            *self.required_paths,
            *self.expected_values.keys(),
            *self.minimum_item_counts.keys(),
        ]
        if any(
            _SENSITIVE_FIELD.search(segment)
            for path in assertion_paths
            for segment in re.split(r"[.\[\]]+", path)
            if segment
        ):
            raise ValueError("expected behavior must not assert sensitive fields")
        if _metadata_contains_secret(self.model_dump(mode="json")):
            raise ValueError("expected behavior must not contain sensitive fields or values")
        return self


class FixtureBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fixture_argument: str = Field(default="fixture_content", min_length=1, max_length=200)
    payload_mode: Literal["text", "json", "artifact_ref"] = "text"
    candidate_args: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: int = Field(default=60, ge=1, le=3600)
    max_data_bytes: int = Field(default=1_000_000, ge=1, le=100_000_000)

    @model_validator(mode="after")
    def _fixture_argument_is_reserved_for_fixture(self) -> FixtureBinding:
        if self.fixture_argument in self.candidate_args:
            raise ValueError("candidate_args must not override fixture_argument")
        if _metadata_contains_secret(self.candidate_args):
            raise ValueError("candidate_args must not contain sensitive fields or values")
        return self


class SanitizationRules(BaseModel):
    model_config = ConfigDict(extra="forbid")

    replacement: Literal["[SANITISED]"] = "[SANITISED]"
    redact_fields: list[str] = Field(default_factory=list, max_length=100)
    remove_fields: list[str] = Field(
        default_factory=lambda: [
            "generated_at", "updated_at", "timestamp", "request_id", "trace_id",
            "session_id", "nonce", "data-request-id", "data-timestamp", "data-session-id",
        ],
        max_length=100,
    )
    drop_html_comments: bool = True

    @model_validator(mode="after")
    def _field_rules_do_not_conflict(self) -> SanitizationRules:
        if any(not item.strip() for item in [*self.redact_fields, *self.remove_fields]):
            raise ValueError("sanitization field names must not be blank")
        overlap = sorted(
            {_normalise_name(item) for item in self.redact_fields}
            & {_normalise_name(item) for item in self.remove_fields}
        )
        if overlap:
            raise ValueError(f"fields cannot be both redacted and removed: {overlap}")
        return self


class SanitizationSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    redaction_count: int = Field(default=0, ge=0)
    removal_count: int = Field(default=0, ge=0)
    redacted_fields: list[str] = Field(default_factory=list)
    removed_fields: list[str] = Field(default_factory=list)
    input_bytes: int = Field(ge=0)
    output_bytes: int = Field(ge=0)
    rules_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class SourceDriftFixture(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    fixture_id: str = Field(..., pattern=r"^fixture/source-drift/[0-9a-f]{16}/[0-9a-f]{64}$")
    fixture_format: Literal["html", "json"]
    capability_path: str = Field(..., min_length=1, max_length=500)
    failed_job_id: str = Field(..., min_length=1, max_length=200)
    source_artifact_id: UUID
    source_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    fixture_artifact: ArtifactRef
    expected_behavior: FixtureExpectedBehavior
    binding: FixtureBinding
    sanitization: SanitizationSummary
    promotion_trace_id: UUID

    @model_validator(mode="after")
    def _fixture_identity_matches_artifact(self) -> SourceDriftFixture:
        if self.fixture_artifact.content_hash not in self.fixture_id:
            raise ValueError("fixture_id must contain the sanitised artifact content hash")
        if self.fixture_artifact.trace_id != self.promotion_trace_id:
            raise ValueError("fixture artifact trace must match promotion_trace_id")
        if self.source_artifact_id not in self.fixture_artifact.derived_from:
            raise ValueError("fixture artifact must derive from the failed source artifact")
        return self


def _normalise_name(value: str) -> str:
    return value.strip().lower().replace("-", "_")


def _redact_text(value: str, replacement: str) -> tuple[str, int]:
    count = 0

    def replace_json(match: re.Match) -> str:
        nonlocal count
        count += 1
        return f"{match.group(1)}{replacement}{match.group(3)}"

    redacted = _JSON_SECRET_VALUE.sub(replace_json, value)
    for index, pattern in enumerate(_TEXT_SECRET_PATTERNS):
        def replace(match: re.Match) -> str:
            nonlocal count
            count += 1
            if index == 0:
                return f"{match.group(1)}{replacement}"
            if index == 1:
                return f"{match.group(1)}={replacement}"
            if index == 3:
                return f"{match.group(0).split('://', 1)[0]}://{replacement}@"
            return replacement

        redacted = pattern.sub(replace, redacted)
    return redacted, count


def _metadata_contains_secret(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            _SENSITIVE_FIELD.search(str(key)) or _metadata_contains_secret(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_metadata_contains_secret(item) for item in value)
    if isinstance(value, str):
        _, count = _redact_text(value, "[SANITISED]")
        return count > 0
    return False


def _field_action(name: str, rules: SanitizationRules) -> str | None:
    normalised = _normalise_name(name)
    remove = {_normalise_name(item) for item in rules.remove_fields}
    redact = {_normalise_name(item) for item in rules.redact_fields}
    if normalised in remove:
        return "remove"
    if normalised in redact or _SENSITIVE_FIELD.search(normalised):
        return "redact"
    return None


def _sanitize_json_value(
    value: Any,
    rules: SanitizationRules,
    path: str,
    redacted_fields: list[str],
    removed_fields: list[str],
) -> Any:
    if isinstance(value, dict):
        result = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            item_path = f"{path}.{key}" if path else key
            action = _field_action(key, rules)
            if action == "remove":
                removed_fields.append(item_path)
                continue
            if action == "redact":
                result[key] = rules.replacement
                redacted_fields.append(item_path)
                continue
            result[key] = _sanitize_json_value(
                item, rules, item_path, redacted_fields, removed_fields
            )
        return result
    if isinstance(value, list):
        return [
            _sanitize_json_value(
                item, rules, f"{path}[{index}]", redacted_fields, removed_fields
            )
            for index, item in enumerate(value)
        ]
    if isinstance(value, str):
        cleaned, replacements = _redact_text(value, rules.replacement)
        redacted_fields.extend([path] * replacements)
        return cleaned
    return value


class _SanitizingHTMLParser(HTMLParser):
    def __init__(self, rules: SanitizationRules):
        super().__init__(convert_charrefs=False)
        self.rules = rules
        self.parts: list[str] = []
        self.redacted_fields: list[str] = []
        self.removed_fields: list[str] = []
        self.json_script_depth = 0

    def _attributes(self, tag: str, attrs: list[tuple[str, str | None]]) -> str:
        marker_sensitive = any(
            name.lower() in {"name", "property", "id"}
            and value is not None
            and _SENSITIVE_FIELD.search(value)
            for name, value in attrs
        )
        rendered = []
        for name, value in attrs:
            field = f"{tag}@{name}"
            action = _field_action(name, self.rules)
            if action == "remove":
                self.removed_fields.append(field)
                continue
            if action == "redact" or (
                marker_sensitive and name.lower() in {"content", "value"}
            ):
                value = self.rules.replacement
                self.redacted_fields.append(field)
            elif value is not None:
                value, count = _redact_text(value, self.rules.replacement)
                self.redacted_fields.extend([field] * count)
            rendered.append(name if value is None else f'{name}="{html.escape(value, quote=True)}"')
        return (" " + " ".join(rendered)) if rendered else ""

    def handle_starttag(self, tag, attrs):
        self.parts.append(f"<{tag}{self._attributes(tag, attrs)}>")
        if tag.lower() == "script" and any(
            name.lower() == "type" and value and "json" in value.lower()
            for name, value in attrs
        ):
            self.json_script_depth += 1

    def handle_startendtag(self, tag, attrs):
        self.parts.append(f"<{tag}{self._attributes(tag, attrs)}/>")

    def handle_endtag(self, tag):
        self.parts.append(f"</{tag}>")
        if tag.lower() == "script" and self.json_script_depth:
            self.json_script_depth -= 1

    def handle_data(self, data):
        if self.json_script_depth:
            try:
                payload = json.loads(data)
            except json.JSONDecodeError:
                pass
            else:
                redacted: list[str] = []
                removed: list[str] = []
                cleaned = _sanitize_json_value(payload, self.rules, "script", redacted, removed)
                self.redacted_fields.extend(redacted)
                self.removed_fields.extend(removed)
                self.parts.append(json.dumps(cleaned, sort_keys=True, separators=(",", ":")))
                return
        cleaned, count = _redact_text(data, self.rules.replacement)
        self.redacted_fields.extend(["#text"] * count)
        self.parts.append(cleaned)

    def handle_entityref(self, name):
        self.parts.append(f"&{name};")

    def handle_charref(self, name):
        self.parts.append(f"&#{name};")

    def handle_comment(self, data):
        if self.rules.drop_html_comments:
            self.removed_fields.append("#comment")
        else:
            self.parts.append(f"<!--{data}-->")

    def handle_decl(self, decl):
        self.parts.append(f"<!{decl}>")

    def handle_pi(self, data):
        self.parts.append(f"<?{data}>")


def sanitize_fixture(
    content: bytes,
    fixture_format: Literal["html", "json"],
    rules: SanitizationRules,
) -> tuple[bytes, SanitizationSummary]:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FixturePromotionError("source fixture must be UTF-8") from exc
    redacted_fields: list[str] = []
    removed_fields: list[str] = []
    if fixture_format == "json":
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise FixturePromotionError(f"source artifact is not valid JSON: {exc}") from exc
        sanitized = _sanitize_json_value(
            payload, rules, "", redacted_fields, removed_fields
        )
        output = (json.dumps(sanitized, sort_keys=True, indent=2) + "\n").encode()
    else:
        parser = _SanitizingHTMLParser(rules)
        parser.feed(text)
        parser.close()
        redacted_fields = parser.redacted_fields
        removed_fields = parser.removed_fields
        output = "".join(parser.parts).encode()
    rules_json = json.dumps(rules.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return output, SanitizationSummary(
        redaction_count=len(redacted_fields),
        removal_count=len(removed_fields),
        redacted_fields=sorted(set(redacted_fields)),
        removed_fields=sorted(set(removed_fields)),
        input_bytes=len(content),
        output_bytes=len(output),
        rules_sha256=hashlib.sha256(rules_json.encode()).hexdigest(),
    )


def _infer_format(media_type: str | None) -> Literal["html", "json"]:
    resolved = (media_type or "").split(";", 1)[0].strip().lower()
    if resolved in {"text/html", "application/xhtml+xml"}:
        return "html"
    if resolved == "application/json" or resolved.endswith("+json"):
        return "json"
    raise FixturePromotionError(
        "source artifact media_type must be HTML or JSON, or fixture_format must be supplied"
    )


def promote_source_drift_fixture(
    source_artifact: ArtifactRef | dict,
    failed_job_id: str,
    capability_path: str,
    expected_behavior: FixtureExpectedBehavior | dict,
    *,
    binding: FixtureBinding | dict | None = None,
    sanitization_rules: SanitizationRules | dict | None = None,
    fixture_format: Literal["html", "json"] | None = None,
    store: FilesystemArtifactStore | None = None,
) -> SourceDriftFixture:
    if not failed_job_id or len(failed_job_id) > 200:
        raise FixturePromotionError("failed_job_id must contain 1 to 200 characters")
    if not capability_path.startswith("f/") or len(capability_path) > 500:
        raise FixturePromotionError("capability_path must be a valid f/* path")
    if fixture_format is not None and fixture_format not in {"html", "json"}:
        raise FixturePromotionError("fixture_format must be html or json")
    source = (
        source_artifact if isinstance(source_artifact, ArtifactRef)
        else ArtifactRef.model_validate(source_artifact)
    )
    expected = (
        expected_behavior
        if isinstance(expected_behavior, FixtureExpectedBehavior)
        else FixtureExpectedBehavior.model_validate(expected_behavior)
    )
    resolved_binding = (
        binding if isinstance(binding, FixtureBinding)
        else FixtureBinding.model_validate(binding or {})
    )
    rules = (
        sanitization_rules
        if isinstance(sanitization_rules, SanitizationRules)
        else SanitizationRules.model_validate(sanitization_rules or {})
    )
    artifact_store = store or FilesystemArtifactStore()
    raw = artifact_store.read(source)
    resolved_format = fixture_format or _infer_format(source.media_type)
    sanitized, summary = sanitize_fixture(raw, resolved_format, rules)
    content_hash = artifact_store.hash_bytes(sanitized)
    capability_key = hashlib.sha256(capability_path.encode()).hexdigest()[:16]
    fixture_id = f"fixture/source-drift/{capability_key}/{content_hash}"
    promotion_trace_id = uuid4()
    media_type = "text/html; charset=utf-8" if resolved_format == "html" else "application/json"
    artifact = artifact_store.write(
        sanitized,
        trace_id=promotion_trace_id,
        stage=ArtifactStage.intermediate,
        creator_capability=CAPABILITY_PATH,
        creator_capability_version=CAPABILITY_VERSION,
        media_type=media_type,
        derived_from=[source.artifact_id],
        metadata={
            "kind": "source_drift_regression_fixture",
            "fixture_id": fixture_id,
            "failed_job_id": failed_job_id,
            "capability_path": capability_path,
            "source_artifact_id": str(source.artifact_id),
            "source_content_hash": source.content_hash,
            "expected_behavior": expected.model_dump(mode="json"),
            "binding": resolved_binding.model_dump(mode="json"),
            "sanitization": summary.model_dump(mode="json"),
        },
    )
    return SourceDriftFixture(
        fixture_id=fixture_id,
        fixture_format=resolved_format,
        capability_path=capability_path,
        failed_job_id=failed_job_id,
        source_artifact_id=source.artifact_id,
        source_content_hash=source.content_hash,
        fixture_artifact=artifact,
        expected_behavior=expected,
        binding=resolved_binding,
        sanitization=summary,
        promotion_trace_id=promotion_trace_id,
    )


def main(
    source_artifact: dict,
    failed_job_id: str,
    capability_path: str,
    expected_behavior: dict,
    binding: dict | None = None,
    sanitization_rules: dict | None = None,
    fixture_format: str = "",
) -> dict:
    result = promote_source_drift_fixture(
        source_artifact,
        failed_job_id,
        capability_path,
        expected_behavior,
        binding=binding,
        sanitization_rules=sanitization_rules,
        fixture_format=fixture_format or None,
    )
    return result.model_dump(mode="json")
