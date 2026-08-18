"""HF-021 policy-bounded HTTP fetch with raw artifact retention."""
from __future__ import annotations

import ipaddress
import socket
import time
from collections.abc import Callable
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from f.libraries.lineage.helpers import (
    LineageState,
    begin_lineage,
    require_step_context,
    write_artifact,
)
from f.libraries.lineage.models import ArtifactRef, ArtifactStage, ExecutionContext
from f.libraries.storage.artifacts import FilesystemArtifactStore
from pydantic import BaseModel, Field

CAPABILITY_PATH = "f/capabilities/collection/web_fetch"
CAPABILITY_VERSION = "1.0.0"
RETRYABLE_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})
SAFE_RESPONSE_HEADERS = frozenset({
    "cache-control", "content-length", "content-type", "etag", "last-modified", "location",
})
FORBIDDEN_REQUEST_HEADERS = frozenset({
    "connection", "host", "proxy-authorization", "proxy-connection", "transfer-encoding",
})


class FetchPolicyError(ValueError):
    """The request was rejected before contacting the target."""


class FetchAttempt(BaseModel):
    attempt: int
    status: str
    status_code: int | None = None
    error: str | None = None


class WebFetchResult(BaseModel):
    schema_version: str = "1.0"
    status: str
    requested_url: str
    final_url: str | None = None
    status_code: int | None = None
    content_type: str | None = None
    headers_summary: dict[str, str] = Field(default_factory=dict)
    raw_artifact: ArtifactRef | None = None
    attempts: list[FetchAttempt]
    redirects: list[str] = Field(default_factory=list)
    retryable: bool = False
    error: str | None = None
    lineage: LineageState


def _default_resolver(host: str, port: int) -> list[str]:
    return sorted({item[4][0] for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})


def _normalized_host(host: str) -> str:
    stripped = host.rstrip(".")
    if not stripped:
        raise FetchPolicyError("hostname cannot be empty")
    try:
        return stripped.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise FetchPolicyError("URL hostname is not valid IDNA") from exc


def _domain_allowed(host: str, allowed_domains: list[str]) -> bool:
    normalized = _normalized_host(host)
    for allowed in allowed_domains:
        candidate = _normalized_host(allowed)
        if normalized == candidate or normalized.endswith(f".{candidate}"):
            return True
    return False


def _public_address(address: str) -> bool:
    try:
        return ipaddress.ip_address(address).is_global
    except ValueError as exc:
        raise FetchPolicyError(f"resolver returned invalid IP address {address!r}") from exc


def _validate_target(
    url: str,
    allowed_domains: list[str],
    resolver: Callable[[str, int], list[str]],
) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"}:
        raise FetchPolicyError("only http and https URLs are allowed")
    if parsed.username is not None or parsed.password is not None:
        raise FetchPolicyError("credentials embedded in URLs are not allowed")
    if not parsed.hostname:
        raise FetchPolicyError("URL must include a hostname")
    host = _normalized_host(parsed.hostname)
    if not allowed_domains or not _domain_allowed(host, allowed_domains):
        raise FetchPolicyError(f"hostname {host!r} is not in allowed_domains")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise FetchPolicyError("URL port is invalid") from exc
    try:
        addresses = resolver(host, port)
    except (OSError, socket.gaierror) as exc:
        raise FetchPolicyError(f"hostname {host!r} could not be resolved") from exc
    if not addresses:
        raise FetchPolicyError(f"hostname {host!r} resolved to no addresses")
    blocked = sorted(address for address in addresses if not _public_address(address))
    if blocked:
        raise FetchPolicyError(
            f"hostname {host!r} resolves to non-public address(es): {', '.join(blocked)}"
        )
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, ""))


