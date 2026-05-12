# Build Plan — ki (Personal AI Agent)

## Resolved

- **WhatsApp JID:** `<your-jid>@lid` (WhatsApp uses `@lid` format on multi-device, not `@s.whatsapp.net`)
- **Obsidian vault:** `git@github.com:<your-username>/ki-vault.git`
- **VPS:** 4GB RAM, 50GB disk; runs Docker Compose for other services
- **Deploy path:** `/opt/ki` (this repo), `/opt/vault` (ki-vault clone)
- **Heartbeat:** Proactive messaging enabled
- **Embeddings:** `text-embedding-3-small` via OpenAI API — vector size 1536
- **Vault sync:** Managed as a `vault-sync` container in `docker-compose.yml` — tracked in this repo, not a separate host-level systemd timer

---

## Development & Deployment Workflow

All code is written locally in this repo and deployed by pushing to GitHub then pulling on the VPS.

**Day-to-day:**

```bash
# local
git push

# VPS
cd /opt/ki && git pull --rebase && docker compose up --build -d
```

**First-time VPS setup (once only):**

```bash
git clone git@github.com:<your-username>/ki.git /opt/ki
cd /opt/ki
cp .env.example .env    # fill in secrets
chmod 600 .env
docker compose up --build -d
```

The VPS never has code edited on it directly. The only things that live on the VPS outside of git are:

- `/opt/ki/.env` — secrets, never committed
- `/root/.ssh/ki_vault_deploy_key` — SSH key for vault repo access, generated once
- `baileys/auth_info/` — WhatsApp session state, gitignored Docker volume
- `data/` — Postgres and Qdrant volume data, gitignored

---

## Phase 1 — It Talks Back

**Goal:** WhatsApp message in → Claude reply out. No memory, no tools. Just the pipe.

**Baileys container** (`baileys/`)

- [x] `baileys/Dockerfile` — Node 20 slim image, install `@whiskeysockets/baileys` and `pino`
- [x] `baileys/package.json` — Baileys ^6.7.18, pino ^9.0.0
- [x] `baileys/src/index.js` — connect, allowlist check on `ALLOWED_JID`, forward text to FastAPI `/message`, send reply back
- [x] Mount `baileys_auth` named volume so QR auth persists across restarts
- [x] Gracefully ignore non-text messages — reply "I can only handle text messages for now"

**FastAPI container** (`api/`)

- [x] `api/Dockerfile` — Python 3.12 slim image
- [x] `api/src/main.py` — `POST /message` + `GET /health`
- [x] `api/src/agent.py` — `claude-sonnet-4-6`, minimal system prompt, no tools, no memory
- [x] `api/requirements.txt` — fastapi, uvicorn[standard], anthropic, pydantic

**Docker Compose**

- [x] `docker-compose.yml` at repo root — `baileys` + `api` on `ki_internal` bridge network
- [ ] Confirm `ki_internal` doesn't conflict with existing network names on the VPS

**First-time VPS setup**

- [x] Clone repo: `git clone git@github.com:<your-username>/ki.git /opt/ki`
- [x] Create `/opt/ki/.env`, fill in `ANTHROPIC_API_KEY` and `YOUR_WHATSAPP_JID` (`@lid` format, discovered from logs), `chmod 600 .env`

**Verification**

- [x] `docker compose up --build -d`
- [x] `docker compose logs -f baileys` — scan QR with WhatsApp Account 2 (+<your-international-number>)
- [x] Send a message → reply arrives
- [x] Restart containers → no re-scan needed (`baileys_auth` volume persists)

---

## Phase 2 — It Remembers

**Goal:** Persistent conversation history + explicit memory facts. Agent can recall past conversations and stored preferences.

**Postgres**

- [x] Add `postgres:16-alpine` service to `docker-compose.yml`
- [x] Write `api/migrations/001_initial.sql`
- [x] Write migration runner in `api/src/db.py`
- [x] Add `DATABASE_URL` to `.env.example`
- [x] Add `asyncpg` + `sqlalchemy[asyncio]` to `api/requirements.txt`

