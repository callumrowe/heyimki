# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Ki is a self-hostable personal AI assistant: a FastAPI agent with persistent memory, an Obsidian vault it reads and writes, browser tools, a proactive heartbeat, a PWA web app and an optional WhatsApp gateway. Anyone should be able to clone the repo, run `./setup.sh` and get their own instance, locally or on a VPS. **Keep it that way:** no personal names, hosts, paths or accounts in code, prompts or docs. Instance-specific values come from `.env`.

## Layout

- `api/` — Python + FastAPI. Agent loop (`src/agent.py`), routes and scheduler (`src/main.py`), heartbeat (`src/heartbeat.py`), vault indexer (`src/indexer.py`), memory and DB helpers (`src/memory.py`, `src/db.py`), instance config (`src/config.py`).
  - `src/tools/` — tools called during a conversation: `obsidian.py` (vault), `browser.py`, `curl.py`.
  - `src/integrations/` — auto-discovered modules (see below).
  - `prompts/` — `identity.md` (personality), `instructions.md` (operating rules), `heartbeat.md` (synthesis), `coding_agent.md` (added only when the coding agent is on).
  - `web/` — the PWA (vanilla JS), served at `/app/`.
  - `migrations/` — SQL, applied at startup.
- `baileys/` — Node WhatsApp gateway (compose profile `whatsapp`).
- `coding-agent/` — Node service that runs Claude Code against the repo (profile `coding-agent`).
- `deploy/Caddyfile` — TLS reverse proxy (profile `https`).
- `examples/vault/` — starter Obsidian vault; `setup.sh` copies it to `./vault`.
- `bin/ki` — management CLI (local by default, remote with `KI_HOST`).
- `setup.sh` — interactive first-run setup that writes `.env`.

## Commands

```bash
./setup.sh                                  # first run: writes .env, starts the stack
docker compose up -d --build                # build + start (profiles from COMPOSE_PROFILES in .env)
docker compose logs -f api                  # tail api logs
bin/ki help                                 # management commands
python3 -m py_compile api/src/*.py          # quick syntax check
node --check baileys/src/index.js coding-agent/src/server.js api/web/app.js
```

There is no test suite. Smoke-test changes by bringing the stack up and hitting `/health`, then the web app.

## Configuration rules

- **The user's identity comes from `.env`.** `USER_NAME` and `USER_TZ` are read in `src/config.py`. Prompts use `{{USER_NAME}}` / `{{USER_TZ}}` placeholders, filled by `config.render()`; this applies to prompt values stored in the DB as well. Python tool descriptions say "the user". Transcript labels use `config.USER_LABEL`. Never hardcode a name, timezone (the default is `UTC`), hostname or path.
- **Optional services are compose profiles.** `COMPOSE_PROFILES` in `.env` is read by compose and also passed into the api, where `config.CODING_AGENT_ENABLED` / `WHATSAPP_ENABLED` derive from it. The coding-agent tools (`delegate_coding_task`, `deploy_changes`, `get_service_logs`) and `prompts/coding_agent.md` are only given to the model when the coding agent is enabled.
- **Optional integrations are gated by env vars.** Each one is listed in its module's `REQUIRED_ENV_VARS`. Add new env vars to `docker-compose.yml` (with a `:-` default), to `.env.example` (with a comment), and to the README table.

## Security model (keep these invariants)

- **Every route needs auth except** `/health`, `/`, the icons, `/push/vapid-public-key` and the Google OAuth callback. The web app uses `DESKTOP_SECRET` (`_require_desktop_auth`). Baileys and the CLI use `INTERNAL_SECRET` (`_require_internal_auth`, which also accepts `DESKTOP_SECRET`). The headless browser shares the internal network, so any web page Ki opens can reach internal services. Internal is not trusted.
- **The coding agent requires `Authorization: Bearer $INTERNAL_SECRET` on every endpoint except `/health`.** The api sends it via `_CODING_AGENT_HEADERS`.
- **The api container has no Docker socket.** Only the opt-in coding agent does.
- **`run_curl` must never use a shell.** It runs `create_subprocess_exec("curl", ...)` on `shlex`-split args.
- The api port binds to `127.0.0.1` by default. Public access goes through the `https` profile.

## Vault

- The vault is any Obsidian vault folder, bind-mounted at `/vault` from `KI_VAULT_PATH`.
- **Git is optional.** All write paths go through `_git()` in `tools/obsidian.py`:
  - not a git repo → no-op
  - repo without a remote, or `VAULT_GIT_SYNC=off` → `pull`/`push` are skipped
  - "nothing to commit" is not an error
  - commits use the identity `Ki <ki@localhost>` unless `GIT_AUTHOR_*` is set
  - an SSH key at `/secrets/vault_deploy_key` is used if present
