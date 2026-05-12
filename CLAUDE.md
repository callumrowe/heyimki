# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

`ki` is a self-hosted personal AI agent (see `personal-ai-agent-runbook.md` for the full architecture and `plan.md` for the detailed build plan). It runs on a VPS (`root@<your-server-ip>`) and is accessible via WhatsApp, with persistent memory, Obsidian vault integration, browser access, and a proactive heartbeat.

**Current status:** Phases 1–7 are live. Voice message transcription is live. PWA web app built (March 2026). Desktop app with streaming responses, bridge, and admin UI built. Phase 6 (Ki builds integrations) built March 2026 — coding-agent service proven; Ki has since authored: `time_tools`, `reminders`, `web_search`, `image_gen`. Phase 7 (broader self-modification) built March 2026 — Ki has full read/write access to the entire repo via coding-agent; no scope restrictions or confirmation gate. Image support live (Baileys extracts images, Claude sees them as vision blocks). Document support live (Baileys detects `documentMessage`, PDFs forwarded as Claude `document` content blocks; other types return a friendly error). Voice message support live (Baileys detects `audioMessage`, downloads bytes, API transcribes via OpenAI Whisper, forwards as text). `ki integrations` CLI command live. See `plan.md`. WhatsApp → Claude pipe works with persistent Postgres memory. Obsidian vault is indexed into Qdrant on startup (text-embedding-3-small); agent has search_vault, read_note, append_to_note, write_note tools and auto-injects top-5 vault excerpts per message (filtered to score ≥ 0.35 — below-threshold results are logged but not sent to Claude). Heartbeat runs every 3h during active hours, injects current context into every conversation, and sends proactive WhatsApp messages written in Ki's voice. Browser tools (Playwright + browser-use) and a consequential action gate are live. Phoenix traces every LLM call and tool use.

**Vault indexer — preprocessing pipeline (built March 2026):**
- Root-level `.md` files and `/templates/` are skipped
- Daily notes (`daily/YYYY-MM-DD.md`): content above the final `---` separator is extracted; Obsidian Tasks plugin blocks stripped; open `- [ ]` todos extracted as separate `doc_type: "todo"` Qdrant points; remaining content chunked **one point per top-level bullet group** (not one blob per day)
- Regular notes: Obsidian syntax stripped, nested bullets flattened (sub-bullets appended to parent with `. `), chunked per top-level bullet line within each heading section — note title and heading are prefixed into each chunk's content for embedding context
- Point IDs use `hashlib.md5` (stable across restarts — `hash()` was non-deterministic and caused duplicates on every restart)
- `search_vault` supports optional `doc_type` filter (`"note"`, `"daily_note"`, `"todo"`)
- On full reindex (no prior commit), Qdrant `obsidian` collection is wiped and recreated first
- Force a full reindex: `docker exec ki-postgres-1 psql -U agent agent -c "DELETE FROM indexer_state WHERE key = 'vault_commit';"` then `docker compose restart api`

**Reaction-to-close (built March 2026):**
- Reacting 👍 to any Ki WhatsApp message marks that topic as done
- Baileys tracks the last 50 sent message IDs → text; on 👍 reaction, POSTs the message text to `POST /reaction`
- `/reaction` calls `_classify_memory(reaction, message)` → Haiku picks the category (`preference`, `fact`, or `person`) and writes the memory content; saves the result via `mem.save_memory(category, content)`
- `fromMe` on the reaction key reflects the user's phone perspective (always `false` for Ki's messages); ID lookup in the sent-message cache is the authoritative check — if the ID is in the map, it was a Ki message

