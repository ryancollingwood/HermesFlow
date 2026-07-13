"""HF-021 bounded web-fetch and SSRF policy tests."""
import json

import httpx
import pytest

from f.capabilities.collection.web_fetch import FetchPolicyError, web_fetch
from f.libraries.storage.artifacts import FilesystemArtifactStore


PUBLIC_IP = "93.184.216.34"


def public_resolver(host, port):
    return [PUBLIC_IP]


def fetch(tmp_path, handler, **kwargs):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    store = FilesystemArtifactStore(tmp_path)
    result = web_fetch(
        "https://example.com/resource",
        ["example.com"],
        client=client,
        store=store,
        resolver=public_resolver,
        retry_backoff_seconds=0,
        sleeper=lambda _: None,
        **kwargs,
    )
    client.close()
    return result, store


@pytest.mark.parametrize(
    "content,content_type",
    [
        (b"<html><body>ok</body></html>", "text/html; charset=utf-8"),
        (json.dumps({"ok": True}).encode(), "application/json"),
    ],
)
def test_html_and_json_are_retained_as_raw_artifacts(tmp_path, content, content_type):
    result, store = fetch(
        tmp_path,
        lambda request: httpx.Response(200, content=content, headers={
            "Content-Type": content_type,
            "ETag": "abc",
            "Set-Cookie": "secret=1",
        }),
    )
    assert result.status == "success"
    assert result.status_code == 200
    assert result.content_type == content_type.split(";", 1)[0]
    assert store.read(result.raw_artifact) == content
    assert result.headers_summary["etag"] == "abc"
    assert "set-cookie" not in result.headers_summary


def test_redirect_target_is_revalidated_and_reported(tmp_path):
    def handler(request):
        if request.url.path == "/resource":
            return httpx.Response(302, headers={"Location": "/final"})
        return httpx.Response(200, text="done", headers={"Content-Type": "text/html"})

    result, _ = fetch(tmp_path, handler)
    assert result.final_url == "https://example.com/final"
    assert result.redirects == ["https://example.com/final"]


def test_redirect_to_local_network_is_blocked_before_second_request(tmp_path):
    requests = []

    def resolver(host, port):
        return ["127.0.0.1"] if host == "internal.example.com" else [PUBLIC_IP]

    def handler(request):
        requests.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://internal.example.com/admin"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(FetchPolicyError, match="non-public"):
        web_fetch(
            "https://example.com/resource", ["example.com"], client=client,
            store=FilesystemArtifactStore(tmp_path), resolver=resolver,
        )
    client.close()
    assert len(requests) == 1


def test_timeout_is_retried_then_success_is_returned(tmp_path):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(200, text="recovered", headers={"Content-Type": "text/html"})

    result, _ = fetch(tmp_path, handler, max_retries=1)
    assert result.status == "success"
    assert [attempt.status for attempt in result.attempts] == [
        "retryable_timeout", "success",
    ]


def test_exhausted_timeout_is_a_retryable_transport_failure(tmp_path):
    def handler(request):
        raise httpx.ReadTimeout("still down", request=request)

    result, _ = fetch(tmp_path, handler, max_retries=1)
    assert result.status == "transport_error"
    assert result.retryable is True
    assert result.raw_artifact is None
    assert len(result.attempts) == 2


def test_declared_oversized_response_is_rejected_without_artifact(tmp_path):
    result, store = fetch(
        tmp_path,
        lambda request: httpx.Response(200, content=b"12345"),
        max_size_bytes=4,
    )
    assert result.status == "size_limit_exceeded"
    assert result.raw_artifact is None
    assert not [path for path in tmp_path.rglob("*") if path.is_file()]


def test_streamed_oversized_response_is_stopped_without_artifact(tmp_path):
    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            yield b"123"
            yield b"45"

    result, _ = fetch(
        tmp_path,
        lambda request: httpx.Response(
            200, stream=Stream(), headers={"Content-Type": "application/json"}
        ),
        max_size_bytes=4,
    )
    assert result.status == "size_limit_exceeded"
    assert "body exceeds" in result.error


def test_404_is_non_retryable_and_body_is_retained(tmp_path):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(404, text="missing", headers={"Content-Type": "text/html"})

    result, store = fetch(tmp_path, handler, max_retries=3)
    assert calls == 1
    assert result.status == "http_error"
    assert result.retryable is False
    assert store.read(result.raw_artifact) == b"missing"


def test_500_is_retried_and_exhaustion_remains_distinctly_retryable(tmp_path):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(503, text="unavailable")

    result, _ = fetch(tmp_path, handler, max_retries=2)
    assert calls == 3
    assert result.status == "http_error"
    assert result.retryable is True
    assert [attempt.status for attempt in result.attempts] == [
        "retryable_http_error", "retryable_http_error", "http_error",
    ]


@pytest.mark.parametrize("address", ["127.0.0.1", "::1", "169.254.169.254", "10.0.0.2"])
def test_local_and_link_local_network_addresses_are_blocked(tmp_path, address):
    with pytest.raises(FetchPolicyError, match="non-public"):
        web_fetch(
            "http://internal.example.com/", ["example.com"],
            resolver=lambda host, port: [address],
            store=FilesystemArtifactStore(tmp_path),
        )


def test_disallowed_domain_is_rejected_without_dns_or_http(tmp_path):
    resolved = False

    def resolver(host, port):
        nonlocal resolved
        resolved = True
        return [PUBLIC_IP]

    with pytest.raises(FetchPolicyError, match="allowed_domains"):
        web_fetch(
            "https://evil-example.com/", ["example.com"], resolver=resolver,
            store=FilesystemArtifactStore(tmp_path),
        )
    assert resolved is False


@pytest.mark.parametrize(
    "url,headers,error",
    [
        ("file:///etc/passwd", {}, "only http and https"),
        ("https://user:pass@example.com/", {}, "credentials embedded"),
        ("https://example.com/", {"Host": "internal"}, "cannot be overridden"),
    ],
)
def test_unsafe_request_shapes_are_rejected(tmp_path, url, headers, error):
    with pytest.raises(FetchPolicyError, match=error):
        web_fetch(
            url, ["example.com"], headers=headers, resolver=public_resolver,
            store=FilesystemArtifactStore(tmp_path),
        )


def test_query_values_are_not_retained_in_result_or_artifact_metadata(tmp_path):
    client = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, text="ok")
    ))
    store = FilesystemArtifactStore(tmp_path)
    result = web_fetch(
        "https://example.com/data?api_key=secret", ["example.com"],
        client=client, store=store, resolver=public_resolver,
    )
    client.close()
    assert "secret" not in result.requested_url
    metadata = store.read_metadata(result.raw_artifact.artifact_id)["metadata"]
    assert "secret" not in json.dumps(metadata)