- **The `vault_sync` scheduler job** runs every `VAULT_SYNC_MINUTES`: it pulls (git vaults with a remote only), then calls `reindex()`.
- **Indexer change detection** (`indexer.py`):
  - git vaults use `rev-parse HEAD` and `git diff`
  - plain folders use an `fs:<newest mtime_ns>:<count>` fingerprint and re-index files modified since the last run
  - force a full reindex with `bin/ki reindex`, which deletes `indexer_state.vault_commit` and restarts the api
- **Chunking:**
  - root-level notes and `templates/` are skipped
  - daily notes (`daily/YYYY-MM-DD.md`) are chunked one point per top-level bullet group from the content above the final `---`, and open `- [ ]` items become `doc_type: "todo"` points
  - point IDs use `hashlib.md5` (stable across restarts)
- **Writes into `## Daily notes`** insert before the next `---` or `##`.
- qdrant-client 1.12+ uses `query_points()` (returns `.points`), not `search()`.

## Integrations

Each file in `api/src/integrations/` exposes `REQUIRED_ENV_VARS`, `TOOL_SCHEMAS`, `SCHEDULES` and `async def run_tool(name, input)`, plus `get_schedule_fn(name)` if it has schedules. `loader.py` imports every module whose name doesn't start with `_` and skips any with missing env vars. Files prefixed `_` are libraries. The agent builds its tool list from static tools plus integration schemas. `main.py` registers integration schedules.

## Heartbeat

`heartbeat.py` runs every `HEARTBEAT_HOURS` outside quiet hours (`HEARTBEAT_QUIET_START`–`HEARTBEAT_QUIET_END`, in `USER_TZ`).

- **Each run:** `reindex()`, then recent conversations plus vault todos, then a Sonnet synthesis into state JSON (`open_todos`, `backlog_todos`, `open_loops`, `current_projects`, `recent_themes`, `things_to_mention`, `ki_commitments`, `snoozed`, `proactive_message`, `last_updated`).
- **Delivery:** the state is injected into every conversation's system prompt as `## Current context`. `proactive_message` is saved to the `desktop` channel, sent as web push, and sent to WhatsApp if enabled, with a 2-hour cooldown.
- **Gotchas:**
  - strip markdown fences before `json.loads`
  - overwrite `last_updated` server-side
  - use `CAST(:state AS jsonb)`, not `::jsonb` (SQLAlchemy bind params)

## Prompts

`_load_prompt()` in `agent.py` prefers the DB (`settings` key `prompt:{name}`, editable in the admin UI) over the file on disk. Files are seeded into the DB on first boot, so later edits to `identity.md` / `instructions.md` only apply on a fresh install, or after `DELETE FROM settings WHERE key = 'prompt:identity'`.

## Coding agent (opt-in)

- `POST /code {task}` spawns Claude Code in `/repo` (the repo bind mount), then commits.
- It pushes to the current branch on `origin` only when `CODING_AGENT_GIT_PUSH=true`.
- `/restart` restarts `ki-api-1` and `ki-coding-agent-1`. The compose project is named `ki`, so those container names are stable.
- `/deploy` runs `docker compose --project-directory $REPO_HOST_PATH up --build -d`. `REPO_HOST_PATH` comes from `KI_REPO_DIR` in `.env`, and the entrypoint symlinks it to `/repo`, so the host daemon receives host paths.
- The entrypoint runs the agent as the UID that owns the checkout, or hands a root-owned checkout to the `ki` user, because Claude Code refuses to run as root.

## Safe migration practices (follow every time)

1. Read all of `api/migrations/*.sql` before writing a new one. Never assume a fresh schema.
2. Never pair `CREATE TABLE IF NOT EXISTS` with a bare `CREATE INDEX` on a new column. If the table already exists, the column was never added.
3. Add columns with `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`.
4. Always use `CREATE INDEX IF NOT EXISTS`.
5. Only use `CREATE TABLE IF NOT EXISTS` for brand-new tables.

`run_migrations()` in `db.py` tracks applied files in `schema_migrations`. It splits each file on `;` (asyncpg can't run multi-statement strings), and it retries the Postgres connection with backoff.

## Other notes

- WhatsApp JIDs on multi-device use `@lid`, not `@s.whatsapp.net`. With `YOUR_WHATSAPP_JID` unset, Baileys answers nobody and logs the JID of each incoming chat.
- `PYTHONUNBUFFERED=1` is set, so `print()` shows up in `docker compose logs` straight away.
- Phoenix instrumentation is wrapped in `try/except`, so the api still starts if Phoenix is down.
- `SESSION_GAP_HOURS` (default 0.5): after this long without a message, the conversation starts fresh, and heartbeat state carries the context.
