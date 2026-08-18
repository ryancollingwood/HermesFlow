"""
Candidate creation operation — path: f/hermes_flow/candidate_ops/create

Other scripts import `create_candidate` directly:

    from f.hermes_flow.candidate_ops.create import create_candidate

(same import pattern as `f.hermes_flow.catalogue.models`.) This is a
Windmill *administrative* operation: it writes new script assets under
`f/hermes_flow/candidates/` (its own code lives at a sibling path,
`f/hermes_flow/candidate_ops/` — see `f.hermes_flow.candidate_ops.models`'s
docstring for why), which ordinary Hermes sessions cannot do directly — the
`windmill-mcp` token Hermes uses (`docs/windmill-sync.md`) deliberately
excludes `scripts:write`, matching `architecture/adr/0001`'s "Hermes never
mutates active-code" boundary.

**How this script can write despite that, safely:** Windmill jobs run with
the permissions of the *script's own owner* (`WM_PERMISSIONED_AS`), not
the caller's token scope — confirmed live: a job triggered with a token
scoped to only `scripts:read`/`jobs:run:scripts` still ran with
`permissioned_as=u/admin` and successfully created another script via its
own ambient `wmill.Windmill()` client. So Hermes's narrowly-scoped token
never needs `scripts:write` itself; it only needs enough to call *this one
script* (`jobs:run:scripts`, which it already has), and this script's own
elevated identity does the write on its behalf — a narrow, auditable
gateway rather than a broad grant. The safety boundary is therefore
enforced by this module's own code, not by Windmill's token scopes:
`create_candidate()` refuses to write anywhere outside
`f.hermes_flow.candidate_ops.models.CANDIDATES_ROOT` (see the path-escape
check below), and every other admin-capable script in this repo must hold
the same discipline — this is not a general-purpose "run anything as
admin" gateway.

Idempotency: `compute_candidate_id(request_key)` is deterministic, so the
same `request_key` always resolves to the same path. Before writing
anything, `create_candidate()` checks whether a script already exists at
that path and, if so, returns the existing candidate's record unchanged
instead of creating a duplicate or erroring.

The Windmill client is dependency-injected (`client` parameter) so
`windmill/tests/test_candidate_create.py` can exercise the full creation
logic — idempotency, path-escape rejection, derived-candidate provenance —
against an in-memory fake without a live server. Running this script
directly (`main()`) uses the real ambient `wmill.Windmill()` client, which
doubles as the integration test proving this logic actually has write
access when it needs to.

`import wmill` is deliberately a top-level import, not deferred inside
`_real_client()`: `wmill generate-metadata`'s static dependency scan only
picks up top-level imports, and a deferred import here previously produced
a lock file silently missing `wmill` entirely — the script failed at
runtime with `ModuleNotFoundError: No module named 'wmill'` despite
working fine under local pytest (which never calls `_real_client()` when a
fake `client` is injected). Caught by actually deploying and running this
script live, not just by local tests passing.
"""
from typing import Protocol

import wmill
from f.hermes_flow.candidate_ops.models import (
    CANDIDATES_ROOT,
    CandidateRecord,
    compute_candidate_id,
    compute_candidate_path,
    metadata_variable_path,
)


class CandidateCreationError(ValueError):
    pass


class _Response(Protocol):
    status_code: int

    def json(self) -> dict: ...
    @property
    def text(self) -> str: ...


class WindmillAdminClient(Protocol):
    """Duck-typed subset of `wmill.Windmill()` this module actually uses.

    `wmill.Windmill.get`/`.post` raise on any non-2xx by default
    (`raise_for_status=True`) rather than returning the response — found by
    actually deploying and running this script, not by local tests, since
    the fake client used there never needed to model that behaviour until
    it was discovered live. Every call site here that treats "404" as an
    expected, handled outcome (not an error) passes
    `raise_for_status=False` explicitly.
    """

    workspace: str

    def get(self, path: str, raise_for_status: bool = True) -> _Response: ...
    def post(self, path: str, json: dict, raise_for_status: bool = True) -> _Response: ...


def _real_client() -> WindmillAdminClient:
    return wmill.Windmill()