**Memory layer** (`api/src/memory.py`)

- [x] `get_recent_conversation(limit: int = 20)`
- [x] `save_turn(user: str, assistant: str)`
- [x] `save_memory(category, content, source)`
- [x] `get_memories(category: str = None)`
- [x] `get_latest_heartbeat_state()` (used Phase 4)
- [x] `save_heartbeat_state(state: dict)` (used Phase 4)
- [x] `log_token_usage(model, input_tokens, output_tokens)`

**Agent update** (`api/src/agent.py`)

- [x] Prepend recent conversation history to every Claude call
- [x] Add `memory_save` as a Claude tool
- [x] Inject stored memories as a bullet list in the system prompt
- [x] Log token usage on every Claude response

**`/usage` endpoint** (`api/src/main.py`)

- [x] `GET /usage` — token counts and estimated cost by model, last 30 days

**Backups — on the VPS directly, not in git**

- [x] `mkdir /opt/backups`
- [x] Crontab: `0 3 * * * docker exec ki-postgres-1 pg_dump -U agent agent | gzip > /opt/backups/db_$(date +\%Y\%m\%d).sql.gz`
- [x] Crontab: `0 4 * * * find /opt/backups -name "db_*.sql.gz" -mtime +14 -delete`

**👍 Reaction-to-close** (March 2026)

- [x] Baileys tracks last 50 sent message IDs → text in a `Map`; `sendReply` stores each ID on send
- [x] `messages.upsert` handler checks for `reactionMessage` before all other filters (reactions can arrive with any `type` and `fromMe` is unreliable — use cache lookup as authoritative check)
- [x] On 👍 reaction to a Ki message: POST to `POST /reaction` with the original message text
- [x] `/reaction` endpoint calls Claude Haiku to distill a one-line topic summary, saves as `fact` memory: `"User marked as done and moved on from: {topic}"`

**Verification**

- [x] Tell it your name → "what's my name?" in a new message → recalls it
- [x] Restart containers → memory persists

---

## Phase 3 — It Knows Your Notes

**Goal:** Agent can semantically search and read/write the `ki-vault` Obsidian vault.

**Vault sync — `vault-sync` container (tracked in this repo)**

Rather than a host-level cron or systemd timer, vault sync runs as a container defined in `docker-compose.yml`. The deploy key is generated once on the VPS (one-time manual step) and mounted in; everything else is in git.

One-time VPS setup (manual, not in git):

- [x] `ssh-keygen -t ed25519 -f /root/.ssh/ki_vault_deploy_key -N "" -C "VPS-ki-vault"`
- [x] Add the public key to `<your-username>/ki-vault` → Settings → Deploy Keys (write access)
- [x] Clone vault: `GIT_SSH_COMMAND="ssh -i /root/.ssh/ki_vault_deploy_key" git clone git@github.com:<your-username>/ki-vault.git /opt/vault`
- [x] `git -C /opt/vault config user.name "ki" && git -C /opt/vault config user.email "ki@local"`
- [x] Add to `/opt/ki/.env`: `OPENAI_API_KEY` and `GITHUB_DEPLOY_KEY_PATH=/root/.ssh/ki_vault_deploy_key`

In this repo:

- [x] Add `vault-sync` service to `docker-compose.yml`
- [x] Mount `/opt/vault` into `api` container at `/vault` (read-write — needed for write tools)
- [x] `OBSIDIAN_VAULT_PATH=/vault` already in `.env.example`

**Mac side (one-time)**

- [x] Install Obsidian Git community plugin in the `ki-vault` vault
- [x] Configure: auto-commit every 1 minute, auto-push enabled
- [x] Verify a test commit appears in `<your-username>/ki-vault` on GitHub

**Embeddings**

- [x] Add `openai` to `api/requirements.txt`
- [x] Add `qdrant/qdrant:latest` service to `docker-compose.yml`
- [x] On `api` startup: create Qdrant collections `conversations` and `obsidian` — vector size **1536** (`text-embedding-3-small`)

**Vault indexer** (`api/src/indexer.py`)

