"""
Capability catalogue model and loader — path: f/hermes_flow/catalogue/models

Other scripts import these directly:

    from f.hermes_flow.catalogue.models import Catalogue, CatalogueEntry, load_catalogue

(same import pattern as `f.hermes.client` / `f.libraries.capability.models`.)
`windmill/capability-index.yaml` is the version-controlled capability
index this module validates and loads — a repo-only file (outside
`wmill.yaml`'s sync scope, like `wmill.yaml` itself; it's HermesFlow
control-plane config, not a Windmill asset) read directly from the
checked-out repo by CI and by whatever eventually calls
`load_catalogue()` (HF-009's search operation).

Each `CatalogueEntry` wraps a full `CapabilityMetadata`
(`f.libraries.capability.models` — path, maturity, owners, effects,
autonomy, limits, test_requirements, dependencies; see that module for
why none of that is duplicated here) plus the discovery-only fields the
catalogue itself is responsible for: `kind` (script vs. flow), `tags`,
and short `inputs_summary`/`outputs_summary` descriptions. Those
summaries are deliberately prose, not the full argument JSON Schema —
Windmill's own `*.script.yaml` stays authoritative for that, exactly as
`CapabilityMetadata.path` already just points at the script rather than
re-describing it.

Schema versioning follows the same additive-only-within-a-MAJOR rule as
`f.libraries.lineage.models` — see that module's docstring.

Running THIS script directly loads `capability-index.yaml`'s content
(passed as the `catalogue_yaml` argument — Windmill jobs don't have
filesystem access to the git repo, so the caller supplies the text) and
returns a validated summary, which doubles as an integration test that
this module's validation logic runs correctly inside Windmill's actual
Python environment, not just under local pytest.
"""
from collections import Counter
from enum import Enum

import yaml
from f.libraries.capability.models import CapabilityMetadata
from pydantic import BaseModel, Field, ValidationError, field_validator

SCHEMA_VERSION = "1.0"


class CapabilityKind(str, Enum):
    script = "script"
    flow = "flow"


class CatalogueEntry(BaseModel):
    """One discoverable capability: a Windmill script/flow plus its metadata."""

    kind: CapabilityKind
    tags: list[str] = Field(
        default_factory=list,
        description="Discovery keywords, e.g. ['web', 'fetch', 'read-only'].",
    )
    inputs_summary: str = Field(
        ...,
        min_length=1,
        description="Short, agent-facing description of what this capability takes as "
        "input. Not the full argument schema — read that from Windmill via "
        "getScriptByPath/getFlowByPath using metadata.path.",
    )
    outputs_summary: str = Field(
        ...,
        min_length=1,
        description="Short, agent-facing description of what this capability returns.",
    )
    input_kinds: list[str] = Field(
        default_factory=list,
        description="Controlled-vocabulary-by-convention short labels for what this "
        "capability consumes, e.g. ['resource:hermes_endpoint']. For HF-009's "
        "schema-compatibility search — exact-match only, not a real type system. "
        "Optional: an entry with none just never matches a kind-based search.",
    )
    output_kinds: list[str] = Field(
        default_factory=list,
        description="Same convention as input_kinds, for what this capability produces, "
        "e.g. ['model_list']. Lets one capability's output_kinds be matched against "
        "another's input_kinds for primitive-chaining searches.",
    )
    metadata: CapabilityMetadata


class CatalogueValidationError(ValueError):
    """Raised with a message that names the offending entry and field explicitly."""


class Catalogue(BaseModel):
    """The full capability index."""

    schema_version: str = Field(
        default=SCHEMA_VERSION,
        description="Schema MAJOR.MINOR this record was written against.",
    )
    entries: list[CatalogueEntry] = Field(default_factory=list)

    @field_validator("entries")
    @classmethod
    def _no_duplicate_paths(cls, v: list[CatalogueEntry]) -> list[CatalogueEntry]:
        counts = Counter(entry.metadata.path for entry in v)
        dupes = sorted(path for path, n in counts.items() if n > 1)
        if dupes:
            raise ValueError(f"duplicate capability path(s) in catalogue: {dupes}")
        return v

    def list_scripts(self) -> list[CatalogueEntry]:
        return [e for e in self.entries if e.kind is CapabilityKind.script]

    def list_flows(self) -> list[CatalogueEntry]:
        return [e for e in self.entries if e.kind is CapabilityKind.flow]

    def get(self, path: str) -> CatalogueEntry | None:
        return next((e for e in self.entries if e.metadata.path == path), None)


def load_catalogue(catalogue_yaml: str) -> Catalogue:
    """Parse and validate catalogue YAML text, raising CatalogueValidationError with
    the offending entry (by path, or index if the path itself is what's missing/bad)
    and field named explicitly on any failure."""
    try:
        raw = yaml.safe_load(catalogue_yaml) or {}
    except yaml.YAMLError as e:
        raise CatalogueValidationError(f"catalogue is not valid YAML: {e}") from e

    if not isinstance(raw, dict):
        raise CatalogueValidationError(
            f"catalogue must be a YAML mapping with 'entries', got {type(raw).__name__}"
        )

    raw_entries = raw.get("entries") or []
    if not isinstance(raw_entries, list):
        raise CatalogueValidationError("catalogue 'entries' must be a list")

    validated_entries: list[CatalogueEntry] = []
    for i, raw_entry in enumerate(raw_entries):
        asset = None
        if isinstance(raw_entry, dict):
            asset = (raw_entry.get("metadata") or {}).get("path")
        asset_label = asset or f"entries[{i}] (no path — malformed before metadata.path could be read)"
        try:
            validated_entries.append(CatalogueEntry(**raw_entry))
        except ValidationError as e:
            details = "; ".join(
                f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors()
            )
            raise CatalogueValidationError(
                f"catalogue entry '{asset_label}' failed validation — {details}"
            ) from e

    try:
        return Catalogue(
            schema_version=raw.get("schema_version", SCHEMA_VERSION),
            entries=validated_entries,
        )
    except ValidationError as e:
        details = "; ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors()
        )
        raise CatalogueValidationError(f"catalogue failed validation — {details}") from e


def main(catalogue_yaml: str) -> dict:
    """Self-test / integration check: load the given catalogue text and summarize it."""
    catalogue = load_catalogue(catalogue_yaml)
    return {
        "schema_version": catalogue.schema_version,
        "entry_count": len(catalogue.entries),
        "script_paths": [e.metadata.path for e in catalogue.list_scripts()],
        "flow_paths": [e.metadata.path for e in catalogue.list_flows()],
    }
