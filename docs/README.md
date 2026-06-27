# Bootstrap notes — beyond AGENTS.md

`AGENTS.md` is the canonical, maintained source of conventions for this repo and should be read first. This file fills in repo-navigation context that's useful but isn't a "rule" worth stating there.

## Repo navigation notes

- `docs/baserow-accounts-and-sharing.md` details the sharing model in the baserow application and it's implications for setup
- `docs/hermes-docker-build.md` has the full rationale for the lazy-install security model referenced (but not fully reproduced) in AGENTS.md's "Adding Python packages for Hermes" section.
- `docs/windmill-sync.md` has the full sync-scope breakdown; AGENTS.md only summarizes it.
- CI is two workflows: `.github/workflows/ci.yml` (compose validation + Caddyfile validation + Python/ruff/YAML checks + a windmill script↔metadata↔lockfile consistency check — all static, no running server) and `.github/workflows/hermes-image.yml` (path-filtered to `hermes/**`, builds the derived Hermes image to confirm `requirements.txt` pins resolve against `LAZY_DEPS`). Note: `docker-compose.directus.yml` is still not in the CI compose-merge check — see AGENTS.md's wiring checklist.
- Full Makefile target list (for quick lookup, not reproduced in AGENTS.md): `bootstrap`, `wizard`, `secure`, `secrets`, `apikey`, `init`, `check`, `fix-permissions`, `pull`, `build`, `up`/`down`/`restart`, `logs`, `ps`, `health`, `backup`, `validate`, `lint`, `ci`, `hermes-heal`, `hermes-workspace`, `hermes-secure`, `headroom`/`headroom-revert`, `mlx`/`mlx-revert`/`mlx-status`, `memory`/`memory-revert`, `hindsight-mlx`/`hindsight-mlx-revert`, `aux-cloud`/`aux-local`/`aux-hindsight`/`aux-status`, `windmill-push`/`windmill-pull`/`windmill-check`, `baserow`/`baserow-revert`/`baserow-mcp`,`directus`/`directus-revert`.