- [x] On startup: compare last indexed commit hash (stored in Postgres `indexer_state` table) to current `git rev-parse HEAD` on `/vault`
- [x] For each changed `.md` file: chunk by `##` heading, embed, upsert into Qdrant `obsidian`
- [x] Store new commit hash after successful run
- [x] Expose `reindex()` callable for the heartbeat to trigger
- [x] **Preprocessing pipeline** (March 2026): skip vault root files and `/templates/`; strip Obsidian syntax (dataview/tasks blocks, wiki links, embeds, bare URLs); flatten nested outliner bullets (sub-bullets appended to parent); chunk daily notes per top-level bullet group; chunk regular notes per top-level bullet line within each heading section; prefix each chunk with note title + heading for embedding context
- [x] **Stable point IDs**: use `hashlib.md5` instead of `hash()` — eliminates duplicate points on container restart
- [x] **Full reindex safety**: wipe and recreate Qdrant `obsidian` collection when no prior commit exists
- [x] **`doc_type` payload field**: `"daily_note"`, `"todo"`, `"note"` — enables filtered search
- [x] **Todo extraction**: open `- [ ]` items in daily notes indexed as separate `"todo"` type points with `date` metadata

**Obsidian tools** (`api/src/tools/obsidian.py`)

- [x] `search_vault(query, limit=5, doc_type=None)` — embed query → Qdrant search, optional doc_type filter, returns date + doc_type in results
- [x] `read_note(filename)` — read from `/vault/`, guard against path traversal
- [x] `append_to_note(filename_or_daily, content, commit_message=None)` — for daily notes, inserts above `---` separator; formats as bullet point
- [x] `write_note(filename, content, overwrite=False)` — pull, create, commit, push; refuse overwrite unless flag set
- [x] Register all four in `tools/__init__.py`

**Agent update**

- [x] Vault search wired into `memory.py` and `agent.py`
- [x] Inject top-5 vault excerpts into system prompt per message
- [x] `save_turn()`: embed each turn and upsert into Qdrant `conversations` collection
- [x] Semantic search available via `search_conversations()` in `memory.py`

**Verification**

- [x] `docker compose ps` — `vault-sync` container is running
- [x] Write a note on Mac → vault indexed on startup → agent finds it via search_vault
- [x] Agent appends to daily note → appears on Mac within ~1 minute
- [x] "What did we talk about when I mentioned Y?" → retrieves correct past turn

---

## Phase 4 — It Has a Heartbeat

**Goal:** Agent maintains a live "current state" snapshot and proactively messages when something is worth flagging.

**Heartbeat scheduler** (`api/src/heartbeat.py`)

