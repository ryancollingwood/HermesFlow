# Exemplar walkthrough: conversation to Windmill inspection

This is the shortest path to seeing the whole HermesFlow lifecycle work end
to end: one Hermes conversation, routed through capability search and
execution, inspected afterwards directly in Windmill. Every command below is
the same one already exercised by
`windmill/tests/test_hermesflow_conversation_skill.py::test_live_hermes_to_windmill_and_back`
— nothing here is a hypothetical.

## Prerequisites

1. The stack is up: `docker compose ps` shows `hermes`, `windmill_server`,
   `windmill_worker`, and `db` running. If you haven't installed yet, see
   [README → Quick start](../README.md#quick-start) or
   [INSTALL.md](../INSTALL.md).
2. **[INSTALL.md step 18](../INSTALL.md#18-hermesflow-lifecycle-assets-recommended-after-first-boot)
   has been run** — this walkthrough needs the HermesFlow capability
   catalogue pushed to Windmill and the `hermesflow` skill deployed to
   Hermes, neither of which the installer does for you:
   ```sh
   make windmill-push hermes-skills-push hermesflow-mcp collection-db-migrate
   ```
3. Hermes has a working model configured (the installer does this; confirm
   with `docker exec hermes hermes config get model.default`).

## 1. Start a scoped conversation

HermesFlow sessions run with a restricted toolset — this is not optional
window-dressing, it's what makes "Windmill is the exclusive execution
boundary" structural rather than a prompt-level suggestion the model can
ignore (see [README → Execution principle](../README.md#execution-principle)).
Ask for a one-off product comparison from an explicit, single-hostname
source:

```sh
docker exec hermes hermes chat -Q \
  -t windmill,hermesflow,clarify -s hermesflow \
  -q "Compare product information from https://example.com/ in a one-off read-only run, with example.com as the exact and only allowed source hostname. Search Windmill first, inspect and execute the existing product collection flow automatically if policy permits, wait for completion, then report the exact workflow path and version, Windmill job ID, and artifact references."
```

(Drop `-Q` and `-q "..."` for an interactive session instead — the same
prompt typed at the `>` prompt behaves identically.)

## 2. What happens, and what you'll see

Per the `hermesflow` skill's own rules
([SKILL.md](../hermes/skills/workflow-orchestration/hermesflow/SKILL.md)):

1. **Classify the intent.** A one-off read from an explicit URL is a
   supported read, not a write — no clarification needed.
2. **Search before naming anything.** Hermes calls Windmill's `listFlows`
   (and `listScripts` if a primitive might cover it alone) rather than
   assuming `f/workflows/product_collection` from memory, then inspects the
   selected flow's schema with `getFlowByPath`.
3. **Evaluate policy.** The active product-collection flow's `execute`
   action may run automatically for a non-destructive, one-off request
   within its declared bounds (see
   [architecture/adr/0002](../architecture/adr/0002-capability-lifecycle.md)
   for why `execute` can be automatic while `promote`/`schedule` never can).
4. **Execute through the narrow MCP tool**, not native `runFlowByPath` —
   `run_product_collection` validates the target, resource, and bounds
   first (this is exactly what `make hermesflow-mcp` registered in the
   prerequisites). Hermes then waits for the job and retrieves it with
   Windmill's `getJob`.
5. **Report honestly.** One sentence naming the workflow, plus the workflow
   path, capability version, Windmill job ID, and artifact references —
   never a paraphrase that drops the job reference or an artifact that
   wasn't actually returned.

A successful run's output contains, verifiably:

- `f/workflows/product_collection` (the workflow path)
- `1.0.0` (the capability version)
- a UUID-shaped Windmill job ID
- the word "artifact" (referencing the raw/normalized/report artifacts written)

If any of these is missing, something didn't complete the way it should —
see [Troubleshooting](#troubleshooting) below.

## 3. Inspect the result in Windmill

Take the job ID reported in step 2 and inspect it directly — this is the
"ends with Windmill inspection" half of the exemplar, and it's how you
confirm Hermes's summary matches what Windmill actually recorded, not just
what it claims.

**Via the UI:** open `http://windmill.localhost`, workspace `main`, sign in
(default superadmin `admin@windmill.dev` / `changeme` unless you changed it),
and go to **Runs** → search for the job ID. You'll see the flow's step graph,
each step's inputs/outputs, and the final result.

**Via the API** (same pattern used throughout this repo — see
[README → Push it](../README.md#push-it) for the token-minting form):

```sh
TOKEN=$(curl -s http://windmill.localhost/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"admin@windmill.dev","password":"changeme"}')
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://windmill.localhost/api/w/main/jobs_u/get/<job-id-from-step-2>" | python3 -m json.tool
```

The response's `result` field carries the same normalized/report/dataset
artifact references Hermes reported in step 2 — nothing Hermes told you
should be unverifiable here.

## 4. See it in the health dashboard (optional)

If you enabled the observability override
(`./install.sh --with-observability` or `make observability`), the
capability you just ran is already one row in the **HermesFlow Capability
Health** Grafana dashboard — see
[`docs/capability-health-dashboard.md`](capability-health-dashboard.md) for
what each column means. Run the report directly for an immediate refresh
instead of waiting for its five-minute schedule:

```sh
# from the Windmill UI: Scripts → f/hermes_flow/testing/health_report → Run
```

## 5. What happens when something breaks

You don't need to deliberately break anything to see how HermesFlow responds
to a failure — the behavior is already exercised by the test suite, and
reading it is faster and safer than reproducing a real regression by hand:

- **A failed job gets inspected and classified** before any repair is
  attempted — `windmill/tests/test_adaptive_repair.py` and
  [`docs/failure-inspection.md`](failure-inspection.md) cover exactly which
  failure categories (source drift, code defect, dependency, policy,
  infrastructure) are repairable and which stop the process outright.
- **A repairable failure can generate a tested candidate and one bounded,
  approval-gated retry** — see
  [`docs/adaptive-repair-retry.md`](adaptive-repair-retry.md). Nothing here
  is automatic promotion; a human still approves the fix before it goes
  live.
- **A sustained, non-transient regression can be recommended for rollback**
  — see [`docs/rollback-recommendation.md`](rollback-recommendation.md) for
  the exact threshold and why a purely transient (e.g. network-outage)
  failure streak is deliberately *not* recommended for rollback.

If you want to see one of these live rather than just read about it, the
most direct route is `cd windmill && pytest tests/test_adaptive_repair.py -v`
or `pytest tests/test_rollback_recommendation.py -v` — both run the real
logic (inspection, classification, policy, candidate generation, approval
gating) against an in-memory fake Windmill client, so you can watch every
stage's assertions pass without needing to actually break a live capability.

## Troubleshooting

See [README → Troubleshooting → HermesFlow lifecycle](../README.md#hermesflow-lifecycle)
for policy denials, promotion test failures, dashboard status meanings,
adaptive-repair attempt limits, rollback-recommendation thresholds, and an
unavailable Windmill/MCP connection. If step 1's command exits non-zero
before you even get a response, confirm step 18's prerequisites actually
ran — a missing `hermesflow` skill or unpushed flow is the most common cause,
and neither `install.sh`/`install.py` deploys them for you.
