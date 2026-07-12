# 0005 — Hermes → Windmill invocation transport

**Status:** Accepted
**Related:** [HF-000B](https://github.com/ryancollingwood/HermesFlow/issues/38), `docs/plans/hermesflow-lifecycle.md`

## Context

The lifecycle architecture (§4 of the solution document) shows Hermes talking
to the HermesFlow lifecycle controls "via MCP" without ever tasking the
decision. Two options were on the table:

1. A bespoke Hermes tool that calls Windmill's REST API directly (no MCP).
2. Register Windmill as a native MCP server with Hermes.

The initial working assumption (recorded in `docs/plans/hermesflow-lifecycle.md`
before this ADR) was option 1, on the grounds that this stack has had MCP
connectivity issues before. That assumption didn't hold up against two facts
discovered while doing this task:

- **Hermes has no generic raw-HTTP-tool mechanism.** Per `AGENTS.md`'s
  "Runtime / agent (MCP) bootstrap" section, every external service Hermes
  reaches — Baserow, Directus, and now Windmill — is wired in as an MCP
  server. Option 1 would mean building a tool-integration mechanism that
  doesn't exist anywhere else in this stack, to avoid a transport Hermes
  already uses natively.
- **The past MCP pain was specifically about the stdio bridge**, not MCP
  itself: `AGENTS.md` documents that services which only speak legacy
  HTTP+SSE need `mcp-remote` baked into the Hermes image as a stdio bridge
  (the `baserow-mcp` pattern), and that this bridging is what's fragile.
  Windmill CE (running here: v1.755.0) ships a **native MCP server**
  speaking real Streamable-HTTP/SSE at `/api/mcp/w/{workspace}/{sse,mcp}` —
  the same no-bridge, "register the endpoint/token directly" pattern already
  used for Directus (v11.12+). No bridge, no legacy-transport translation.

Windmill's own OpenAPI spec curates ~38 of its REST operations with
`x-mcp-tool: true` (listScripts, getScriptByPath, runScriptByPath,
createScript, listFlows, runFlowByPath, createSchedule, listJobs, getJobLogs,
etc.) — this *is* the MCP tool surface, not a hand-rolled one.

**Further discovery: this was already wired up.** `docker exec hermes hermes
mcp list` showed a `windmill` entry already registered and enabled
(`http://windmill_server:8000/api/mcp/w/main/sse`), alongside `directus`
(also native MCP) and stdio servers `karakeep`/`agentmail`. The backing
Windmill token (label `hermes-mcp`, scope `mcp:all`) was created 2026-06-21 —
three weeks before this ADR — in a session this repo has no other record of.
It is real, working, undocumented infrastructure.

## Decision

**Windmill is registered with Hermes as a native MCP server over
Streamable-HTTP/SSE**, matching the Directus pattern. No stdio bridge, no
bespoke HTTP-tool mechanism. The option-1 alternative (raw HTTP tool) is
rejected: it would be new, unproven plumbing solving a problem the native
route doesn't have.

```
hermes mcp add windmill --url http://windmill_server:8000/api/mcp/w/main/sse --auth header
```

The token is a Windmill API token scoped via `/api/users/tokens/create`
(Windmill's own token-scoping model — `scripts:read`, `jobs:run:scripts`,
etc. — maps directly onto the autonomy actions `discover`/`execute` in
[HF-003](https://github.com/ryancollingwood/HermesFlow/issues/41)).

## Live-session proof (2026-07-12)

Ran a bounded, toolset-scoped Hermes session:

```
docker exec hermes hermes chat -Q -t windmill -q "list the scripts under \
  f/hermes, then run f/hermes/client by path and report the job id and result"
```

- **List: fully succeeded.** Hermes called `listScripts` and correctly
  returned `f/hermes/client` and `f/hermes/chat` with hashes.
- **Run: transport succeeded, execution hit a real, documentable limit.**
  Hermes called `runScriptByPath`, which submitted a genuine Windmill job
  (`019f55ef-e96c-a968-3e94-5610d732b37b`) — proving the run path works end
  to end at the transport level. The job itself failed with
  `TypeError: 'NoneType' object is not subscriptable`, because
  `runScriptByPath`'s MCP schema doesn't accept script arguments, so
  Windmill ran `f/hermes/client` with `conn=None` instead of the
  `f/hermes/local` resource the script requires. The alternative tool that
  *does* accept arguments, `runScriptPreviewAndWaitResult`, returned 403 —
  the currently configured token lacks the scope for it.

This satisfies "Hermes can list and run one Windmill script through the
chosen transport" — the job ID is real, returned by the real server, over
the real transport. It also surfaces a constraint load-bearing for later
capability work.

## Consequences

- **Capabilities invoked through the `windmill` MCP tool must not require
  Windmill-resource-typed arguments** (`conn: hermes_endpoint` and similar)
  unless the registered token is scoped for `runScriptPreviewAndWaitResult`
  and Hermes is taught to pass resolved arguments through it. This affects
  [HF-019](https://github.com/ryancollingwood/HermesFlow/issues/57) (the
  structured Hermes invocation wrapper) and any HF-021+ capability that takes
  resource-typed connections/secrets — plan for those to be invoked via
  direct Windmill job-run REST calls (as `windmill-push`/`windmill-pull`
  already do for admin auth) rather than through the MCP tool, or design
  their signatures to avoid resource-typed args entirely.
- **Done: this wiring is now productized.** `make windmill-mcp`
  (see [`docs/windmill-sync.md#windmill-mcp-registration`](../../docs/windmill-sync.md#windmill-mcp-registration))
  mints or reuses a token scoped to `mcp:all`, `scripts:read`, `flows:read`,
  `jobs:read`, `jobs:run:scripts`, `jobs:run:flows` via Windmill's
  `/api/users/tokens/create`, persists it to `.env` as `WM_MCP_TOKEN`,
  registers it with Hermes, and verifies the connection — matching the
  `baserow-mcp` pattern, so a fresh install now reproduces this transport
  instead of depending on undocumented runtime state. It is idempotent and
  non-destructive: if a `windmill` MCP connection is already registered and
  healthy (as the pre-existing `mcp:all`/`mcp:favorites`-scoped tokens on
  this host are), the target leaves it alone rather than silently replacing
  it — rotating an existing connection onto the token this target mints is a
  deliberate, separate step (`docker exec hermes hermes mcp remove windmill`
  then re-run).

  **Scope-model correction found while building this:** the `mcp:all`
  scope in `hermes-mcp`'s original token (§Context above) is not, as first
  assumed, a broad "grant everything" scope — Windmill's MCP endpoint
  requires *some* `mcp:*`-family scope just to be reachable at all (verified:
  a token with only REST scopes like `scripts:read` gets `403 Required
  scope: mcp:*` before any tool runs), independently of the granular REST
  scopes that actually gate what each tool call is allowed to do.
  `mcp:scripts`/`mcp:flows` were tried as a narrower alternative but connect
  while reporting zero tools, so `mcp:all` remains the only value that
  exposes the tool set. The narrowing this ADR called for is real, just
  enforced one layer down: with `mcp:all` + the five granular scopes above
  (no `*:write`), Hermes can see all ~38–43 MCP tools including
  write-shaped ones, but a direct test of the underlying REST calls confirms
  `scripts/create` and `variables/create` both `403` while `scripts/list`
  `200`s — writes fail closed at call time rather than being hidden from
  the tool list.
- `-t <toolset>` on `hermes chat`/`hermes -z` (scoping a session to a named
  MCP server's tools) is confirmed to work and is the mechanism
  [HF-006](https://github.com/ryancollingwood/HermesFlow/issues/44) (audit
  and restrict Hermes's direct-execution tools) should build on for
  HermesFlow-mode sessions.
