# Lock File Recovery — Windmill Script Lock Wiped/Empty

When a Windmill Python script's lock file is empty (`# py: 3.12` with no packages), the worker can't install dependencies and the script fails with `ModuleNotFoundError` on the first third-party import.

## How the Lock Gets Emptied

- **`mcp_windmill_createScript` creates a script without a lock** — the MCP tool has no `lock` parameter, so deploying/updating a script via MCP stores an empty lock.
- **`wmill generate-metadata` on the full tree** — can wipe lock files of scripts with imports inside function bodies (already documented in SKILL.md pitfall #2, but also applies to dbt_run which has `from dbt.cli.main import dbtRunner` inside the `main()` function).
- **Any deploy path that doesn't preserve the existing lock** — if a script is updated via MCP, the old lock is lost.

## Detection

```python
# Via MCP:
mcp_windmill_getScriptByPath(path="f/data_platform/dbt_run")
# Look for: "lock": "# py: 3.12\n"
```

A healthy lock has `# py: 3.12\nagate==1.9.1\n...` (50+ lines for dbt).

## Recovery: Generate the Lock

From any host with `uv` and network access:

```sh
# For dbt-run scripts (dbt-core + dbt-duckdb)
uv pip compile --python-version 3.12 - <<'EOF' > /tmp/dbt_lock.txt
dbt-core==1.11.11
dbt-duckdb==1.10.1
requests
EOF

# Format for Windmill
{ echo "# py: 3.12"; grep -E '^[a-zA-Z0-9_.\-]+(\[[a-z,]+\])?==' /tmp/dbt_lock.txt; } > /tmp/dbt_lock_formatted.txt
```

For extract scripts (dlt-based), see step 4 of the main SKILL.md (includes the `setuptools<81` pin).

## Recovery: Push the Lock

### Option A: Windmill UI (works from any environment)

1. Open Windmill UI → Scripts → `f/data_platform/<script_name>`
2. Click **Script Editor** → **Dependencies** tab
3. Replace the empty lock content with the formatted output from above
4. Click **Save**

### Option B: `wmill sync push` (needs CLI on local machine)

```sh
# Push from local repo with lock file on disk
wmill sync push --yes
```

### Option C: Windmill REST API (needs direct network access)

```sh
curl -X PUT \
  -H "Authorization: Bearer $WM_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"lock\": \"# py: 3.12\nagate==1.9.1\n...\"}" \
  "$BASE_INTERNAL_URL/api/w/main/scripts/lock/f/data_platform/dbt_run"
```

## MCP Limitation: Can't Pass Required Parameters

`mcp_windmill_runScriptByPath` only accepts a `path` — **no `args` parameter**. This means:

- **`f/data_platform/dbt_run`** requires a `db` resource parameter. The MCP run tool can't provide it, so it always runs with `db=None` and crashes with `TypeError: 'NoneType' object is not subscriptable`.
- **Workaround**: trigger via Windmill UI (the resource form auto-fills the `data_platform_db` resource) or via the REST API with `{"db": "f/data_platform/data_platform_db"}` in the request body.

### REST API Trigger (with resource param)

```sh
curl -X POST \
  -H "Authorization: Bearer $WM_TOKEN" \
  -H "Content-Type: application/json" \
  "$BASE_INTERNAL_URL/api/w/main/jobs/run/p/f/data_platform/dbt_run" \
  -d '{"db": "f/data_platform/data_platform_db"}'
```

## Verify Recovery

```sh
# Check the lock is populated
mcp_windmill_getScriptByPath(path="f/data_platform/dbt_run")
# lock should now show 50+ lines of packages

# Trigger the script
mcp_windmill_runScriptByPath(path="f/data_platform/dbt_run")
# ... still fails from MCP because db param can't be passed, but check the job logs:
# ModuleNotFoundError should be gone; next error will be about the db param
```
