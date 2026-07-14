# Capability health dashboard and report

HF-033 exposes capability health in two existing surfaces:

- `f/hermes_flow/testing/health_report` returns a versioned Windmill report;
- the optional Grafana stack auto-provisions **HermesFlow Capability Health**.

There is no custom application. The report combines the version-controlled
`CapabilityMetadata` catalogue with HF-020's current scheduled-test
`HealthState` variables. Catalogue metadata remains authoritative for maturity,
owners, dependencies, active version, and whether scheduled health is enabled.
HF-020 state remains authoritative for the tested version, latest status/time,
consecutive failures, a bounded latest-10 status window, and latest test and
Windmill job IDs.

## Health and freshness rules

Each current catalogue entry appears, including entries with no test data:

| State | Dashboard status |
|---|---|
| current active version passed within the freshness threshold | `healthy` |
| current version passed but evidence is stale, or its last run was skipped | `warning` |
| current version has a failed run or non-zero consecutive failures | `failed` |
| no evidence, or the recorded evidence belongs to an older active version | `untested` |

The default freshness threshold is 24 hours and is explicit in every report.
The dashboard also shows the age of the projection itself, so a stopped report
schedule is visible rather than silently presenting old data as current.

`scheduled_dependents` is derived from reverse catalogue dependencies: it lists
enabled scheduled-health capabilities that depend on each row. Asset, latest
job, and health-schedule URLs point directly at Windmill's authoritative API
resources.

## Grafana projection

Approved HF-020 schedule reconciliation now includes
`f/hermes_flow/health_dashboard_report`, which runs every five minutes. Each
ordinary HF-020 scheduled test also attempts a refresh after saving its state.
The report writes Prometheus exposition format to:

```text
/shared/metrics/hermesflow_capabilities.prom
```

The write uses a temporary sibling plus atomic `os.replace`; Node Exporter sees
either the old complete snapshot or the new complete snapshot. The file is
replace-in-place current state—not append-only history, logs, prompts, inputs,
or test outputs. Prometheus provides the existing time-series retention, so
HF-033 introduces no duplicate long-term store.

The optional observability override mounts `${SHARED_DIR}` read-only into Node
Exporter and enables its textfile collector. Grafana reads the resulting
`hermesflow_capability_*` metrics from the already-provisioned Prometheus
datasource. The dashboard includes capability totals, failed/untested/stale
counts, projection age, a filterable maturity/version/status table, recent
failure counts, scheduled-dependent counts, and Windmill links.

Run the report directly in Windmill whenever an immediate refresh is useful.
Missing state is a normal `untested` row; a projection failure never replaces
or corrupts HF-020's authoritative state.

The report contract is checked in at
`docs/schemas/capability_health_report.schema.json`.