- [x] Add `apscheduler[asyncio]` and `httpx` to `api/requirements.txt`
- [x] Start `AsyncIOScheduler` in `main.py` on FastAPI startup; shutdown cleanly on exit
- [x] Schedule `run_heartbeat()` every `HEARTBEAT_HOURS` hours (default 3, env var)
- [x] Heartbeat steps:
  1. Call `reindex()` — re-indexes vault if HEAD has changed since last run
  2. Pull conversations from the last `HEARTBEAT_HOURS` hours via `get_conversations_since()`
  3. Load previous heartbeat state from Postgres
  4. Call Haiku with synthesis prompt → structured JSON: `open_todos`, `open_loops`, `current_projects`, `recent_themes`, `things_to_mention`, `last_updated`
  5. Strip markdown fences from Haiku response before JSON parsing (Haiku wraps output in ` ```json ``` `)
  6. Overwrite `last_updated` with server time (don't trust model timestamp)
  7. Save to `heartbeat_states`
  8. If `things_to_mention` non-empty and 2hr cooldown has passed: POST proactive WA message to Baileys `/send`; save `proactive_sent_at` back to state
- [x] `CAST(:state AS jsonb)` syntax required — `::jsonb` confuses SQLAlchemy's bind param parser

**System prompt integration**

- [x] `_build_system()` accepts `heartbeat_state` param; formats as `## Current context` bullet list (projects, todos, loops, themes)
- [x] `respond()` fetches latest heartbeat state and passes it through
- [x] Section omitted gracefully if no state exists yet

**Proactive messaging**

- [x] Built-in Node `http` server on port 3000 in `baileys/src/index.js`
- [x] `POST /send`: validates `secret === INTERNAL_SECRET`, calls `activeSock.sendMessage()` (module-level socket ref set on connect)
- [x] `INTERNAL_SECRET` in `.env.example`; passed to both `baileys` and `api` containers via `docker-compose.yml`
- [x] `BAILEYS_URL=http://baileys:3000` wired into `api` container

**Manual trigger**

- [x] `POST /heartbeat/trigger` on FastAPI — runs full heartbeat immediately, returns state JSON
- [x] Invoke from VPS: `docker compose exec api python3 -c "import urllib.request; req = urllib.request.Request('http://localhost:8000/heartbeat/trigger', method='POST'); r = urllib.request.urlopen(req); print(r.read().decode())"`

**Verification**

- [x] `/heartbeat/trigger` → row appears in `heartbeat_states`, state JSON returned
- [x] New conversation → system prompt contains `## Current context` with live todos/projects
- [ ] Proactive message arrives on WhatsApp when `things_to_mention` is set

---

## Phase 5 — Browser + Integrations

**Goal:** Agent can browse the web and connect to external data sources.

**Playwright browser tool** (`api/src/tools/browser.py`)

- [x] Add `playwright>=1.40.0` to `api/requirements.txt`
- [x] Add to `api/Dockerfile`: `RUN playwright install chromium --with-deps`
- [x] Add `shm_size: '512m'` to `api` service in `docker-compose.yml`
- [x] `navigate(url)` — singleton headless Chromium; strips noise elements (nav, script, footer, etc); returns up to 4000 chars of body text
- [x] `browser_navigate` tool registered in `agent.py` TOOLS list

**browser-use interactive agent** (`api/src/tools/browser.py`)

- [x] Add `browser-use>=0.12.0` to `api/requirements.txt`
- [x] `browser_act(task)` — LLM-driven browser agent (Sonnet); can fill forms, click buttons, complete multi-step web tasks; vision=True; extend_system_message for CAPTCHA reporting (stops and reports rather than retrying)
- [x] `browser_act` tool registered in `agent.py` TOOLS list; Baileys timeout removed (browser tasks can run long)
- [x] CAPTCHA / bot-gate handling: agent instructed to stop immediately and report clearly if it hits any human-verification wall

**Consequential action gate**

- [x] Add migration `api/migrations/004_pending_actions.sql` — `pending_actions` table with id, action_name, details, payload (jsonb), created_at, expires_at, resolved_at, confirmed
- [x] `api/src/actions.py`: `get_pending_action()`, `save_pending_action()`, `resolve_pending_action()`, `execute_pending_action()` — dispatches on action_name
- [x] `request_confirmation` tool in `agent.py` — Claude stores payload in `pending_actions`; agent instructs user to reply "confirm" or "cancel"
- [x] In `agent.py respond()`: pending action checked first; confirm/cancel words resolve and execute (or abort); expiry: 10 minutes

**Ki identity + system prompt files (built March 2026)**

- [x] `api/prompts/identity.md` — Ki's character: warm, direct, dry Kiwi/Australian wit, intentional swearing, proactive opinions, no AI self-awareness
- [x] `api/prompts/instructions.md` — operational rules extracted from old `_STATIC_SYSTEM` f-string (timezone, WhatsApp format, tool usage, daily note format)
- [x] `agent.py`: `_load_prompt()` loads both files at module init; concatenated into `_STATIC_SYSTEM`
- [x] `api/Dockerfile`: `COPY prompts/ ./prompts/`

**ki CLI (built March 2026)**

- [x] `bin/ki` — bash script; host config via `KI_HOST` env var or `.ki-host` file (gitignored)
- [x] Stack: `up/down/restart/logs/ps [service]`, `deploy` (git pull + up --build)
- [x] Actions: `heartbeat` (triggers via `docker compose exec -T api python3`)
- [x] Data: `memories [category]`, `state`, `usage`, `db '<sql>'`
- [x] Other: `ssh` (opens shell on the VPS)
- [x] API calls use `docker compose exec` — api port is not published to host

**Heartbeat improvements (built March 2026)**

- [x] Quiet hours: skip entire run (no token spend) between `HEARTBEAT_QUIET_START` (22) and `HEARTBEAT_QUIET_END` (7) Auckland time
- [x] Vault todos: scroll all `doc_type=todo` Qdrant points on each run and feed to Haiku — surfacing todos not mentioned in conversation
- [x] Snoozed items: `snoozed` field in state JSON; Haiku populates when conversation shows acknowledgement/deferral; carries forward across runs; excluded from `things_to_mention` until condition met
- [x] 24h snooze window: extended conversation lookback specifically for snooze detection (3h window was too short — acknowledgements fell outside it)
- [x] Proactive message: Haiku writes the WhatsApp message directly in Ki's voice — conversational prose, not a bullet list
- [x] `things_to_mention` capped at 5 items
- [x] `max_tokens=2048` for synthesis (1024 too low with vault todos in prompt)
- [x] Cooldown skip logged explicitly

**Integrations module** (built March 2026)

Established `api/src/integrations/` (separate from `tools/`) as the home for autonomous background modules — scheduled jobs + optional conversation tools. The split: `tools/` = synchronous Claude tool calls; `integrations/` = self-contained modules with their own env vars and schedules. Phase 6 formalises this with auto-discovery.

---

## Phase 6 — Ki Builds Integrations

**Goal:** Ki can write, register, and activate new integrations itself. You describe what you want via WhatsApp; Ki writes the code, commits it, tells you what env vars to add, and you run `ki deploy` to activate.

**Auto-discovery (built March 2026)**

- [x] Standard integration interface: `REQUIRED_ENV_VARS`, `TOOL_SCHEMAS`, `SCHEDULES`, `run_tool`, `get_schedule_fn`
- [x] `api/src/integrations/loader.py` — scans `integrations/` at startup; skips pure library files; skips integrations with missing env vars (logs warning); returns list of loaded modules
- [x] `agent.py` refactored: loads integrations via `loader.py`; builds TOOLS list dynamically (static tools + integration TOOL_SCHEMAS); dispatches unknown tool names to `_INTEGRATION_TOOL_MAP`; `cache_control` applied to the final tool after full list is built
- [x] `main.py` refactored: registers integration SCHEDULES dynamically from loaded modules

**Volume mount (built March 2026)**

- [x] `docker-compose.yml`: `- ./api/src:/app/src` added to `api` service — bind mount overrides the image's `COPY src/` at runtime on the VPS; `COPY` kept in Dockerfile as standalone-build fallback
- [x] Same volume mounted into `coding-agent` container so Ki-written files are visible to both

**coding-agent service (built March 2026)**

Ki's `delegate_coding_task` tool sends work to this service rather than attempting single-shot file writing.

- [x] `coding-agent/Dockerfile` — Node 20 slim, git + python3 for syntax checks, npm install
- [x] `coding-agent/package.json` — `@anthropic-ai/sdk`, `express`
- [x] `coding-agent/src/server.js` — Express server + full agentic loop using Anthropic SDK directly (no Pi SDK dependency); `POST /code { task }` → runs up to 30 tool-use iterations → returns `{ summary, files_written, required_env_vars }`
- [x] Six safety-constrained tools: `ki_read_file`, `ki_list_dir`, `ki_grep`, `ki_write_file`, `ki_edit_file`, `ki_check_syntax` — write restricted to `integrations/` and `tools/`; protected core filenames denylist; no general bash
- [x] Git commit runs after successful session: `git -C /repo add api/src/... && git commit -m "Ki: add ..."`
- [x] `REQUIRED_ENV_VARS` extracted from written files by regex and returned to Ki
- [x] `docker-compose.yml`: `coding-agent` service added; `CODING_AGENT_URL=http://coding-agent:3002` wired into `api`
- [x] `agent.py`: `delegate_coding_task` tool added to static TOOLS; dispatches to coding-agent via httpx with 300s timeout; Ki instructed to report env vars and `ki deploy` reminder

**ki CLI**

- [x] `ki integrations` — lists loaded integrations (hits a `/integrations` endpoint or queries directly)

**Verification**

- [x] Say to Ki: "Add an integration that does X" → coding-agent reads an existing integration for reference → writes new file → commits → Ki tells you env vars needed
- [x] Add env vars to `.env` on the VPS, run `ki deploy` → new integration loads; tools and schedules are active (`time_tools` integration confirmed live March 2026)
- [x] Rollback path: `git revert HEAD` on the VPS → `ki deploy`

---

## Cross-Cutting Concerns

Implement as you go.

**Observability — Phoenix (built March 2026)**

- [x] `arizephoenix/phoenix:latest` as a Docker service (`ki_phoenix` volume, port 6006 localhost-only)
- [x] `openinference-instrumentation-anthropic` + `arize-phoenix-otel` in `api/requirements.txt`
- [x] Instrumentation initialised in `main.py` (wrapped in `try/except`); traces appear in Phoenix "ki" project
- [x] Every LLM call and tool use traced: messages, token counts, latency, tool inputs/outputs
- [x] Access via SSH tunnel: `ssh -L 6006:localhost:6006 root@<VPS>` → `http://localhost:6006`

**Session gap (built March 2026)**

- [x] `SESSION_GAP_HOURS` env var (default 0.5, i.e. 30 minutes) — conversation history cleared across inactivity gaps; accepts float for sub-hour values
- [x] Implemented in `get_recent_conversation(session_gap_hours: float)`: single SQL query gates on `MAX(created_at) > now() - gap`
- [x] Heartbeat state provides carry-over context between sessions
- [x] Session note: if gap fires but last message was within 4 hours, Ki is told about the gap so it can acknowledge without resuming tasks unprompted
- [x] `minutes_since_last_message()` helper in `memory.py`

**Prompt caching (built March 2026)**

- [x] Tools list: `cache_control: {type: ephemeral}` on the last tool entry — caches the full static tools block
- [x] System prompt split into two blocks: static instructions (cached, `_STATIC_SYSTEM` constant) + dynamic context (date, heartbeat, memories, vault excerpts — never cached)
- [x] `_build_system()` returns a list of two dicts; cached block is the same bytes every turn so Anthropic's cache hit rate is high

**Memory: todo category removed (built March 2026)**

- [x] `todo` removed from `memory_save` tool enum — agent no longer stores todos as memories
- [x] `get_memories()` excludes `category = 'todo'` rows from the system prompt query
- [x] Short-term focus items owned by the heartbeat (`open_todos`, `open_loops`); formal todos go to the vault via `append_to_note`
- [x] System prompt instructs Claude to always follow any tool call (including `memory_save`) with a natural conversational response — prevents "Done." dead-ends

**Token usage**

- [x] Every Claude response: insert row into `token_usage` (model, input_tokens, output_tokens)
- [x] `GET /usage` on FastAPI — total tokens and estimated cost by model, last 30 days

**Error handling (built March 2026)**

- [x] Baileys: 5-minute timeout (covers browser tasks), retry x2 with backoff on connection errors, specific fallback messages distinguishing timeout from connection failure
- [x] API: Qdrant down → note injected into Ki's system prompt so she knows vault answers may be incomplete
- [x] Browser: screenshots sent to user on navigate errors and on every browser_act completion; all tool failures surface exact error message — Ki instructed never to say "Done" on failure

**Security updates on the VPS (one-time, manual)**

- [x] `apt install unattended-upgrades && dpkg-reconfigure --priority=low unattended-upgrades`

---

**Image support (built March 2026)**

- [x] Baileys: detect `imageMessage`, download via `downloadMediaMessage`, base64-encode, forward to `/message` as `{ data, media_type }` alongside caption
- [x] `InboundMessage`: optional `image: dict | None` field
- [x] `agent.respond()`: if image present, build `[{type: image, source: {type: base64, ...}}, {type: text}]` vision content block; fallback caption "What's in this image?"

**`ki integrations` CLI + `/integrations` endpoint (built March 2026)**

- [x] `GET /integrations` — returns name, tools list, schedules list for each loaded integration
- [x] `ki integrations` — hits endpoint via docker exec, pretty-prints JSON

---

## Phase 7 — Broader Self-Modification

**Goal:** Ki can edit her own prompts and core Python files, with a confirmation gate before touching core code.

**coding-agent extended (built March 2026)**

- [x] `coding-agent/src/server.js`: added `PROMPTS_ROOT = "/ki/prompts"` alongside `SRC_ROOT`
- [x] `resolveAbs()`: routes `prompts/` paths to PROMPTS_ROOT, all others to SRC_ROOT
- [x] `checkWriteAllowed()`: `prompts/`, `integrations/`, `tools/` always writable; core Python files require `scope === "core"`; `WRITE_DENYLIST = ["__init__.py", "loader.py"]` (never writable)
- [x] `toGitPath()`: maps `prompts/x` → `api/prompts/x`, others → `api/src/x`
- [x] `/code` endpoint accepts `scope: "integration" | "core"` parameter
- [x] System prompt updated to describe `prompts/` directory, both scopes, and workflow
- [x] `docker-compose.yml`: added `./api/prompts:/ki/prompts` volume mount to `coding-agent`

**Ki tools + instructions (built March 2026)**

- [x] `agent.py`: `delegate_coding_task` tool description and schema updated — added `scope` field (`"integration"` | `"core"`); description explains when to use each and the confirmation requirement
- [x] `agent.py`: `delegate_coding_task` dispatch passes `scope` in the httpx POST body
- [x] `api/prompts/instructions.md`: added `## Self-modification` section — explains which files need confirmation, which don't, and the post-deploy reminder

**Scope model:**

- `"integration"` (default): write to `integrations/`, `tools/`, `prompts/`, `baileys/`, `migrations/`, `requirements.txt` — no confirmation needed
- `"core"`: also allows `agent.py`, `main.py`, `heartbeat.py`, `memory.py`, `db.py`, `actions.py`, etc. — Ki MUST call `request_confirmation` first
- `WRITE_DENYLIST`: `__init__.py`, `loader.py` — never writable in any scope

**Extended write scope (built March 2026):**

- [x] `baileys/src/index.js` — coding-agent can edit the WhatsApp gateway; `./baileys/src:/ki/baileys` mounted
- [x] `api/requirements.txt` — coding-agent can add Python packages; `./api/requirements.txt:/ki/requirements.txt` mounted; changes take effect on `ki deploy`
- [x] `api/migrations/*.sql` — coding-agent can write new migrations; `./api/migrations:/ki/migrations` mounted; migrations run at API startup in filename order; next number: 005

---

---

## Ki-authored integrations (built via coding-agent, March 2026)

- [x] `time_tools` — `get_current_time`; no env vars
- [x] `reminders` + `reminder_checker` — `set_reminder`, `list_reminders`, `cancel_reminder`; `reminder_checker` runs every minute to send due reminders via Baileys; stored in `reminders` table
- [x] `web_search` — Brave Search API; requires `BRAVE_SEARCH_API_KEY`
- [x] `image_gen` — xAI Aurora (Grok API) image generation; sends directly to WhatsApp/desktop via Baileys `/send-image`; requires `XAI_API_KEY`; model overridable via admin `settings` table

---

## Desktop app + Admin UI (built March 2026)

**Goal:** Full desktop interface with streaming responses, private mode, and admin controls.

- [x] `POST /desktop/stream` — SSE streaming for the desktop client; tokens streamed in real time; conversation saved on completion; separate `channel: "desktop"` history
- [x] `POST /desktop/bridge` — summarise recent WhatsApp conversation and inject handoff note into desktop channel (Haiku-written)
- [x] `POST /desktop/reaction` — 👍/❤️/✕ reactions; `_classify_memory()` has Haiku pick category and write content
- [x] `GET /desktop/media` — media grid across all channels
- [x] `GET /admin/schedule-runs` — scheduler job history
- [x] `GET /admin/state`, `GET /admin/states` — heartbeat state snapshots
- [x] `GET /admin/memories` — all memories
- [x] `GET /admin/vault-status` — vault commit + Qdrant point count
- [x] `GET/PUT /admin/settings` — runtime config (`image_gen_model`)
- [x] `GET /admin/session`, `POST /admin/session/reset` — session inspection and manual reset
- [x] `GET /admin/prompts`, `PUT /admin/prompts/{name}` — edit `identity`/`instructions` in DB; no redeploy needed

**Prompts in DB:**
- [x] `_load_prompt(filename)` checks DB (`settings` table, key `prompt:{name}`) first, falls back to file on disk; DB value overrides immediately without redeploy
- [x] `settings` table used for runtime config: `image_gen_model`, `session_reset_before`, `prompt:*` keys

---

---

## April 2026 — Memory & System Quality Improvements

**Memory quality:**
- [x] `memory_save` tool description rewritten: per-category concrete examples (`preference`, `fact`, `person`); explicit "do NOT save closed-loop noise" instruction
- [x] Reaction handlers (`POST /reaction` + `POST /desktop/reaction`) use `_classify_memory(reaction, message)` — Haiku picks category and writes content; previously hardcoded to `fact`

**Recall:**
- [x] `recall_past_conversations` tool added — searches Qdrant `conversations` collection (previously computed but never exposed as a tool); Ki can now look up past exchanges when the user references something discussed before

**Heartbeat suppression:**
- [x] `_build_system()` filters heartbeat items whose keywords overlap ≥50% with current session messages — prevents Ki from parroting back what the user just said in the same session

**Data integrity:**
- [x] Duplicate dedup: `save_user_message()` and `save_turn()` check (content, channel) within a 5-second window before INSERT — eliminates double-save race conditions
- [x] Reminder near-match: `set_reminder` auto-cancels existing active reminders within ±2h with ≥50% word overlap — prevents reminders stacking on each refinement

**Cross-channel continuity:**
- [x] `get_recent_other_channel()` in `memory.py`; when session gap fires, `respond()` injects recent other-channel messages as a labelled system block — desktop stays aware of recent WhatsApp context

**Bug fix:**
- [x] Latent `await` bug: `respond()` (WhatsApp path) was calling `_build_system()` without `await`, so Ki on WhatsApp received a coroutine object instead of the actual system prompt. Fixed.

**Heartbeat step-change (April 2026):**
- [x] Synthesis model upgraded Haiku → Sonnet — better open_loop detection, smarter todo triage, less hallucination on snooze/resurface logic
- [x] Vault todos deduplicated by content before synthesis — fixes 3× duplicates causing noise in both synthesis input and `ki_commitments` snapshot
- [x] `backlog_todos` tier: items 14+ days old with no recent activity graduated there; `open_todos` capped at 12. Stale items block gives model explicit signal for graduation
- [x] Date-aware `ki_commitments`: commitments formatted `YYYY-MM-DD: description` are code-injected into `things_to_mention` on their due date — not relying on the model to notice the date
- [x] `_build_system`: removed `[stale]`/`[aging]` prefix hack; `backlog_todos` shown as compact "Backlog (N): ..." section (6 + overflow); `current_projects` capped at 8; `recent_themes` capped at 5

**Vault score threshold + staleness pruning (April 2026):**
- [x] Auto-injected vault excerpts filtered to score ≥ 0.35 — below-threshold results logged but not sent to Claude
- [x] `current_projects` tracked in `item_metadata`; loops stale 21+ days get explicit DROP instruction in synthesis; projects stale 21+ days get move-to-themes-or-drop instruction

**memory_save location guidance (April 2026):**
- [x] `memory_save` tool description updated: `fact` category now includes location/movement guidance (e.g. if Callum mentions moving cities, save as fact)

---

## Deferred

- **Caddy** — only when an external service needs to push webhooks into the VPS.
- **Health integration** — define when API target is known.
- **Multi-step confirmation** — current gate is single-step ("confirm"/"cancel"). Richer flows need more state design; defer until simple version is proven.