def create_candidate(
    request_key: str,
    reason: str,
    content: str,
    language: str = "python3",
    source_path: str | None = None,
    base_version: str | None = None,
    conversation_id: str | None = None,
    request_id: str | None = None,
    generated_by_capability: str | None = None,
    failed_job_id: str | None = None,
    repair_context_sha256: str | None = None,
    generation_trace_id: str | None = None,
    generation_artifact_ids: list[str] | None = None,
    client: WindmillAdminClient | None = None,
) -> dict:
    """Create (or idempotently return) a candidate. Returns a dict with an
    "idempotent" bool plus the CandidateRecord's fields."""
    w = client or _real_client()
    ws = w.workspace

    candidate_id = compute_candidate_id(request_key)
    path = compute_candidate_path(candidate_id)
    if not path.startswith(CANDIDATES_ROOT + "/"):
        # Defence in depth: compute_candidate_path is trusted, but never write
        # anywhere this check doesn't confirm is inside the candidates root.
        raise CandidateCreationError(
            f"computed candidate path {path!r} escaped {CANDIDATES_ROOT!r} — refusing to write"
        )

    existing = w.get(f"/w/{ws}/scripts/get/p/{path}", raise_for_status=False)
    if existing.status_code == 200:
        meta_resp = w.get(
            f"/w/{ws}/variables/get/{metadata_variable_path(candidate_id)}", raise_for_status=False
        )
        if meta_resp.status_code == 200:
            record = CandidateRecord.model_validate_json(meta_resp.json()["value"])
            return {"idempotent": True, **record.model_dump(mode="json")}
        # Script exists but its metadata variable doesn't (shouldn't normally happen —
        # e.g. the metadata write failed after the script write on a prior attempt).
        # Fail loudly rather than silently fabricating metadata for someone else's script.
        raise CandidateCreationError(
            f"candidate script exists at {path} but its metadata variable is missing/unreadable — "
            "inconsistent state, needs manual investigation"
        )

    if source_path is not None and base_version is None:
        src = w.get(f"/w/{ws}/scripts/get/p/{source_path}", raise_for_status=False)
        if src.status_code != 200:
            raise CandidateCreationError(
                f"source_path {source_path!r} does not exist — cannot derive a candidate from it"
            )
        base_version = src.json()["hash"]

    record = CandidateRecord(
        candidate_id=candidate_id,
        path=path,
        request_key=request_key,
        reason=reason,
        source_path=source_path,
        base_version=base_version,
        conversation_id=conversation_id,
        request_id=request_id,
        generated_by_capability=generated_by_capability,
        failed_job_id=failed_job_id,
        repair_context_sha256=repair_context_sha256,
        generation_trace_id=generation_trace_id,
        generation_artifact_ids=generation_artifact_ids or [],
    )

    create_resp = w.post(
        f"/w/{ws}/scripts/create",
        json={
            "path": path,
            "content": content,
            "language": language,
            "summary": f"[candidate] {reason[:100]}",
        },
        raise_for_status=False,
    )
    if create_resp.status_code not in (200, 201):
        raise CandidateCreationError(
            f"failed to create candidate script at {path}: {create_resp.status_code} {create_resp.text}"
        )

    meta_resp = w.post(
        f"/w/{ws}/variables/create",
        json={
            "path": metadata_variable_path(candidate_id),
            "value": record.model_dump_json(),
            "is_secret": False,
            "description": f"CandidateRecord metadata for {path} (HF-011)",
        },
        raise_for_status=False,
    )
    if meta_resp.status_code not in (200, 201):
        raise CandidateCreationError(
            f"created candidate script at {path} but failed to persist its metadata variable: "
            f"{meta_resp.status_code} {meta_resp.text} — inconsistent state, needs manual investigation"
        )

    return {"idempotent": False, **record.model_dump(mode="json")}


def main(
    request_key: str,
    reason: str,
    content: str,
    language: str = "python3",
    source_path: str = "",
    base_version: str = "",
    conversation_id: str = "",
    request_id: str = "",
    generated_by_capability: str = "",
    failed_job_id: str = "",
    repair_context_sha256: str = "",
    generation_trace_id: str = "",
    generation_artifact_ids: list[str] | None = None,
) -> dict:
    """Windmill entrypoint. Empty-string optionals (Windmill has no None in its UI
    forms) are normalized to None before delegating to create_candidate()."""
    return create_candidate(
        request_key=request_key,
        reason=reason,
        content=content,
        language=language,
        source_path=source_path or None,
        base_version=base_version or None,
        conversation_id=conversation_id or None,
        request_id=request_id or None,
        generated_by_capability=generated_by_capability or None,
        failed_job_id=failed_job_id or None,
        repair_context_sha256=repair_context_sha256 or None,
        generation_trace_id=generation_trace_id or None,
        generation_artifact_ids=generation_artifact_ids or [],
    )
