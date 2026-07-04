# Creating and maintaining Hermes skills

This covers Hermes "skills" (Markdown playbooks Hermes's `skills_hub` loads
and routes to) — where the **custom** ones for this repo live, how to author
and deploy them, and how to keep them from drifting once Hermes itself edits
them live. For coding-agent skills (`claude-code`/`opencode`/`codex`)
specifically, see [docs/hermes-coding-agents.md](hermes-coding-agents.md).

## Two skill locations — don't confuse them

| Location | What lives there | Versioned here? |
|---|---|---|
| `/opt/hermes/skills/` (baked into the image) | Hermes's own bundled skills (e.g. `autonomous-ai-agents/{claude-code,opencode,codex}`) | No — upstream, not ours to edit |
| `/opt/data/skills/` (the `DATA_DIR` bind mount) | Everything else: Hermes's curated/installed skill library *and* this repo's custom skills | Only the custom ones, via `hermes/skills/` |

`/opt/data/skills/` on a running deployment is a big tree — bundled
categories like `apple/`, `devops/`, `research/`, `social-media/`, plus
Hermes-internal bookkeeping (`.bundled_manifest`, `.curator_state`,
`.curator_backups/`, `.usage.json[.lock]`, `.hub/`) that the `skills_hub`
auxiliary task manages itself. **None of that belongs in this repo.** Only
the skills *we* author for this stack do — currently
[`hermes/skills/data-science/data-platform-add-pipeline/`](../hermes/skills/data-science/data-platform-add-pipeline/),
which teaches Hermes the dlt → dbt → Windmill pipeline workflow from
[docs/data-platform-add-pipeline.md](data-platform-add-pipeline.md).

This is the same bind-mount-plus-narrow-scope pattern as `windmill/` (see
[docs/windmill-sync.md](windmill-sync.md)): the live directory has far more
in it than what's tracked, and the tooling is deliberately scoped so it can
only ever touch the paths we actually own.

## Anatomy of a skill

```
hermes/skills/<category>/<skill-name>/
├── SKILL.md              # required
└── references/           # optional — supplementary docs SKILL.md points to
    └── *.md
```

`SKILL.md` frontmatter (see the data-platform skill for a worked example):

```yaml
---
name: <skill-name>                  # matches the directory name
description: <one-line summary>     # what skills_hub uses to route to this skill
version: <semver>                   # bump on any substantive content change
author: <your name>
license: MIT
metadata:
  hermes:
    tags: [topic, keywords, ...]            # discovery hints
    related_skills: [other-skill-name]      # cross-links, not enforced
---
```

Body convention (mirrors `data-platform-add-pipeline`'s structure — not
enforced by tooling, but keep new skills consistent with it):

- **Architecture tl;dr** — one paragraph, enough to orient an agent that's
  never seen this skill before.
- **When to use / don't use** — saves a wrong invocation.
- **Step-by-step workflow** — numbered, with copy-pasteable commands.
- **Known gotchas inline**, not just in "Common Pitfalls" — by the time an
  agent hits a pitfall it's already mid-task; the warning needs to be at the
  step where the mistake happens, not just listed at the bottom.
- **`references/`** for anything long enough to clutter the main flow (a
  recovery runbook, an upstream-blocking playbook) — link to it, don't inline
  it.
- **A pointer back to the canonical human-authored doc**, if one exists (see
  `data-platform-add-pipeline`'s top-of-file note). Skills written for
  MCP/agent use and repo docs written for humans cover the same ground from
  different angles and *will* drift apart — say which one wins.

## Creating a new skill

1. Author it under `hermes/skills/<category>/<skill-name>/` in this repo
   first — same reasoning as `windmill/`: a human/PR-reviewable source of
   truth beats reconstructing it later from whatever's live.
2. Push it to the live deployment:

   ```sh
   make hermes-skills-push
   ```

   This copies `hermes/skills/` into `DATA_DIR/skills/` **additively** — it
   only ever copies into the tree, never deletes. Existing skills under
   `DATA_DIR/skills/` that aren't in this repo (everything Hermes bundles or
   you've installed/curated separately) are left completely alone. Both
   installers (`install.sh`/`install.py`) run this automatically as their
   final step, so a fresh install picks up whatever's tracked here.
3. Confirm Hermes picked it up — `skills_hub` discovers skills from the
   filesystem; a restart isn't strictly required but is the simplest way to
   force a re-scan if you don't see it routed to:

   ```sh
   docker compose restart hermes
   ```

## Maintaining a skill — the part that actually matters

The failure mode worth designing around: **Hermes (or another agent) edits
the skill live** — via its own file tools, or because you asked it to "fix
its own playbook" — instead of you editing the repo copy. This already
happened once with `data-platform-add-pipeline`: a Hermes conversation
rewrote `dbt_run`'s resource-handling pattern *and* its own skill file
documenting that pattern, entirely server-side, and the repo copy didn't
know about any of it until someone went and looked.

**Always pull and audit before trusting a live skill matches the repo:**

```sh
make hermes-skills-pull
git diff -- hermes/skills/
```

`hermes-skills-pull` is deliberately narrow, mirroring `hermes-skills-push`'s
safety direction: it only pulls back skill directories that **already exist**
under `hermes/skills/` in this repo. It will never reach into `DATA_DIR/skills/`
and import a bundled or separately-installed skill you never asked to track —
same principle as `wmill.yaml`'s narrow `includes`, applied here without
needing a config file because the scope is just "whatever's already a
subdirectory of `hermes/skills/`".

What to actually look for in the diff, same lessons as
[docs/data-platform-add-pipeline.md](data-platform-add-pipeline.md)'s "audit
the pull" step:

- **Version bump** — did `version:` in the frontmatter actually change to
  reflect the edit? If Hermes (or you) forgot, bump it as part of the review.
- **Lost rationale** — live edits, especially LLM-driven ones, tend to
  flatten "why we do it this way" into "what it does." If a gotcha or design
  reason got silently dropped, that's a regression even if the new prose
  reads fine on its own.
- **New pitfalls documented, old ones still relevant** — a live edit that
  adds a "Pitfall #14" without removing or updating a now-stale "Pitfall #2"
  leaves the next reader to figure out which one still applies.
- **Cross-references still resolve** — if the skill points at a repo doc
  (`docs/data-platform-add-pipeline.md`, a Makefile target, a script path),
  confirm that thing still exists and the skill's description of it is still
  accurate, especially after you've separately updated the doc it's pointing
  at.

A `.DS_Store` (or similar OS noise) occasionally rides along in the pull on
macOS — harmless, already in `.gitignore`, no need to chase it out by hand.

## Why additive, never a mirror

`hermes-skills-push`/`-pull` intentionally use a plain `cp -R`, not
`rsync --delete` or anything that could remove files. This is a direct
lesson from a real incident (see the `[!WARNING]` at the top of
[docs/windmill-sync.md](windmill-sync.md)): a destructive mirror run with the
wrong scope hard-deleted production state that had no backup. Skills don't
carry secrets or backing data the way Windmill resources do, but the same
*class* of mistake — "sync everything, including the parts you didn't mean
to touch" — is just as easy to make here, so the tooling structurally can't
do it. If a skill genuinely needs to be removed, delete it explicitly on
both sides (`rm -rf hermes/skills/<path>` and the matching path under
`DATA_DIR/skills/`) — never reach for a `--delete`/mirror flag to do it for
you.
