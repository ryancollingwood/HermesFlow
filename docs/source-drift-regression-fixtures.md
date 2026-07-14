# Source-drift regression fixtures

HF-031 turns a retained failed or changed HTML/JSON source artifact into an
immutable, sanitised fixture for the affected capability. Runtime fixture bytes
remain in the HF-017 content-addressed store under `/shared/artifacts`; they are
never added to Windmill mirror scope or committed automatically.

## Promotion record

`f/hermes_flow/repair/promote_fixture` integrity-checks the supplied
`ArtifactRef`, sanitises its UTF-8 content, and writes a derived artifact. The
returned `SourceDriftFixture` records:

- an ID containing the sanitised SHA-256 content hash;
- the original source artifact ID/hash and derived-artifact lineage;
- the failed Windmill job and affected active capability;
- the format, exact sanitisation summary, and rules digest;
- bounded candidate argument binding; and
- a human description plus machine-checkable required paths, exact values, and
  minimum item counts.

The checked-in contract is
[`docs/schemas/source_drift_fixture.schema.json`](schemas/source_drift_fixture.schema.json).
Candidate execution evidence is separately versioned in
[`docs/schemas/source_drift_fixture_run.schema.json`](schemas/source_drift_fixture_run.schema.json).

## Sanitisation

JSON is parsed and re-serialized canonically. Credential-shaped field names are
redacted recursively; volatile identifiers and timestamps are removed. Operators
may add exact field names to either set. HTML is parsed and reconstructed while
preserving selectors and document structure; sensitive attributes and embedded
credential text are redacted, volatile attributes and comments are removed, and
JSON script blocks receive the same recursive JSON treatment.

Expected-behaviour metadata and static candidate arguments reject sensitive keys
or credential-shaped values. The original artifact is never modified.

## Candidate regression selection

HF-016's `select_regression_tests` accepts promoted fixture records alongside
the static manifests. Matching fixtures are overlaid as promotion-gating tests
for the changed capability, with a selection reason naming the failed job. A
candidate path is mandatory and must remain under
`f/hermes_flow/candidates/`; active paths are rejected.

`f/hermes_flow/testing/source_drift_fixture` loads the sanitised artifact,
injects text, parsed JSON, or its ArtifactRef into the configured candidate
argument, runs the candidate as a nested bounded Windmill job, and evaluates the
recorded assertions. Both the old baseline and the newly drifted fixture can be
selected together, preventing a repair from fixing the new source by breaking
the old one.