**Heartbeat (Phase 4, built March 2026 — extended through April 2026):**
- `api/src/heartbeat.py` — runs every `HEARTBEAT_HOURS` (default 3) via APScheduler; skipped entirely during quiet hours (no token spend overnight)
- Quiet hours: `HEARTBEAT_QUIET_START=22`, `HEARTBEAT_QUIET_END=7` (Auckland time) — no run, no synthesis, no messages
- Each run: calls `reindex()` first, then fetches 3h conversations for activity/themes and 24h conversations for snooze detection, scrolls all vault `doc_type=todo` points from Qdrant, deduplicates vault todos by content before synthesis, then calls Sonnet to synthesise state
- **Backlog tier (April 2026):** todos 14+ days old with no recent activity are moved to `backlog_todos` (not `open_todos`); a stale items block gives the model explicit signal for graduation. `open_todos` is capped at 12 to keep it actionable
- **Staleness pruning (April 2026):** open loops 21+ days old get an explicit DROP instruction in synthesis; projects 21+ days old get a move-to-themes-or-drop instruction. Both tracked in `item_metadata`
- **Date-aware ki_commitments (April 2026):** commitments formatted `YYYY-MM-DD: description` are code-injected into `things_to_mention` on their due date — not relying on the model to notice the date
- State JSON keys: `open_todos`, `backlog_todos`, `open_loops`, `current_projects`, `recent_themes`, `things_to_mention` (max 5), `ki_commitments`, `snoozed`, `proactive_message`, `last_updated`
- `snoozed`: items explicitly acknowledged or deferred in conversation — carries forward across runs; excluded from `things_to_mention` until condition is met. Uses 24h conversation window so acknowledgements don't fall outside the lookback
- `proactive_message`: Sonnet writes the WhatsApp message directly in Ki's voice — conversational, no bullet lists
- Always overwrite `last_updated` server-side — don't trust the model's timestamp
- Strip markdown fences from Sonnet JSON response before parsing (`json.loads`)
- `CAST(:state AS jsonb)` required in the INSERT — `::jsonb` breaks SQLAlchemy's bind param parser
- `max_tokens=2048` for synthesis
- State injected into every conversation as `## Current context` in the system prompt; `backlog_todos` shown as compact "Backlog (N): ..." section (6 items + overflow); `current_projects` capped at 8 shown; `recent_themes` capped at 5
- Proactive messaging: Baileys runs a Node `http.createServer` on port 3000; `POST /send` validates `INTERNAL_SECRET` and calls `activeSock.sendMessage()`; 2hr cooldown between proactive sends
- Manual trigger: `ki heartbeat` (see ki CLI below), or `POST /heartbeat/trigger` via `docker compose exec api python3 -c "..."`
- `INTERNAL_SECRET` and `BAILEYS_URL=http://baileys:3000` must be set in `.env` and wired into both containers in `docker-compose.yml`

**Ki identity + system prompt files (built March 2026):**
- `api/prompts/identity.md` — Ki's character: warm, direct, dry Kiwi/Australian wit, intentional swearing, proactive opinions, no AI self-awareness. Edit this to tune personality — no Python to touch
- `api/prompts/instructions.md` — operational rules: timezone, WhatsApp plain text, tool usage, daily note format
- `agent.py` calls `_load_prompt(filename)` which checks the DB (`prompts` table via `mem.get_prompt(name)`) first, then falls back to the file on disk. DB value takes precedence — editing a prompt via admin UI or Ki's coding-agent writes to the DB and takes effect immediately (no redeploy needed)
- To reset a prompt to its file version, delete the DB row: `ki db "DELETE FROM settings WHERE key = 'prompt:identity';"` (key format is `prompt:{name}`)
- `api/Dockerfile` COPYs `prompts/` into the image as the default fallback
**Ki CLI (built March 2026):**
- `bin/ki` — bash script for managing the stack from your local machine; add `~/DEVELOPMENT/ki/bin` to PATH
- Host config: `export KI_HOST=<VPS-ip>` or `echo <ip> > .ki-host` (gitignored) at repo root
- Commands: `ki up/down/restart/logs/ps [service]`, `ki deploy` (git pull + up --build), `ki heartbeat`, `ki memories [category]`, `ki state`, `ki usage`, `ki db '<sql>'`, `ki ssh`
- API calls use `docker compose exec -T api python3` — the api port is not published to the host, only accessible inside Docker

**Browser + action gate (Phase 5, built March 2026):**
- `api/src/tools/browser.py`: `navigate(url)` — singleton Playwright Chromium, strips noise, returns up to 4000 chars; `browser_act(task)` — browser-use LLM agent (Sonnet), vision=True, CAPTCHA-aware
- `api/src/actions.py`: pending action gate — `request_confirmation` tool stores action in Postgres with 10min expiry; "confirm"/"cancel" in next message executes or aborts
- `shm_size: '512m'` on api container required for Chromium

