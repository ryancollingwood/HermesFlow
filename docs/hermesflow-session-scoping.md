# Scoping a Hermes session for HermesFlow mode

`architecture/adr/0001-windmill-exclusive-execution.md` decides that task
code only ever runs through Windmill. The
[`hermesflow` skill](../hermes/skills/workflow-orchestration/hermesflow/SKILL.md)
states that rule at the prompt level. **Neither of those things enforces
it.** This doc covers the mechanism that actually does, why it's a
session-level flag rather than a global config change, and the evidence
that it works.

## The gap this closes

A skill is prompt-level guidance — text injected into the system prompt
that the model is expected to follow. It is not a permission system. Tested
directly: with the `hermesflow` skill preloaded (`-s hermesflow`) but no
toolset restriction, asking Hermes to fetch a URL "directly, don't
overthink it" made it call the built-in `web_extract` tool anyway, despite
the skill's Rule 1 explicitly prohibiting direct execution. The rule was
stated; nothing stopped the model from ignoring it under a mildly
adversarial prompt.

## Inventory: execution-capable built-in toolsets

From `docker exec hermes hermes tools list` on this deployment:

| Toolset | What it is | Why it's execution-capable |
|---|---|---|
| `terminal` | 💻 Terminal & Processes | Arbitrary shell execution |
| `code_execution` | ⚡ Code Execution | Arbitrary Python (or other) execution |
| `browser` | 🌐 Browser Automation | Drives a real browser — navigation, clicks, form submission |
| `file` | 📁 File Operations | Reads/writes the local filesystem |
| `web` | 🔍 Web Search & Scraping | `web_extract` and similar — fetches external content directly, bypassing any Windmill-side fetch capability's policy/limits |
| `computer_use` | 🖱️ Computer Use (macOS) | Drives the host GUI directly |
| `cronjob` | ⏰ Cron Jobs | Creates **Hermes-level** recurring tasks — a separate scheduling mechanism from Windmill's, so it bypasses `AutonomyPolicy.schedule`'s `approval_required` guarantee entirely if left enabled |

`image_gen`/`tts`/`vision` call external generation APIs but don't execute
arbitrary code or mutate state the way the above do; they're not included
in the HermesFlow-mode allowlist below either way, since they're not
needed for orchestration work.

**`delegation`** (spawns sub-agent tasks) is a known gap, not resolved
here — see below.

## The mechanism: session-scoped `-t`, not global `hermes tools disable`

`hermes chat`/`hermes -z`'s `-t`/`--toolsets` flag is a **session-scoped
allowlist**: pass it, and the session has *only* the named toolsets —
confirmed by listing a scoped session's available tools (see Verification
below), not just by the model's self-report.

**Don't use `hermes tools disable <name> [--platform cli]` for this.** That
edits the *global* `cli` platform config, permanently removing the tool
from every Hermes session on this deployment — including ordinary
assistant use outside HermesFlow, where `terminal`/`browser`/`file` are
legitimate (`architecture/adr/0001`'s own Context section: Hermes
"generally" has this tool access, and the ADR's boundary is specifically
about *task execution*, not a blanket prohibition on Hermes ever touching a
shell). Scope the session, not the installation.

## HermesFlow-mode invocation

```sh
hermes chat -t windmill,hermesflow,memory,todo,clarify,session_search -s hermesflow
# non-interactive:
hermes chat -Q -t windmill,hermesflow,memory,todo,clarify,session_search -s hermesflow -q "..."
```

- `windmill` — the only execution transport (ADR 0001/0005).
- `hermesflow` — the narrow HF-028 MCP server registered by
  `make hermesflow-mcp`; its sole tool can submit the fixed product-collection
  flow with validated arguments because Windmill's native MCP run tools cannot
  pass them. Its dedicated `jobs:run`/`jobs:read` token is available only inside
  that fixed-flow server; the model cannot select arbitrary code, schedule work,
  or enable AI through its schema.
- `memory`, `session_search` — recall context across sessions; read-only,
  no task side effects.
- `todo` — task tracking; bookkeeping only.
- `clarify` — asking the user clarifying questions; core to the
  orchestration flow.
- `-s hermesflow` — preloads the skill's rules and reference docs. Still
  necessary even with `-t` scoped: the skill is what teaches the search
  order, candidate lifecycle, and result presentation; the toolset
  restriction only closes the *direct execution* gap, not the rest of the
  skill's guidance.

Everything in the inventory table above is deliberately absent.

## Known gap: sub-agent delegation

The `delegation` toolset isn't in the allowlist, and that's a deferred
decision, not an oversight: if a delegated sub-agent task gets its own,
unscoped toolset, it reopens exactly the gap this doc closes, one level
removed. Don't add `delegation` to a HermesFlow session's toolset list
until sub-agent scoping is worked out.

## Verification

All tested live against this deployment (`docker exec hermes hermes chat
...`):

1. **Tool inventory is exactly the allowlist.** Asked a scoped session
   (`-t windmill,memory,todo,clarify,session_search`) to list every tool
   function it had available: got exactly `clarify`, `memory`,
   `session_search`, `todo` (plus `hindsight_retain`/`hindsight_recall`/
   `hindsight_reflect`, bundled under `memory`), and the 38 `windmill` MCP
   tools. Nothing else — no other built-in toolset, no other MCP server
   (`karakeep`/`agentmail`/`directus` were all excluded too).
2. **Direct execution attempts are structurally refused, not just
   declined.** Asked the same scoped session, in separate turns, to (a)
   run a shell command, (b) open a browser and navigate somewhere, (c)
   write a file, (d) execute Python — all four came back reporting the
   relevant tool as unavailable ("No `terminal` tool is available in this
   session", "I don't have a browser tool... available", etc.), not a
   choice to decline.
3. **Ordinary conversation still works.** Asked the same scoped session an
   explanatory question unrelated to execution (script vs. flow in
   Windmill) — answered normally, no tool calls needed or attempted.

This satisfies HF-006's acceptance criteria: an inventory exists (above),
HermesFlow-mode prevents unapproved direct execution (mechanism +
verification above), Windmill-unavailable messaging is unchanged from
HF-005's testing and is now backed by the *absence* of any fallback tool
rather than only the model's willingness to say no, and ordinary
conversation is confirmed unaffected.
