# Repair-candidate generation

HF-030 adds `f/hermes_flow/repair/generate_candidate`, which turns one complete
HF-029 `RepairContext` into a policy-checked candidate. It never edits the active
script. A valid patch is written through HF-011 under
`f/hermes_flow/candidates/<id>` and remains outside git/Windmill mirror scope
until the normal test and approval-gated promotion lifecycle runs.

## Generation contract

The operation loads the catalogue policy and live active script before calling
Hermes. The captured Windmill hash, source digest, and exact source content must
all still match. Missing, redacted, truncated, or stale source fails before a
model request, preventing generation from an incomplete or superseded base.

The prompt requires the smallest failure-specific change, preserved signatures,
behaviour, dependencies, and effects, plus at least one concrete test update that
reproduces the failure. Logs and source text are labelled evidence, not
instructions. Hermes must return schema-valid JSON containing the complete
patched Python source, rationale, summary, and test updates.

## Retained provenance

HF-019 retains the exact task prompt, empty conversation payload, serialized
redacted `RepairContext`, raw model output, and parsed output. The result and
candidate metadata link the original failed job, source path/version/Windmill
hash, context and prompt SHA-256 digests, lineage trace, model attempts, and all
generation artifact IDs. Repair provenance is structurally all-or-nothing in
`CandidateRecord`.

The checked-in result contract is
[`docs/schemas/repair_generation_result.schema.json`](schemas/repair_generation_result.schema.json).

## Fail-closed patch checks

Before candidate creation, HF-030 rejects:

- unchanged, syntactically invalid, or over-broad patches;
- redacted or credential-shaped content;
- new third-party dependencies;
- undeclared network, database, filesystem, or external-process effects; and
- dynamic execution, unsafe deserialization, subprocess, shell, or Windmill
  administration calls introduced by the patch.

Invalid JSON and schema failures retain their prompt/context/raw-output evidence
but create no candidate. Policy rejection likewise returns the parsed proposal
and rejection reason without changing active or candidate state.

## Source-selector integration

The focused integration contract models a retail markup drift from `.old-card`
to `.product-card`. It exercises the actual HF-019 schema/lineage wrapper with a
fake Hermes transport and fake Windmill API, proves the candidate is derived
from the failed active hash, verifies exact artifact retention, and asserts the
active script remains byte-for-byte unchanged. Companion cases cover malformed
JSON, missing tests, invalid Python, subprocesses, third-party imports,
non-minimal patches, stale bases, and truncated inspection contexts.
