# Generation policy

When SKILL.md's Rule 2 search (see `capability-selection.md`) turns up
nothing that covers the request, generating new code is allowed — but only
under Rule 3's candidate-before-mutation constraint, and only carrying the
metadata a reviewer will actually need.

## Where new code goes

Always a **candidate** path, never a direct write into an active path.
[`architecture/adr/0002-capability-lifecycle.md`](../../../../../architecture/adr/0002-capability-lifecycle.md)'s
lifecycle is `REQUESTED -> SEARCH -> COMPOSE/REUSE/GENERATE -> CANDIDATE ->
TESTED -> ACTIVE -> ...` — generation produces the `CANDIDATE` state, full
stop. The candidate namespace
(`f/hermes_flow/candidates/`, per
[HF-007](https://github.com/ryancollingwood/HermesFlow/issues/45)) is
deliberately kept out of Windmill sync scope the same way
`f/hermes_state/` is, so a candidate existing or changing never shows up as
drift against active code.

This is true no matter how small the change looks. "Just add one field" or
"just fix this one typo" on an *active* capability is still a mutation of
active code and still needs the candidate → test → promote path — there is
no size-based exception.

For a repair of a failed active script, use
`f/hermes_flow/repair/generate_candidate` with the complete HF-029
`RepairContext` instead of authoring a patch directly. HF-030 pins the candidate
to the inspected active Windmill hash, retains the exact prompt/context/model
outputs, requires a concrete test update, and rejects stale, invalid, unsafe, or
effect-expanding patches before candidate creation.

## What a new candidate must carry

Author a `CapabilityMetadata` record (`f/libraries/capability/models.py`)
alongside the code, not as an afterthought:

- **`summary`** — the one-line, agent-facing description another session
  will use to decide whether to reuse this instead of generating a
  duplicate. Write it for a reader who has never seen this task, not for
  yourself right now.
- **`maturity=experimental`** — a brand-new candidate has not proven
  itself; don't mark it `stable` preemptively. Maturity is earned by
  passing through `TESTED`/`ACTIVE`, not asserted at creation.
- **`owners`** — required, non-empty. Generated code without an owner is an
  orphan the moment the session ends.
- **`effects`** — declare `network`/`filesystem`/`database`/`external`
  honestly. Under-declaring effects (marking something side-effect-free
  when it isn't) undermines every downstream consumer that trusts this
  field, including a future policy evaluator
  ([HF-010](https://github.com/ryancollingwood/HermesFlow/issues/48)) that
  will make automation decisions based on it.
- **`test_requirements`** — what a promotion reviewer needs run before this
  can move past `TESTED`. Reference concrete test paths/ids
  (`windmill/tests/contracts/...` once
  [HF-015](https://github.com/ryancollingwood/HermesFlow/issues/53) lands
  that convention), not "should be tested."
- **`dependencies`** — any other capability path this one relies on, so a
  future consumer-impact check ([HF-012](https://github.com/ryancollingwood/HermesFlow/issues/50))
  has something to traverse.
- **`limits`** — set a `timeout_seconds` at minimum for anything that calls
  out to a network or external service. Unbounded-by-default is not a safe
  default for generated code you haven't operated yet.

## Code requirements

CI runs `ruff check --fix` against the whole tree, but that `--fix` only
cleans up the checkout the CI job runs in — it never writes the fix back
into the candidate's committed code. Don't rely on CI to catch or repair
these; run `ruff check` (`--fix` for the safe/cosmetic rules) against a
candidate yourself and get it clean before marking it `TESTED`.

A handful of rules ruff can't auto-fix are also the mistakes generated
code reproduces most often. Avoid these outright rather than waiting for
lint to catch them:

- **No mutable default arguments** (`def f(x=[])`, `def f(x={})`). The
  default object is shared across every call that doesn't pass `x`
  explicitly — a stale-state bug, not a style nit. Default to `None` and
  assign the real value inside the function body.
- **No bare or blanket `except:` / `except Exception:`.** Catch the
  specific exception type the call can actually raise. A blanket catch
  swallows bugs the same way an under-declared `effects` field hides
  side effects — it undermines the next reader's ability to trust what
  the code does.
- **Don't close over a loop variable** in a lambda or nested function
  defined inside the loop. By the time the closure runs, the variable
  holds whatever the loop left it at, not the value it had when the
  closure was created. Bind it as a default argument
  (`lambda x=x: ...`) or capture it explicitly instead.
- **Datetimes are timezone-aware, always** — `datetime.now(timezone.utc)`,
  never `datetime.now()` or `datetime.utcnow()`. A naive datetime
  silently carries no timezone, and any comparison against a
  `CapabilityMetadata`/`ExecutionResult` timestamp downstream will
  misbehave the moment the two disagree.
- **Type-check branches raise `TypeError`**, not a message-only
  exception, when the input's type is wrong — a caller matching on
  exception type needs it to actually be `TypeError`.

Everything else ruff flags — `Optional[X]` vs `X | None`, import
ordering, and similar — is what `ruff check --fix` exists for. Run it and
move on rather than hand-fixing style.

## What autonomy a new candidate gets

`AutonomyPolicy`'s defaults apply unless there's a specific reason to
override them: `discover`/`execute`/`compose`/`create_candidate`/
`modify_candidate` are `automatic` (candidate-namespace-only actions never
touch what's active, so they don't need a human in the loop), and
`promote`/`schedule` are `approval_required` — structurally, not by choice
(see SKILL.md Rule 3). Don't try to widen the automatic set for a
candidate you're confident in; confidence isn't what the promotion gate is
checking for.

## Composition over duplication

If a new capability needs something an existing primitive already does,
call that primitive rather than re-implementing its logic — this is Rule 2
applying recursively inside generation itself, not just at the top-level
search. A generated workflow that duplicates three existing primitives'
logic inline has created exactly the maintenance burden Rule 2 exists to
avoid, even though it technically counts as "new code" rather than "code
that reused nothing."
