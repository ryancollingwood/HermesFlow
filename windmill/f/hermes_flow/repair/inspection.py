"""HF-029: gather and classify a bounded, redacted repair context."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Optional, Protocol
from urllib.parse import quote

import wmill

from f.hermes_flow.candidate_ops.diff import _consumer_impact
from f.hermes_flow.catalogue.models import Catalogue, load_catalogue
from f.hermes_flow.repair.models import (
    ActiveCapabilityEvidence,
    ArtifactEvidence,
    BoundedDocument,
    DependencyEvidence,
    FailureCategory,
    FailureClassification,
    OriginalJob,
    RecentTestEvidence,
    RedactionSummary,
    RepairContext,
    RepairContextLimits,
    TruncationSummary,
)


class FailureInspectionError(RuntimeError):
    pass


class _Response(Protocol):
    status_code: int
    text: str

    def json(self) -> Any: ...


class WindmillReadClient(Protocol):
    workspace: str

    def get(self, path: str, raise_for_status: bool = True) -> _Response: ...


_SENSITIVE_KEY = re.compile(
    r"(?:^|[_-])(?:api[_-]?key|secret|password|passwd|token|authorization|cookie|"
    r"credential|private[_-]?key|client[_-]?secret)(?:$|[_-])",
    re.IGNORECASE,
)
_CONVERSATION_KEY = re.compile(
    r"^(?:conversation|conversation_id|messages|chat_history|prompt|system_prompt|"
    r"user_profile|memory|memories)$",
    re.IGNORECASE,
)
_TEXT_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/-]+=*"),
    re.compile(r"(?i)(api[_-]?key|secret|password|passwd|token|authorization|"
               r"client[_-]?secret)\s*[:=]\s*([^\s,;]+)"),
    re.compile(r"\b(?:gh[opsu]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{16,})\b"),
    re.compile(r"(?i)(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|https?)://"
               r"([^\s/@:]+):([^\s/@]+)@"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----.*?"
               r"-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", re.DOTALL),
)


def _clip(value: Any, limit: int) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 15)] + "...[truncated]"


def redact_text(value: str) -> tuple[str, int]:
    redacted = value
    count = 0
    for index, pattern in enumerate(_TEXT_SECRET_PATTERNS):
        def replace(match: re.Match) -> str:
            nonlocal count
            count += 1
            if index == 0:
                return f"{match.group(1)}[REDACTED]"
            if index == 1:
                return f"{match.group(1)}=[REDACTED]"
            if index == 3:
                return f"{match.group(0).split('://', 1)[0]}://[REDACTED]@"
            return "[REDACTED]"

        redacted = pattern.sub(replace, redacted)
    return redacted, count


def _sanitize(value: Any, path: str = "") -> tuple[Any, int, list[str]]:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        replacements = 0
        excluded: list[str] = []
        for raw_key, item in value.items():
            key = str(raw_key)
            item_path = f"{path}.{key}" if path else key
            if _CONVERSATION_KEY.match(key):
                excluded.append(item_path)
                continue
            if _SENSITIVE_KEY.search(key):
                result[key] = "[REDACTED]"
                replacements += 1
                continue
            cleaned, child_count, child_excluded = _sanitize(item, item_path)
            result[key] = cleaned
            replacements += child_count
            excluded.extend(child_excluded)
        return result, replacements, excluded
    if isinstance(value, (list, tuple)):
        result = []
        replacements = 0
        excluded: list[str] = []
        for index, item in enumerate(value):
            cleaned, child_count, child_excluded = _sanitize(item, f"{path}[{index}]")
            result.append(cleaned)
            replacements += child_count
            excluded.extend(child_excluded)
        return result, replacements, excluded
    if isinstance(value, str):
        cleaned, replacements = redact_text(value)
        return cleaned, replacements, []
    return value, 0, []


def _bounded_document(value: str, max_bytes: int) -> BoundedDocument:
    encoded = value.encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    if len(encoded) <= max_bytes:
        return BoundedDocument(
            content=value, original_bytes=len(encoded), retained_bytes=len(encoded), sha256=digest
        )
    marker = b"\n...[truncated by HF-029]...\n"
    available = max(0, max_bytes - len(marker))
    head_size = available * 3 // 4
    tail_size = available - head_size
    retained = encoded[:head_size] + marker + (encoded[-tail_size:] if tail_size else b"")
    content = retained.decode("utf-8", errors="replace")
    return BoundedDocument(
        content=content,
        original_bytes=len(encoded),
        retained_bytes=len(content.encode("utf-8")),
        truncated=True,
        sha256=digest,
    )


_CLASSIFIERS: tuple[tuple[FailureCategory, tuple[tuple[str, str], ...]], ...] = (
    (FailureCategory.policy, (
        (r"\b403\b|forbidden|permission denied|access denied", "authorization was denied"),
        (r"policy denied|approval required|ssrf|blocked by policy", "policy enforcement blocked execution"),
    )),
    (FailureCategory.input, (
        (r"validationerror|invalid (?:input|argument|url)|missing required|required field", "input validation failed"),
        (r"unprocessable entity|\b422\b|value_error", "input shape or value was rejected"),
    )),
    (FailureCategory.source_drift, (
        (r"parser (?:failed|error)|selector (?:missing|not found|no longer)", "source parser or selector no longer matched"),
        (r"markup (?:changed|drift)|schema drift|unexpected (?:html|payload|response shape)", "source format changed"),
    )),
    (FailureCategory.dependency, (
        (r"modulenotfounderror|importerror|no module named|version conflict", "runtime dependency is missing or incompatible"),
        (r"relation .* does not exist|undefinedtable|missing migration|schema .* not found", "database/schema dependency is unavailable"),
    )),
    (FailureCategory.infrastructure, (
        (r"connection refused|connecterror|endpoint unavailable|name or service not known|dns", "an external endpoint is unavailable"),
        (r"timed? out|timeout|service unavailable|bad gateway|gateway timeout|\b50[234]\b", "infrastructure failed or timed out"),
        (r"could not connect to server|database .* unavailable|operationalerror", "database infrastructure is unavailable"),
    )),
    (FailureCategory.code_defect, (
        (r"syntaxerror|nameerror|attributeerror|keyerror|indexerror|assertionerror", "code raised a programming error"),
        (r"traceback \(most recent call last\)|typeerror", "active code raised an unhandled exception"),
    )),
)


def classify_failure(*parts: Any) -> FailureClassification:
    evidence = "\n".join(str(part or "") for part in parts).lower()
    matches: dict[FailureCategory, list[str]] = {}
    for category, patterns in _CLASSIFIERS:
        for pattern, reason in patterns:
            if re.search(pattern, evidence, re.IGNORECASE):
                matches.setdefault(category, []).append(reason)
    if not matches:
        return FailureClassification(
            category=FailureCategory.unknown,
            confidence=0.25,
            reasons=["failure evidence did not match a deterministic classifier"],
        )
    # Classifier order is the tie-breaker: policy/input/source/dependency and
    # infrastructure signals are more specific than a generic traceback.
    order = {category: index for index, (category, _) in enumerate(_CLASSIFIERS)}
    category = min(matches, key=lambda item: (-len(matches[item]), order[item]))
    reasons = list(dict.fromkeys(matches[category]))
    confidence = min(0.95, 0.65 + 0.1 * (len(reasons) - 1))
    return FailureClassification(category=category, confidence=confidence, reasons=reasons)


def _artifact_evidence(value: Any) -> tuple[list[ArtifactEvidence], int]:
    found: list[ArtifactEvidence] = []
    seen: set[tuple[Optional[str], Optional[str]]] = set()
    replacements = 0

    def walk(item: Any) -> None:
        nonlocal replacements
        if isinstance(item, dict):
            if "artifact_id" in item or "storage_uri" in item:
                values = {}
                for field, limit in (
                    ("artifact_id", 200), ("stage", 100),
                    ("storage_uri", 2000), ("description", 1000),
                ):
                    cleaned, count = redact_text(str(item.get(field) or ""))
                    values[field] = _clip(cleaned, limit) or None
                    replacements += count
                identity = (values["artifact_id"], values["storage_uri"])
                if identity not in seen:
                    seen.add(identity)
                    found.append(ArtifactEvidence(
                        artifact_id=values["artifact_id"],
                        stage=values["stage"],
                        storage_uri=values["storage_uri"],
                        description=values["description"],
                    ))
            for child in item.values():
                walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)

    walk(value)
    return found, replacements


def _asset_kind(job: dict[str, Any]) -> str:
    return "flow" if job.get("is_flow") or job.get("job_kind") == "flow" else "script"


def _test_evidence(items: list[dict[str, Any]], capability_path: str) -> tuple[list[RecentTestEvidence], int, list[str]]:
    relevant = []
    replacements = 0
    excluded: list[str] = []
    for item in items:
        paths = item.get("capability_paths") or [item.get("capability_path")]
        if capability_path not in paths:
            continue
        details, count = redact_text(str(item.get("details") or ""))
        replacements += count
        relevant.append(RecentTestEvidence(
            test=_clip(item.get("test") or item.get("id") or "unknown-test", 500),
            status=_clip(item.get("status") or ("passed" if item.get("passed") else "failed"), 50),
            job_id=_clip(item.get("job_id"), 200) or None,
            recorded_at=_clip(item.get("recorded_at") or item.get("started_at"), 100) or None,
            details=_clip(details, 2000) or None,
        ))
    relevant.sort(key=lambda item: item.recorded_at or "", reverse=True)
    return relevant, replacements, excluded


def _fit_total(context: RepairContext) -> RepairContext:
    limit = context.limits.max_total_bytes
    while context.serialized_size() > limit:
        documents = [
            ("logs", context.logs),
            ("active_capability.code", context.active_capability.code),
            ("inputs", context.inputs),
        ]
        reducible = [(name, doc) for name, doc in documents if doc.retained_bytes > 256]
        if reducible:
            name, document = max(reducible, key=lambda pair: pair[1].retained_bytes)
            reduced = _bounded_document(document.content, max(256, document.retained_bytes // 2))
            reduced.original_bytes = document.original_bytes
            reduced.sha256 = document.sha256
            reduced.truncated = True
            if name == "logs":
                context.logs = reduced
            elif name == "inputs":
                context.inputs = reduced
            else:
                context.active_capability.code = reduced
            if name not in context.truncation.truncated_sections:
                context.truncation.truncated_sections.append(name)
            continue
        if context.recent_test_evidence:
            context.recent_test_evidence.pop()
            context.truncation.omitted_test_evidence += 1
            continue
        if context.dependency_impact:
            context.dependency_impact.pop()
            context.truncation.omitted_dependencies += 1
            continue
        if context.artifacts:
            context.artifacts.pop()
            context.truncation.omitted_artifacts += 1
            continue
        raise FailureInspectionError("repair context metadata exceeds max_total_bytes")
    for _ in range(3):
        context.total_bytes = context.serialized_size()
    if context.serialized_size() > limit:
        raise FailureInspectionError("repair context exceeds max_total_bytes after final sizing")
    return context


def build_repair_context(
    *,
    job: dict[str, Any],
    active_asset: dict[str, Any],
    catalogue: Catalogue,
    logs: str = "",
    recent_test_evidence: Optional[list[dict[str, Any]]] = None,
    workspace: str = "main",
    windmill_base_url: str = "http://windmill.localhost",
    limits: Optional[RepairContextLimits] = None,
    collection_warnings: Optional[list[str]] = None,
) -> RepairContext:
    bounds = limits or RepairContextLimits()
    if job.get("success") is True or str(job.get("status", "")).lower() in {"success", "succeeded"}:
        raise FailureInspectionError("HF-029 only inspects failed jobs")
    path = str(job.get("script_path") or job.get("path") or job.get("flow_path") or "").strip()
    job_id = str(job.get("id") or job.get("job_id") or "").strip()
    if not path or not job_id:
        raise FailureInspectionError("failed job must include id/job_id and script_path/path")

    sanitized_inputs, input_redactions, excluded = _sanitize(job.get("args") or {})
    inputs_json = json.dumps(sanitized_inputs, sort_keys=True, indent=2, default=str)
    sanitized_logs, log_redactions = redact_text(logs or str(job.get("logs") or ""))
    raw_code = active_asset.get("content")
    asset_kind = _asset_kind(job)
    if raw_code is None:
        raw_code = json.dumps(active_asset.get("value") or active_asset, sort_keys=True, indent=2, default=str)
    sanitized_code, code_redactions = redact_text(str(raw_code))
    failure_value = job.get("error") or job.get("failure_summary") or job.get("result") or "failed job"
    sanitized_failure, failure_redactions, failure_excluded = _sanitize(failure_value, "failure")
    excluded.extend(failure_excluded)
    failure_summary = _clip(
        json.dumps(sanitized_failure, sort_keys=True, default=str)
        if isinstance(sanitized_failure, (dict, list)) else sanitized_failure,
        4000,
    )

    entry = catalogue.get(path)
    version = entry.metadata.capability_version if entry else active_asset.get("version")
    impacts = _consumer_impact(catalogue, path) if entry else []
    dependency_evidence = [DependencyEvidence(
        path=_clip(item["path"], 500),
        relationship=item["impact"],
        distance=item["distance"],
        via=_clip(item.get("via"), 500) or None,
        tests=[_clip(test, 500) for test in item.get("tests", [])[:50]],
    ) for item in impacts]
    artifacts, artifact_redactions = _artifact_evidence({
        "inputs": job.get("args"),
        "result": job.get("result"),
    })
    tests, test_redactions, test_excluded = _test_evidence(
        recent_test_evidence or [], path
    )
    excluded.extend(test_excluded)

    retained_artifacts = artifacts[:bounds.max_artifacts]
    retained_dependencies = dependency_evidence[:bounds.max_dependencies]
    retained_tests = tests[:bounds.max_test_evidence]
    truncation = TruncationSummary(
        omitted_artifacts=max(0, len(artifacts) - len(retained_artifacts)),
        omitted_dependencies=max(0, len(dependency_evidence) - len(retained_dependencies)),
        omitted_test_evidence=max(0, len(tests) - len(retained_tests)),
    )
    code_document = _bounded_document(sanitized_code, bounds.max_code_bytes)
    input_document = _bounded_document(inputs_json, bounds.max_input_bytes)
    log_document = _bounded_document(sanitized_logs, bounds.max_log_bytes)
    for name, document in (
        ("active_capability.code", code_document), ("inputs", input_document), ("logs", log_document)
    ):
        if document.truncated:
            truncation.truncated_sections.append(name)

    context = RepairContext(
        original_job=OriginalJob(
            job_id=job_id,
            workspace=workspace,
            path=path,
            api_url=(
                f"{windmill_base_url.rstrip('/')}/api/w/{quote(workspace, safe='')}/"
                f"jobs_u/get/{quote(job_id, safe='')}"
            ),
        ),
        failure_summary=failure_summary or "failed job",
        classification=classify_failure(failure_summary, sanitized_logs),
        active_capability=ActiveCapabilityEvidence(
            path=path,
            capability_version=_clip(version, 200) or None,
            windmill_hash=_clip(active_asset.get("hash"), 500) or None,
            asset_kind=asset_kind,
            code=code_document,
        ),
        inputs=input_document,
        logs=log_document,
        artifacts=retained_artifacts,
        dependency_impact=retained_dependencies,
        recent_test_evidence=retained_tests,
        redaction=RedactionSummary(
            replacement_count=(input_redactions + log_redactions + code_redactions
                               + failure_redactions + test_redactions + artifact_redactions),
            excluded_fields=sorted(set(excluded))[:50],
        ),
        truncation=truncation,
        collection_warnings=[_clip(item, 1000) for item in (collection_warnings or [])[:20]],
        limits=bounds,
    )
    return _fit_total(context)


def inspect_failure_from_windmill(
    job_id: str,
    catalogue_yaml: str,
    *,
    recent_test_evidence: Optional[list[dict[str, Any]]] = None,
    windmill_base_url: str = "http://windmill.localhost",
    limits: Optional[RepairContextLimits] = None,
    client: Optional[WindmillReadClient] = None,
) -> RepairContext:
    windmill = client or wmill.Windmill()
    workspace = windmill.workspace
    response = windmill.get(
        f"/w/{workspace}/jobs_u/get/{quote(job_id, safe='')}", raise_for_status=False
    )
    if response.status_code != 200:
        raise FailureInspectionError(
            f"failed job {job_id!r} could not be loaded: HTTP {response.status_code}"
        )
    job = response.json()
    path = str(job.get("script_path") or job.get("path") or job.get("flow_path") or "")
    if not path:
        raise FailureInspectionError("failed job response did not identify its script/flow path")

    warnings: list[str] = []
    logs = str(job.get("logs") or "")
    if not logs:
        log_response = windmill.get(
            f"/w/{workspace}/jobs_u/get_logs/{quote(job_id, safe='')}", raise_for_status=False
        )
        if log_response.status_code == 200:
            try:
                payload = log_response.json()
                logs = payload if isinstance(payload, str) else json.dumps(payload, default=str)
            except Exception:
                logs = log_response.text
        else:
            warnings.append(f"job logs unavailable: HTTP {log_response.status_code}")

    asset_kind = _asset_kind(job)
    endpoint = (
        f"/w/{workspace}/flows/get/{quote(path, safe='/')}"
        if asset_kind == "flow"
        else f"/w/{workspace}/scripts/get/p/{quote(path, safe='/')}"
    )
    asset_response = windmill.get(endpoint, raise_for_status=False)
    if asset_response.status_code != 200:
        warnings.append(f"active {asset_kind} unavailable: HTTP {asset_response.status_code}")
        active_asset: dict[str, Any] = {"content": "[active code unavailable]"}
    else:
        active_asset = asset_response.json()

    return build_repair_context(
        job=job,
        active_asset=active_asset,
        catalogue=load_catalogue(catalogue_yaml),
        logs=logs,
        recent_test_evidence=recent_test_evidence,
        workspace=workspace,
        windmill_base_url=windmill_base_url,
        limits=limits,
        collection_warnings=warnings,
    )


def main(
    job_id: str,
    catalogue_yaml: str,
    recent_test_evidence: Optional[list[dict]] = None,
    windmill_base_url: str = "http://windmill.localhost",
    max_total_bytes: int = 131_072,
) -> dict:
    context = inspect_failure_from_windmill(
        job_id,
        catalogue_yaml,
        recent_test_evidence=recent_test_evidence or [],
        windmill_base_url=windmill_base_url,
        limits=RepairContextLimits(max_total_bytes=max_total_bytes),
    )
    return context.model_dump(mode="json")