def _safe_url(url: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", "", ""))


def _headers_summary(headers: httpx.Headers) -> dict[str, str]:
    return {
        key.lower(): _safe_url(value) if key.lower() == "location" else value
        for key, value in headers.items()
        if key.lower() in SAFE_RESPONSE_HEADERS
    }


def _content_type(headers: httpx.Headers) -> str | None:
    value = headers.get("content-type")
    return value.split(";", 1)[0].strip().lower() if value else None


def _validate_peer(response: httpx.Response) -> None:
    stream = response.extensions.get("network_stream")
    if stream is None or not hasattr(stream, "get_extra_info"):
        return
    peer = stream.get_extra_info("server_addr")
    if peer and not _public_address(peer[0]):
        raise FetchPolicyError(f"connection reached non-public peer address {peer[0]}")


def _read_bounded(response: httpx.Response, max_size_bytes: int) -> bytes:
    content_length = response.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > max_size_bytes:
                raise ValueError(f"response Content-Length exceeds {max_size_bytes} bytes")
        except ValueError as exc:
            if "exceeds" in str(exc):
                raise
    chunks: list[bytes] = []
    size = 0
    for chunk in response.iter_bytes():
        size += len(chunk)
        if size > max_size_bytes:
            raise ValueError(f"response body exceeds {max_size_bytes} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


def web_fetch(
    url: str,
    allowed_domains: list[str],
    headers: dict[str, str] | None = None,
    timeout_seconds: float = 30,
    max_size_bytes: int = 5_000_000,
    max_retries: int = 2,
    max_redirects: int = 5,
    retry_backoff_seconds: float = 0.25,
    *,
    context: ExecutionContext | None = None,
    lineage: LineageState | None = None,
    store: FilesystemArtifactStore | None = None,
    client: httpx.Client | None = None,
    resolver: Callable[[str, int], list[str]] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
) -> WebFetchResult:
    if timeout_seconds <= 0 or timeout_seconds > 300:
        raise ValueError("timeout_seconds must be between 0 and 300")
    if max_size_bytes <= 0 or max_size_bytes > 100_000_000:
        raise ValueError("max_size_bytes must be between 1 and 100000000")
    if max_retries < 0 or max_retries > 10:
        raise ValueError("max_retries must be between 0 and 10")
    if max_redirects < 0 or max_redirects > 20:
        raise ValueError("max_redirects must be between 0 and 20")
    request_headers = headers or {}
    forbidden = sorted(set(key.lower() for key in request_headers) & FORBIDDEN_REQUEST_HEADERS)
    if forbidden:
        raise FetchPolicyError(f"request header(s) cannot be overridden: {forbidden}")
    resolve = resolver or _default_resolver
    validated_url = _validate_target(url, allowed_domains, resolve)
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
    artifact_store = store or FilesystemArtifactStore(max_size_bytes=max_size_bytes)
    http = client or httpx.Client(
        timeout=timeout_seconds, follow_redirects=False, trust_env=False
    )
    owns_client = client is None
    attempts: list[FetchAttempt] = []
    redirects: list[str] = []
    try:
        for attempt_number in range(1, max_retries + 2):
            current_url = validated_url
            redirect_count = 0
            try:
                for _ in range(max_redirects + 1):
                    with http.stream(
                        "GET", current_url, headers=request_headers,
                        timeout=timeout_seconds, follow_redirects=False,
                    ) as response:
                        _validate_peer(response)
                        if response.has_redirect_location:
                            location = response.headers.get("location")
                            if not location:
                                raise FetchPolicyError("redirect response is missing Location")
                            if redirect_count >= max_redirects:
                                raise FetchPolicyError("redirect limit exceeded")
                            current_url = _validate_target(
                                urljoin(current_url, location), allowed_domains, resolve
                            )
                            redirect_count += 1
                            redirects.append(_safe_url(current_url))
                            continue
                        summary = _headers_summary(response.headers)
                        try:
                            raw = _read_bounded(response, max_size_bytes)
                        except ValueError as exc:
                            attempts.append(FetchAttempt(
                                attempt=attempt_number,
                                status="size_limit_exceeded",
                                status_code=response.status_code,
                                error=str(exc),
                            ))
                            return WebFetchResult(
                                status="size_limit_exceeded",
                                requested_url=_safe_url(url),
                                final_url=_safe_url(current_url),
                                status_code=response.status_code,
                                content_type=_content_type(response.headers),
                                headers_summary=summary,
                                attempts=attempts,
                                redirects=redirects,
                                error=str(exc),
                                lineage=lineage,
                            )
                        retryable = response.status_code in RETRYABLE_STATUS_CODES
                        if retryable and attempt_number <= max_retries:
                            attempts.append(FetchAttempt(
                                attempt=attempt_number,
                                status="retryable_http_error",
                                status_code=response.status_code,
                            ))
                            break
                        status = "success" if response.is_success else "http_error"
                        attempts.append(FetchAttempt(
                            attempt=attempt_number,
                            status=status,
                            status_code=response.status_code,
                        ))
                        artifact = write_artifact(
                            lineage,
                            artifact_store,
                            context,
                            raw,
                            stage=ArtifactStage.raw,
                            media_type=response.headers.get("content-type") or "application/octet-stream",
                            metadata={
                                "kind": "raw_http_response",
                                "url": _safe_url(current_url),
                                "status_code": response.status_code,
                                "headers_summary": summary,
                            },
                        )
                        return WebFetchResult(
                            status=status,
                            requested_url=_safe_url(url),
                            final_url=_safe_url(current_url),
                            status_code=response.status_code,
                            content_type=_content_type(response.headers),
                            headers_summary=summary,
                            raw_artifact=artifact,
                            attempts=attempts,
                            redirects=redirects,
                            retryable=retryable,
                            lineage=lineage,
                        )
            except httpx.TimeoutException as exc:
                attempts.append(FetchAttempt(
                    attempt=attempt_number, status="retryable_timeout", error=str(exc)
                ))
            except httpx.TransportError as exc:
                attempts.append(FetchAttempt(
                    attempt=attempt_number, status="retryable_transport_error", error=str(exc)
                ))
            if attempt_number <= max_retries:
                sleeper(retry_backoff_seconds * attempt_number)
        error = attempts[-1].error or attempts[-1].status
        return WebFetchResult(
            status="transport_error",
            requested_url=_safe_url(url),
            attempts=attempts,
            redirects=redirects,
            retryable=True,
            error=error,
            lineage=lineage,
        )
    finally:
        if owns_client:
            http.close()


def main(
    url: str,
    allowed_domains: list[str],
    headers: dict[str, str] = {},
    timeout_seconds: float = 30,
    max_size_bytes: int = 5_000_000,
    max_retries: int = 2,
    max_redirects: int = 5,
) -> dict:
    return web_fetch(
        url=url,
        allowed_domains=allowed_domains,
        headers=headers,
        timeout_seconds=timeout_seconds,
        max_size_bytes=max_size_bytes,
        max_retries=max_retries,
        max_redirects=max_redirects,
    ).model_dump(mode="json")