**PWA web app (built March 2026):**
- `api/web/` — vanilla JS/CSS/HTML Progressive Web App, adapted from Electron renderer
- Served by FastAPI at `/app` via `StaticFiles` mount (same-origin, no CORS needed)
- Accessible at `https://ki.example.com/app/` — routed by the platform Caddy container on the `core` Docker network
- `api` service joined to the `core` external network; port 8000 bound to `127.0.0.1` only; Caddy block: `ki.example.com { reverse_proxy ki-api-1:8000 }`
- Setup screen only requires the DESKTOP_SECRET (no host field — same-origin implied)
- Web Push notifications via VAPID (`pywebpush`): generate keys with `python3 -c "from pywebpush import Vapid; v=Vapid(); v.generate_keys(); print('PRIVATE:', v.private_key); print('PUBLIC:', v.public_key)"` then set `VAPID_PRIVATE_KEY`, `VAPID_PUBLIC_KEY`, `VAPID_SUBJECT` in `.env`
- Push subscriptions stored in `push_subscriptions` table (migration `010_push_subscriptions.sql`)
- Heartbeat sends web push + APNs to all subscribers alongside WhatsApp (concurrent via `asyncio.gather`)
- `api/web/sw.js` — service worker: handles web push events, notification click → focuses/opens app, caches app shell
- `./api/web:/app/web` bind-mounted in docker-compose so UI edits deploy without a full rebuild

**iOS native app (CapacitorJS, built April 2026):**
- `capacitor.config.json` + `package.json` at repo root — Capacitor v6, bundle ID `com.example.ki`, webDir `api/web`
- `app.js` detects `IS_CAPACITOR` (`window.Capacitor?.isNativePlatform?.()`) — conditionally uses APNs instead of VAPID
- APNs device tokens stored in `apns_tokens` table (migration `022_apns_tokens.sql`)
- `api/src/push.py` — `send_apns_to_all()` signs JWT with the APNs auth key and posts via HTTP/2; called from heartbeat and desktop stream completions
- Set `APNS_SANDBOX=true` for AltStore/dev builds (uses `api.sandbox.push.apple.com`); `false` for TestFlight
- **Push notifications require an Apple Developer account ($99/yr)** — the app itself can be sideloaded free via AltStore, but APNs won't fire until the server has APNS_KEY_ID/APNS_TEAM_ID/APNS_AUTH_KEY set

