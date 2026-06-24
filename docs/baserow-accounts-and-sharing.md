# Baserow accounts, MCP, and sharing data

This explains how Baserow accounts and workspaces relate to the
[`make baserow-mcp`](../README.md#hermes--baserow-over-mcp) bootstrap, what the
account-creation options are, and how to let other people (or agents) see the
same data. It applies to the optional Baserow add-on
([`docker-compose.baserow.yml`](../docker-compose.baserow.yml)).

## The one thing to understand: data is workspace-scoped

In Baserow, all tables live inside a **workspace**. **Visibility in the UI ==
workspace membership** — you only see a workspace's databases/tables if your
account is a member of that workspace. This is core open-source behaviour (not a
paid feature).

Two independent doors lead into the same workspace data:

- **The Baserow UI** — humans who are *members* of the workspace.
- **An MCP endpoint** — exposes one workspace's data to Hermes as tools. The
  endpoint is owned by a user and bound to a workspace; its key (embedded in the
  URL `…/mcp/<key>/sse`) **is** the credential, so treat it as a secret.

`make baserow-mcp` creates (or reuses) an MCP endpoint named `hermes` in a
workspace and registers it with Hermes via the baked-in `mcp-remote` bridge.

## What `make baserow-mcp` does with credentials

It reads `BASEROW_EMAIL` / `BASEROW_PASSWORD` from `.env` (or prompts), then:

1. **Tries to log in** with those credentials.
2. **If login fails, it creates the account** with that email/password (requires
   self-service signups to be enabled — the default; see *Caveats*).
3. Ensures a workspace exists (uses `BASEROW_MCP_WORKSPACE` by name if set,
   otherwise your first workspace, otherwise creates one called `Hermes`).
4. Creates/reuses the `hermes` MCP endpoint in that workspace and wires Hermes.

The whole thing is idempotent — re-running reuses the existing account, workspace,
and endpoint.

## Account options

### Option A — Your own account (recommended for a single user)

Use the account you manage data with. The agent and you operate on the **same**
workspace, so anything you create in the UI is immediately visible to Hermes and
vice-versa. No sharing step needed.

```sh
# .env
BASEROW_EMAIL=you@example.com
BASEROW_PASSWORD=your-baserow-password
```

If the account doesn't exist yet, `make baserow-mcp` now creates it for you.

### Option B — A dedicated "bot" / service account

Point `BASEROW_EMAIL`/`BASEROW_PASSWORD` at a separate account just for the agent
(e.g. `agent@example.com`). `make baserow-mcp` creates it and puts the MCP
endpoint in **that account's** workspace.

> **Caveat:** the data then lives in the bot's workspace, which your *human*
> account can't see until you share it. To view/edit it in the UI you must invite
> your human account into the bot's workspace (see *Sharing* below), or log into
> the UI as the bot account.

This is the trade-off that made Option A the default: with a bot account you get
isolation, but you take on a sharing step.

## Sharing data with other users

Workspace membership is what grants UI access, and inviting members is part of the
free self-hosted version.

**In the UI:** open the workspace → **Members** (workspace settings) → **Invite** →
enter the person's email. They accept and then see the workspace's tables.

**Via the API** (same pattern `make baserow-mcp` uses):

```sh
BURL=http://localhost:3010   # or http://baserow.localhost
# Log in as a workspace admin to get a JWT:
TOKEN=$(curl -fsS -X POST "$BURL/api/user/token-auth/" -H 'Content-Type: application/json' \
  -d '{"email":"owner@example.com","password":"…"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')
# Invite someone to workspace <WSID>:
curl -fsS -X POST "$BURL/api/workspaces/invitations/workspace/<WSID>/" \
  -H "Authorization: JWT $TOKEN" -H 'Content-Type: application/json' \
  -d '{"email":"teammate@example.com","permissions":"MEMBER","message":"","base_url":"http://baserow.localhost"}'
```

**Roles:** the free version offers `ADMIN` and `MEMBER` (both can view and edit
data). Fine-grained roles (viewer / commenter / per-table permissions, i.e. RBAC)
are a paid Baserow feature — but plain viewing via membership works on OSS.

## Caveats

- **The agent only sees the endpoint's workspace.** Data you create in a
  *different* workspace is invisible to Hermes (and vice-versa). Pin the workspace
  with `BASEROW_MCP_WORKSPACE=<name>` if you have more than one.
- **Auto-create needs signups enabled.** If `allow_new_signups` is off (Baserow
  admin → Settings), `make baserow-mcp` can't create the account — make it in the
  UI first. If the email already exists but the password is wrong, creation fails
  too; the target tells you so.
- **The first account on a fresh instance becomes an instance admin** (`is_staff`)
  — it can manage global settings. Keep that in mind when auto-creating the very
  first account.
- **`BASEROW_PASSWORD` lives in `.env`** (git-ignored). It's your Baserow login —
  if you'd rather not store it, leave it blank and you'll be prompted.
- **The MCP endpoint key is a credential.** Anyone with the
  `…/mcp/<key>/sse` URL has full tool access to that workspace. Revoke it by
  deleting the `hermes` endpoint in Baserow (workspace settings → **MCP**) or via
  `DELETE /api/mcp/endpoint/<id>/`; then re-run `make baserow-mcp` to mint a new
  one.
- **Deleting the account deletes its workspaces and endpoints** (and therefore the
  agent's data and access). Back up with `make backup` (which dumps `baserow_db`).

## See also

- [README → Baserow (structured data)](../README.md#baserow-structured-data)
- [README → Hermes ⇄ Baserow over MCP](../README.md#hermes--baserow-over-mcp)