**iOS build steps (run on Callum's Mac, one-time setup):**
```bash
# 1. Install dependencies
cd ~/DEVELOPMENT/ki
npm install

# 2. Add the iOS platform (creates the ios/ Xcode project — gitignored, stays on your Mac)
npx cap add ios

# 3. Sync web assets into the iOS project
npx cap sync ios

# 4. Open in Xcode and build to your device
npx cap open ios
# In Xcode: select your iPhone as the target, hit Run (Cmd+R)
# Xcode will prompt for your Apple ID signing (free account is fine for sideload)
```

**Re-deploy flow (after any code change):**
```bash
cd ~/DEVELOPMENT/ki
npx cap sync ios    # copies updated api/web/ assets into the ios/ project
npx cap open ios    # or just rebuild in Xcode directly
```

**AltStore setup (first time):**
1. Install AltStore on your iPhone via altstore.io (requires AltServer on your Mac)
2. In AltServer on Mac: Install AltStore → select your iPhone
3. Open AltStore on iPhone, go to My Apps
4. Drag the `.ipa` exported from Xcode (Product → Archive → Distribute → Ad Hoc) into AltStore, or use `xcrun altool`
5. AltStore auto-refreshes the 7-day sideload when your Mac is on the same wifi

**Desktop app (built March 2026):**
- `api/web/` serves both the PWA (mobile-focused) and the desktop app — same origin, same auth (`DESKTOP_SECRET`)
- Desktop uses a separate `channel: "desktop"` so WhatsApp and desktop histories are independent; bridge feature (`POST /desktop/bridge`) summarises recent WhatsApp conversation and injects a handoff note into the desktop channel
- Streaming responses: `POST /desktop/stream` — SSE endpoint; agent streams tokens as `{"type": "delta", "text": "..."}` events; conversation is saved on completion
- Desktop reactions: `POST /desktop/reaction` — `{reaction, message_text}`; supports 👍 (done), ❤️ (appreciated), ✕ (dismissed); each calls `_classify_memory(reaction, message_text)` — Haiku picks category (`preference`, `fact`, or `person`) and writes the content
- Media grid: `GET /desktop/media` — returns all media (images) across all channels for the gallery view

**Admin UI (built March 2026):**
- Accessible at `https://ki.example.com/app/` (same PWA, admin tab) — requires `DESKTOP_SECRET`
- Schedule runs: `GET /admin/schedule-runs` — recent job execution history per integration schedule
- Heartbeat state: `GET /admin/state` (latest), `GET /admin/states` (all snapshots newest-first)
- Memories: `GET /admin/memories` — all stored memories
- Vault status: `GET /admin/vault-status` — current vault commit hash + Qdrant point count
- Settings: `GET/PUT /admin/settings` — runtime config (e.g., `image_gen_model`)
- Session: `GET /admin/session` — current session info (active, message count, age); `POST /admin/session/reset` — force a new session without waiting for gap timeout
- Prompts: `GET /admin/prompts`, `PUT /admin/prompts/{name}` — read and overwrite `identity` or `instructions` in the DB; changes take effect immediately with no redeploy

**Settings system:**
- `settings` table in Postgres stores arbitrary key→value pairs (text); used for both runtime config and prompt overrides
- `mem.get_setting(key, default=None)` / `mem.set_setting(key, value)` — used throughout for `image_gen_model`, `session_reset_before`, and `prompt:{name}` keys
- Prompt keys follow the convention `prompt:identity`, `prompt:instructions`

**Voice message support:**
- Baileys detects `audioMessage`, downloads bytes via `downloadMediaMessage`, forwards to `/message` as `audio: {data, mime_type}`
- `InboundMessage`: optional `audio: dict | None` field (`{"data": "<base64>", "mime_type": "audio/ogg; codecs=opus"}`)
- `agent.respond()`: calls `_transcribe_audio(data, mime_type)` → OpenAI Whisper `whisper-1`; transcript replaces `user_message` in the conversation; transcription failures fall through gracefully
- `OPENAI_API_KEY` already present for embeddings — no new credentials needed

**Ki-authored integrations (built via coding-agent, March 2026):**
- `time_tools` — `get_current_time`; no env vars; no schedules
- `reminders` — `set_reminder`, `list_reminders`, `cancel_reminder`; delivery handled by `reminder_checker` (scheduled every minute); reminders stored in `reminders` table; no env vars
- `web_search` — `web_search` tool using Brave Search API; requires `BRAVE_SEARCH_API_KEY`
- `image_gen` — `generate_image` tool using xAI Aurora (Grok API); generates image, sends directly to WhatsApp/desktop via Baileys `/send-image`; requires `XAI_API_KEY`; model overridable via `image_gen_model` setting in admin UI

**Baileys internal endpoints (all validate `INTERNAL_SECRET`):**
- `POST /send` — text message
- `POST /send-image` — `{data: base64, media_type, caption?, jid?}` — sends as WhatsApp image; `jid` defaults to `ALLOWED_JID`
- `POST /send-document` — `{data: base64, filename, media_type}` — sends as WhatsApp file attachment

**Observability — Phoenix (built March 2026):**
- `arizephoenix/phoenix:latest` runs as a Docker service; UI at `localhost:6006` via SSH tunnel (`ssh -L 6006:localhost:6006 root@<VPS>`)
- `openinference-instrumentation-anthropic` auto-instruments every `AsyncAnthropic` call — traces appear in the "ki" project in Phoenix
- Every trace shows: input/output messages, token counts, tool call inputs/outputs, latency, the full agentic loop as nested spans
- Instrumentation is wrapped in `try/except` in `main.py` — api still starts if Phoenix is slow or down
- Phoenix data persists in the `ki_phoenix` Docker volume

**Session gap (built March 2026):**
- `SESSION_GAP_HOURS` (default 0.5, i.e. 30 minutes) — accepts float; if the last message is older than this, `get_recent_conversation` returns `[]`
- Each new session starts with an empty messages array; the heartbeat state in the system prompt provides carry-over context
- Implemented as a single SQL query: the `WHERE` gate checks `MAX(created_at) > now() - gap_hours`
- If gap fires but last message was within 4 hours, Ki gets a session note so it can acknowledge the context drop without resuming tasks unprompted

**Integrations vs tools — the split:**
- `api/src/tools/` — synchronous Claude tool calls used in conversation (obsidian, browser)
- `api/src/integrations/` — autonomous background modules: scheduled jobs + optional conversation tools; auto-discovered at startup via `loader.py`

**Image support (built March 2026):**
- Baileys: `imageMessage` detected alongside text; `downloadMediaMessage` fetches bytes; base64-encoded + `mimetype` forwarded to `/message` as `image: { data, media_type }`
- `InboundMessage`: optional `image` field passed through to `agent.respond()`
- `agent.respond()`: if image present, builds a two-block vision content block `[{type: image, source: {type: base64, ...}}, {type: text, text: caption}]`; fallback caption is "What's in this image?" if none provided

**Phase 6 — Ki builds integrations (built March 2026):**
- Standard integration interface every file must expose: `REQUIRED_ENV_VARS`, `TOOL_SCHEMAS`, `SCHEDULES`, `async def run_tool(name, input)`, and `get_schedule_fn(name)` if SCHEDULES is non-empty
- `api/src/integrations/loader.py` — scans `integrations/` at startup; skips `_SKIP` filenames (pure libraries); skips integrations with missing env vars (logs warning)
- `agent.py` builds TOOLS list dynamically: static tools + all integration TOOL_SCHEMAS; dispatches via `_INTEGRATION_TOOL_MAP` (tool name → module)
- `main.py` registers integration SCHEDULES dynamically from loaded modules
- `./api/src:/app/src` bind-mounted into `api` container; `COPY src/` kept in Dockerfile as standalone fallback
- `coding-agent/` — Node.js Express service; `POST /code { task }` runs an Anthropic agentic loop (up to 30 tool calls); tools: `ki_read_file`, `ki_list_dir`, `ki_grep`, `ki_write_file`, `ki_edit_file`, `ki_check_syntax`; full repo access via `./:/repo` mount; git-commits on success; returns `{ summary, files_written, required_env_vars }`
- `delegate_coding_task` tool in `agent.py` — POSTs to `http://coding-agent:3002/code` with 300s timeout; Ki tells user what env vars to add and to run `ki deploy`
- Rollback: `git revert HEAD` on the VPS → `ki deploy`
- Git flow: coding-agent commits and pushes to GitHub after each session; `/root/.ssh` bind-mounted for auth

**Phase 7 — Full self-modification (April 2026):**
- coding-agent has full read/write access to the entire repo via `./:/repo` — single bind mount, no scope restrictions, no denylist
- All paths are repo-relative (e.g. `api/src/agent.py`, `baileys/src/index.js`, `docker-compose.yml`)
- `ki_check_syntax` supports both `.py` (py_compile) and `.js` (node --check)
- `ki_grep` searches `.py`, `.js`, `.md`, `.yml`, `.yaml`, `.sql`, `.txt`, `.json`
- No `scope` field on `delegate_coding_task` — removed entirely
- No confirmation gate — Ki uses judgement; for significant core changes, briefly describes what she's doing before delegating


**Memory system improvements (April 2026):**
- `memory_save` tool description rewritten with per-category examples: `preference` (long-term likes/dislikes/working style), `fact` (biographical/contextual facts about Callum), `person` (someone Callum mentions). Explicit "do NOT save closed-loop noise" instruction.
- Reaction handlers (`POST /reaction` and `POST /desktop/reaction`) use `_classify_memory(reaction, message)` in `main.py` — Haiku picks category and writes content. No longer hardcoded to `fact`.
- Heartbeat suppression: `_build_system()` accepts `session_messages` and filters heartbeat `things_to_mention`/`open_loops`/`open_todos`/`current_projects` items whose keywords overlap ≥50% with current session text. Prevents Ki from parroting back what the user just said.
- `recall_past_conversations` tool added to `_STATIC_TOOLS` in `agent.py`: searches Qdrant `conversations` collection (582+ points). Wires up the previously computed but unused semantic conversation index.
- Duplicate dedup: `save_user_message()` and `save_turn()` in `memory.py` each run a 5-second window check on (content, channel) before INSERT. Eliminates "hey/hey" double-saves from race conditions.
- Cross-channel continuity: `get_recent_other_channel(current_channel, hours=2.0)` in `memory.py`; when session gap fires, `respond()` fetches other-channel messages and injects them as a labelled system block. Desktop stays aware of recent WhatsApp context.
- Reminder near-match: `set_reminder` checks active reminders within ±2h with ≥50% word overlap — auto-cancels duplicates before inserting. Prevents reminders stacking on each refinement.

**Daily note writes:**
- `append_to_note` for daily notes inserts content into the `## Daily notes` section — finds that heading and inserts before the next `---` or `##` heading that closes it
- Falls back to inserting before the last `---` if no `## Daily notes` heading exists
- Ki is instructed to format daily note content as `- bullet` with tab-indented sub-bullets; no headers

## Commands

Use the `ki` CLI from your local machine (add `~/DEVELOPMENT/ki/bin` to PATH, set `KI_HOST` or `.ki-host`):

```bash
ki deploy             # git pull --rebase + docker compose up --build -d on the VPS
ki up [service]       # build and start (all or one service)
ki down               # stop everything
ki logs [service]     # tail logs
ki ps                 # service status
ki heartbeat          # trigger heartbeat immediately
ki integrations       # list loaded integrations with tools and schedules
ki memories [cat]     # list memories (preference/fact/person)
ki state              # latest heartbeat state JSON
ki usage              # token usage last 30 days
ki db '<sql>'         # raw psql query
ki ssh                # shell on the VPS
```

Direct docker commands (on the VPS):
```bash
docker compose up --build -d       # full rebuild and start
docker compose logs -f api         # tail api logs
docker compose down                # stop everything
```

## Deployment

All code lives in this repo. The VPS runs the stack by cloning/pulling this repo and running Docker Compose.

**Typical deploy:**
```bash
# Locally
git push

# On VPS (or just: ki deploy)
git pull --rebase && docker compose up --build -d
```

Use `--rebase` — Ki commits directly to the VPS repo when writing integrations/prompts, so plain `git pull` will fail with "divergent branches" if Ki has committed since your last push.

**First-time server setup (VPS, `root@<your-server-ip>`):**
```bash
git clone git@github.com:<your-username>/ki.git /opt/ki
cd /opt/ki
cp .env.example .env   # then fill in secrets
chmod 600 .env
# clone vault separately — NOT inside the ki repo
GIT_SSH_COMMAND="ssh -i /root/.ssh/ki_vault_deploy_key" git clone git@github.com:<your-username>/ki-vault.git /opt/vault
docker compose up --build -d

# Add to platform Caddy (/opt/platform/Caddyfile):
#   ki.example.com {
#       reverse_proxy ki-api-1:8000
#   }
# Then reload: docker compose -f /opt/platform/docker-compose.yml exec router caddy reload --config /etc/caddy/Caddyfile
```

The `.env` file is never committed — it is created directly on the VPS. The `baileys/auth_info/` directory (WhatsApp session) and `data/` (Postgres/Qdrant volumes) are also gitignored and live only on the VPS.

## Key Paths (VPS — `root@<your-server-ip>`)

- `/opt/ki` — this repo, cloned on the VPS
- `/opt/vault` — `<your-username>/ki-vault` Obsidian vault, cloned separately; mounted into containers at `/vault`. **Must be at `/opt/vault`, not inside `/opt/ki`** — if accidentally cloned inside the ki repo, move it: `mv /opt/ki/vault /opt/vault`
- `/root/.ssh/ki_vault_deploy_key` — SSH deploy key for vault repo; generated once on the VPS, never committed
- `/opt/backups` — nightly Postgres dumps

## Architecture

Services communicating over an internal Docker network (`ki_internal`):

- **`baileys/`** — Node.js WhatsApp gateway (`@whiskeysockets/baileys`). Receives text and image messages, forwards to the FastAPI agent, sends replies back. Filtered to a single allowlisted WhatsApp JID.
- **`api/`** — Python + FastAPI agent brain. Calls Claude (Sonnet for conversations, Haiku for heartbeat/indexing), manages memory, routes tool calls. Source bind-mounted from `./api/src` so Ki-written integration files persist.
- **`coding-agent/`** — Node.js service; receives coding tasks from the api, runs an agentic Anthropic loop with safety-constrained file tools, commits and pushes to GitHub.

Supporting services (all Docker):
- **Postgres** — conversation history, explicit memories, heartbeat state snapshots, token usage
- **Qdrant** — vector embeddings for semantic search over conversations and Obsidian notes
- **APScheduler** (in-process in `api`) — heartbeat every N hours + integration-defined schedules; auto-discovered from `integrations/` at startup
- **Phoenix** — LLM observability UI; traces every Anthropic call and tool use; port 6006 (localhost-only, SSH tunnel to access)

## Git commit rule (HARD RULE — no exceptions)

After writing or modifying ANY files, the coding agent MUST:

1. **Stage all changes:** `git -C /repo add -A`
2. **Commit with a descriptive message:** `git -C /repo commit -m "Ki: <what and why>"`
3. **Push to GitHub:** `git -C /repo push origin main`

This is enforced automatically by `gitCommitAndPush()` in `coding-agent/src/server.js` at the end of every `/code` session — it stages with `git add -A` (catches everything, not just tracked files), builds a commit message from the files written + task summary, rebases before pushing, and returns `git_push_failed: true` in the HTTP response if the push fails so Ki can tell the user. There are no exceptions: every task that touches files must end with a commit on GitHub.

## Safe migration practices (MUST follow every time)

1. **Read all existing migrations first.** Before writing any new migration file, read ALL files in `api/migrations/*.sql` to understand exactly what tables and columns already exist. Never assume a fresh schema.
2. **Never pair `CREATE TABLE IF NOT EXISTS` with a bare `CREATE INDEX` on a new column.** If the table already exists the `CREATE TABLE` is a silent no-op — the subsequent `CREATE INDEX` will then fail because the column was never added. Use `ALTER TABLE` instead (see rule 3).
3. **Add columns with `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`.** Never assume a column is absent just because your migration creates it — the table may already exist from an earlier migration. Always use the `IF NOT EXISTS` guard.
4. **Always use `CREATE INDEX IF NOT EXISTS`.** Never write a bare `CREATE INDEX` — it will error if the index already exists.
5. **Prefer `ALTER TABLE` over `CREATE TABLE` for schema changes.** When adding columns or constraints to a table that was created in an earlier migration, always `ALTER` the existing table. Only use `CREATE TABLE IF NOT EXISTS` for brand-new tables that no prior migration touches.

## Architecture Notes

- `api/migrations/` is COPY'd into the Docker image — migrations run at startup via `run_migrations()` in `db.py`, which splits each `.sql` file by `;` and executes statements individually (asyncpg doesn't support multi-statement `execute` calls).
- Migration runner retries connecting to Postgres with exponential backoff (up to 10 attempts) — `depends_on` only guarantees start order, not readiness.
- WhatsApp JID uses `@lid` format on multi-device, not `@s.whatsapp.net`. The correct JID must be discovered from Baileys logs on first connect.
- `./api/src` is bind-mounted into both `api` and `coding-agent` at runtime; `COPY src/` in the Dockerfile is a standalone-build fallback only.
- `git` must be installed in the api image (it is) — the indexer uses `subprocess` to run `git` against the mounted `/vault`.
- Vault is re-indexed on every api startup if the git HEAD has changed. The heartbeat also calls `reindex()` on each run.
- qdrant-client 1.12+ uses `query_points()` (returns `.points` list), not the old `search()` method.
- `PYTHONUNBUFFERED=1` is set on the api container so `print()` output appears immediately in `docker compose logs`.

## Environment

See `.env.example` for the full list. All vars are active. Key ones: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` (embeddings), `DB_PASSWORD`, `YOUR_WHATSAPP_JID`, `INTERNAL_SECRET`, `HEARTBEAT_HOURS` (default 3), `SESSION_GAP_HOURS` (default 0.5), `HEARTBEAT_QUIET_START` (default 22), `HEARTBEAT_QUIET_END` (default 7).
