# Personal AI Agent — Architecture & Build Runbook

> A self-hosted, private AI assistant on your existing DigitalOcean VPS. Accessible via WhatsApp, with persistent memory, Obsidian integration, browser access, and a proactive heartbeat.

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Tech Stack](#tech-stack)
3. [Project Structure](#project-structure)
4. [Docker Setup](#docker-setup)
5. [Service by Service](#service-by-service)
6. [Obsidian Sync Strategy](#obsidian-sync-strategy)
7. [Memory Design](#memory-design)
8. [Heartbeat Design](#heartbeat-design)
9. [Tool / Integration Framework](#tool--integration-framework)
10. [Security Baseline](#security-baseline)
11. [Build Order](#build-order)
12. [Cost Estimate](#cost-estimate)
13. [Future: Adding Caddy](#future-adding-caddy)
14. [Things to Decide Before You Start](#things-to-decide-before-you-start)

---

## Architecture Overview

```
Your Phone (WA Account 2)
         │  WhatsApp Web protocol
         ▼
 ┌─────────────┐
 │   Baileys   │  Node.js — WA gateway
 │  (sidecar)  │
 └──────┬──────┘
        │  HTTP (internal network)
        ▼
 ┌─────────────┐     ┌──────────────┐
 │   FastAPI   │◄───►│   Postgres   │  conversation history
 │   (agent)   │     │              │  structured memory
 └──────┬──────┘     └──────────────┘
        │
 ┌──────┴──────────────────────────────┐
 │           Tool Router               │
 ├──────────┬──────────────┬───────────┤
 │          │              │           │
 ▼          ▼              ▼           ▼
┌────────┐ ┌──────────┐ ┌──────────┐ ┌──────────────┐
│Qdrant  │ │Playwright│ │Integrat- │ │coding-agent  │
│(vector │ │(browser) │ │ions      │ │Node.js — Ki  │
│memory) │ └──────────┘ │(calendar,│ │writes its own│
└────────┘              │time, ...)│ │integrations  │
                        └──────────┘ └──────────────┘
        │
        ▼
 ┌─────────────┐
 │  Scheduler  │  Heartbeat every N hours + integration schedules
 └─────────────┘
```

**Key principle:** Baileys handles only WhatsApp protocol. Your agent brain stays in Python. They communicate via HTTP on an internal Docker network — nothing is exposed to the internet.

---

## Tech Stack

| Layer | Choice | Reason |
|---|---|---|
| WhatsApp | Baileys (`@whiskeysockets/baileys`) | Free, QR-based, no Twilio costs |
| Agent brain | Python + FastAPI | Async, huge ecosystem |
| LLM | Claude API (Sonnet for main + heartbeat, Haiku for reactions/bridge) | Best tool use, multimodal |
| Vector memory | Qdrant | Docker-native, excellent Python SDK |
| Structured memory | PostgreSQL | Conversation history, facts, todos |
| Browser | Playwright (Python) | More reliable than Puppeteer for long-running agents |
| Scheduler | APScheduler (in-process) | Simple, no extra container needed |
| Obsidian sync | Git (vault as bare repo) | Reliable, version-controlled |
| Secrets | `.env` file, `chmod 600` | Simple for single-person use |
| Web gateway | None for now, Caddy later if needed | See §Future |

---

## Project Structure

```
ki/
├── docker-compose.yml
├── .env                    # chmod 600 — never commit
├── .env.example            # commit this
├── .ki-host                # VPS IP for ki CLI — gitignored
│
├── bin/
│   └── ki                  # CLI for managing the stack locally
│
├── baileys/
│   ├── Dockerfile
│   ├── package.json
│   ├── src/
│   │   └── index.js        # WA gateway + internal /send server
│   └── auth_info/          # volume-mounted, gitignored
│
├── coding-agent/
│   ├── Dockerfile           # Node 20, git, python3 for syntax checks
│   ├── package.json         # @anthropic-ai/sdk, express
│   └── src/
│       └── server.js        # POST /code → agentic loop → commit + push
│
├── api/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── prompts/
│   │   ├── identity.md     # Ki's character — edit to tune personality
│   │   └── instructions.md # Operational rules (timezone, tools, format)
│   ├── migrations/         # SQL migrations, run on startup
│   └── src/                # bind-mounted at runtime: ./api/src:/app/src
│       ├── main.py         # FastAPI entrypoint + lifespan
│       ├── agent.py        # Orchestration, tool routing, system prompt
│       ├── memory.py       # Postgres + Qdrant memory layer
│       ├── actions.py      # Pending action gate (confirm/cancel)
│       ├── embeddings.py   # OpenAI text-embedding-3-small
│       ├── vector_store.py # Qdrant client
│       ├── indexer.py      # Vault indexer (git diff → embed → upsert)
│       ├── heartbeat.py    # Scheduled state synthesis
│       ├── db.py           # SQLAlchemy async session + migration runner
│       ├── tools/
│       │   ├── browser.py      # Playwright (navigate) + browser-use (act)
│       │   └── obsidian.py     # search_vault, read_note, append, write
│       └── integrations/
│           ├── loader.py       # Auto-discovery: scans for standard interface
│           └── *.py            # Built-in and Ki-authored integrations
│
└── data/                   # Docker volumes — gitignored
    ├── postgres/
    └── qdrant/
```

---

## Docker Setup

### `docker-compose.yml`

Add this to your existing VPS's compose file, or use `include` if you keep services in separate files.

```yaml
networks:
  agent_internal:
    driver: bridge
    # reference your existing network here if services need to share it
    # existing_network:
    #   external: true

volumes:
  baileys_auth:
  agent_postgres:
  agent_qdrant:
  obsidian_vault:

services:

  # ── WhatsApp Gateway ──────────────────────────────────────
  baileys:
    build: ./baileys
    restart: unless-stopped
    volumes:
      - baileys_auth:/app/auth_info
    environment:
      - AGENT_API_URL=http://api:8000
      - ALLOWED_JID=${YOUR_WHATSAPP_JID}  # your WA number, e.g. 447700900000@s.whatsapp.net
    networks:
      - agent_internal
    mem_limit: 256m

  # ── Agent Brain ───────────────────────────────────────────
  api:
    build: ./api
    restart: unless-stopped
    volumes:
      - obsidian_vault:/vault:ro          # read-only mount
    environment:
      - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
      - DATABASE_URL=postgresql://agent:${DB_PASSWORD}@postgres:5432/agent
      - QDRANT_HOST=qdrant
      - QDRANT_PORT=6333
    depends_on:
      - postgres
      - qdrant
    networks:
      - agent_internal
    mem_limit: 512m

  # ── Postgres ──────────────────────────────────────────────
  postgres:
    image: postgres:16-alpine
    restart: unless-stopped
    volumes:
      - agent_postgres:/var/lib/postgresql/data
    environment:
      - POSTGRES_USER=agent
      - POSTGRES_PASSWORD=${DB_PASSWORD}
      - POSTGRES_DB=agent
    networks:
      - agent_internal
    mem_limit: 256m

  # ── Qdrant (vector memory) ────────────────────────────────
  qdrant:
    image: qdrant/qdrant:latest
    restart: unless-stopped
    volumes:
      - agent_qdrant:/qdrant/storage
    networks:
      - agent_internal
    mem_limit: 512m

  # ── Playwright (headless browser) ─────────────────────────
  # Run on-demand via internal API call from agent, not always-on
  # See browser.py — this starts/stops as needed
  # If you want always-on, add it here with shm_size: '512m'
```

### `.env.example`

```
ANTHROPIC_API_KEY=
DB_PASSWORD=
YOUR_WHATSAPP_JID=         # e.g. 447700900000@s.whatsapp.net
OBSIDIAN_VAULT_PATH=/vault
```

---

## Service by Service

### Baileys (WhatsApp Gateway)

```js
// baileys/src/index.js
const { makeWASocket, useMultiFileAuthState, DisconnectReason } = require('@whiskeysockets/baileys')
const fetch = require('node-fetch')

async function start() {
  const { state, saveCreds } = await useMultiFileAuthState('./auth_info')
  const sock = makeWASocket({ auth: state, printQRInTerminal: true })

  sock.ev.on('creds.update', saveCreds)

  sock.ev.on('connection.update', ({ connection, lastDisconnect }) => {
    if (connection === 'close') {
      const shouldReconnect = lastDisconnect?.error?.output?.statusCode !== DisconnectReason.loggedOut
      if (shouldReconnect) start()
    }
  })

  sock.ev.on('messages.upsert', async ({ messages, type }) => {
    if (type !== 'notify') return

    for (const msg of messages) {
      // Only respond to your own number
      if (msg.key.remoteJid !== process.env.ALLOWED_JID) return
      if (msg.key.fromMe) return

      const text = msg.message?.conversation || msg.message?.extendedTextMessage?.text
      if (!text) return

      try {
        const res = await fetch(`${process.env.AGENT_API_URL}/message`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text, jid: msg.key.remoteJid })
        })
        const { reply } = await res.json()
        await sock.sendMessage(msg.key.remoteJid, { text: reply })
      } catch (err) {
        console.error('Agent call failed:', err)
      }
    }
  })
}

start()
```

**First run:** `docker compose logs -f baileys` — scan the QR code with WhatsApp Account 2. Auth is saved to the volume. You won't need to scan again unless you log out.

---

### FastAPI Agent

```python
# api/src/main.py
from fastapi import FastAPI
from pydantic import BaseModel
from .agent import Agent

app = FastAPI()
agent = Agent()

class Message(BaseModel):
    text: str
    jid: str

@app.post("/message")
async def handle_message(msg: Message):
    reply = await agent.respond(msg.text, msg.jid)
    return {"reply": reply}
```

```python
# api/src/agent.py — simplified orchestration shape
import anthropic
from .memory import MemoryLayer
from .tools import get_tool_definitions, run_tool

client = anthropic.AsyncAnthropic()

class Agent:
    def __init__(self):
        self.memory = MemoryLayer()

    async def respond(self, user_message: str, jid: str) -> str:
        # 1. Retrieve relevant context
        recent_history = await self.memory.get_recent_conversation(limit=20)
        relevant_memories = await self.memory.search(user_message, limit=5)
        relevant_notes = await self.memory.search_obsidian(user_message, limit=3)

        # 2. Build system prompt
        system = self._build_system(relevant_memories, relevant_notes)

        # 3. Build messages
        messages = recent_history + [{"role": "user", "content": user_message}]

        # 4. Call Claude with tools
        response = await client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1024,
            system=system,
            tools=get_tool_definitions(),
            messages=messages
        )

        # 5. Handle tool use loop
        while response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    result = await run_tool(block.name, block.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result
                    })
            messages += [
                {"role": "assistant", "content": response.content},
                {"role": "user", "content": tool_results}
            ]
            response = await client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=1024,
                system=system,
                tools=get_tool_definitions(),
                messages=messages
            )

        # 6. Extract reply text
        reply = next(b.text for b in response.content if hasattr(b, 'text'))

        # 7. Persist conversation
        await self.memory.save_turn(user_message, reply)

        return reply

    def _build_system(self, memories, notes) -> str:
        return f"""You are a personal AI assistant. You know this person well.

## Your memories about them:
{memories}

## Relevant notes from their Obsidian vault:
{notes}

Be concise — you're responding via WhatsApp. Use plain text, no markdown.
For consequential actions (posting, submitting forms, moving money),
always confirm before executing."""
```

---

## Obsidian Sync Strategy

**Setup:** Vault is a local Git repo on your Mac, synced to a private GitHub repo via the Obsidian Git plugin (1-minute auto-commit and push). The VPS pulls every 1 minute. At worst the agent's vault is 2 minutes stale — fine for any realistic use case.

### VPS setup

```bash
# Generate a read/write deploy key (do not use your main GitHub SSH key)
ssh-keygen -t ed25519 -f ~/.ssh/obsidian_deploy_key -N "" -C "VPS-obsidian"
cat ~/.ssh/obsidian_deploy_key.pub
# Add the public key to: GitHub repo → Settings → Deploy Keys
# Leave "read-only" UNCHECKED — the agent needs push access

# Clone the vault using the deploy key
GIT_SSH_COMMAND="ssh -i ~/.ssh/obsidian_deploy_key" \
  git clone git@github.com:you/obsidian-vault.git /path/to/obsidian_vault

# Set commit author for agent-authored changes
git -C /path/to/obsidian_vault config user.name "Agent"
git -C /path/to/obsidian_vault config user.email "agent@local"
```

Add to `.env`:

```
GITHUB_DEPLOY_KEY_PATH=/root/.ssh/obsidian_deploy_key
OBSIDIAN_VAULT_PATH=/path/to/obsidian_vault
```

The deploy key is still scoped to this one repo. Write access is limited to what the agent tools explicitly implement — there's no shell access or arbitrary file writes outside the vault.

**Obsidian indexing:** On startup, and when the heartbeat detects vault changes, the agent re-indexes modified `.md` files into Qdrant as vector embeddings. Compare Git commit hashes between runs to identify changed files efficiently.

**Preprocessing pipeline** — run before embedding to maximise retrieval quality:
- Skip vault root `.md` files (Obsidian index pages) and `/templates/`
- Strip Obsidian-specific syntax: `dataview`/`tasks` code blocks, `![[embeds]]`, `[[wiki links]]` → plain text, bare URLs
- Flatten nested outliner-style bullets: each top-level bullet + all its tab-indented children → one prose sentence (joined with `. `), so sub-bullets carry parent context into the embedding
- Daily notes (`daily/YYYY-MM-DD.md`): extract content above the final `---` separator; chunk one point per top-level bullet group; extract open `- [ ]` todos as separate `doc_type: "todo"` points with `date` metadata
- Regular notes: chunk one point per top-level bullet line within each `##` heading section; prefix each chunk with `"{note title} — {heading}: "` for context
- Use `hashlib.md5` for stable point IDs — `hash()` is process-randomised and causes duplicates on restart

```bash
# 1-minute cron pull to match the Mac push schedule (crontab -e)
* * * * * GIT_SSH_COMMAND="ssh -i ~/.ssh/obsidian_deploy_key" git -C /path/to/obsidian_vault pull --quiet
```

```python
# tools/obsidian.py
import os, subprocess
from pathlib import Path
from datetime import date

VAULT_PATH = Path(os.environ["OBSIDIAN_VAULT_PATH"])
DEPLOY_KEY = os.environ["GITHUB_DEPLOY_KEY_PATH"]
GIT_ENV = {"GIT_SSH_COMMAND": f"ssh -i {DEPLOY_KEY}"}

async def search_vault(query: str, limit: int = 5) -> str:
    """Semantic search across Obsidian notes."""
    # Query Qdrant collection 'obsidian' with embedded query
    # Return top-k note excerpts with filenames
    ...

async def read_note(filename: str) -> str:
    """Read a specific note by filename."""
    path = VAULT_PATH / filename
    if path.exists() and path.suffix == '.md':
        return path.read_text()
    return "Note not found."

async def get_daily_note_path() -> Path:
    """Return today's daily note path, creating it if it doesn't exist."""
    today = date.today().strftime("%Y-%m-%d")
    path = VAULT_PATH / f"{today}.md"
    if not path.exists():
        path.write_text(f"# {today}\n\n")
    return path

async def append_to_note(filename: str, content: str, commit_message: str = None) -> str:
    """Append content to a note and push to GitHub."""
    if filename == "daily":
        path = await get_daily_note_path()
    else:
        path = VAULT_PATH / filename
        if not path.exists():
            return f"Note not found: {filename}"

    # Pull latest before writing to avoid conflicts
    subprocess.run(["git", "-C", str(VAULT_PATH), "pull", "--quiet"],
                   env={**os.environ, **GIT_ENV})

    # Append content (ensure clean separation)
    with path.open("a") as f:
        f.write(f"\n{content}\n")

    # Commit and push
    msg = commit_message or f"Agent: append to {path.name}"
    subprocess.run(["git", "-C", str(VAULT_PATH), "add", str(path)])
    subprocess.run(["git", "-C", str(VAULT_PATH), "commit", "-m", msg])
    subprocess.run(["git", "-C", str(VAULT_PATH), "push"],
                   env={**os.environ, **GIT_ENV})

    return f"Written and pushed to {path.name}. Will appear on your Mac within ~1 minute."

async def write_note(filename: str, content: str, overwrite: bool = False) -> str:
    """Create a new note. Refuses to overwrite unless explicitly confirmed."""
    path = VAULT_PATH / filename
    if not filename.endswith(".md"):
        path = VAULT_PATH / f"{filename}.md"

    if path.exists() and not overwrite:
        return f"Note {path.name} already exists. Use append_to_note, or confirm overwrite."

    subprocess.run(["git", "-C", str(VAULT_PATH), "pull", "--quiet"],
                   env={**os.environ, **GIT_ENV})

    path.write_text(content)

    subprocess.run(["git", "-C", str(VAULT_PATH), "add", str(path)])
    subprocess.run(["git", "-C", str(VAULT_PATH), "commit", "-m", f"Agent: create {path.name}"])
    subprocess.run(["git", "-C", str(VAULT_PATH), "push"],
                   env={**os.environ, **GIT_ENV})

    return f"Created {path.name} and pushed. Will appear on your Mac within ~1 minute."
```

---

## Memory Design

Two complementary layers:

### Postgres — structured memory

```sql
-- Conversation history
CREATE TABLE conversations (
    id         SERIAL PRIMARY KEY,
    role       TEXT NOT NULL,           -- 'user' | 'assistant'
    content    TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- Explicit facts / preferences
CREATE TABLE memories (
    id         SERIAL PRIMARY KEY,
    category   TEXT,                    -- 'preference', 'fact', 'todo', 'person'
    content    TEXT NOT NULL,
    source     TEXT,                    -- 'conversation' | 'heartbeat' | 'manual'
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- Heartbeat snapshots
CREATE TABLE heartbeat_states (
    id         SERIAL PRIMARY KEY,
    state      JSONB NOT NULL,          -- structured "current state" object
    created_at TIMESTAMPTZ DEFAULT now()
);
```

### Qdrant — semantic/episodic memory

Two collections:

- `conversations` — embeddings of conversation chunks, for "find things we talked about related to X"
- `obsidian` — embeddings of vault notes, chunked per bullet group with `doc_type` (`"note"`, `"daily_note"`, `"todo"`) and `date` metadata; supports filtered search

**Closing topics with reactions:** Reacting to any Ki message (👍 done, ❤️ appreciated, ✕ dismissed on desktop) calls `_classify_memory(reaction, message)` — Haiku picks the memory category (`preference`, `fact`, or `person`) and writes appropriate content. Baileys tracks sent message IDs; on 👍 reaction the original message text is POSTed to `/reaction`. Claude reads these memories in future conversations and stops re-raising resolved topics.

**Recall past conversations:** `recall_past_conversations` tool searches the Qdrant `conversations` collection — every conversation turn is embedded and stored there. Ki uses this when the user references something discussed before ("remember when we talked about X?").

---

## Heartbeat Design

Runs every `HEARTBEAT_HOURS` hours (default 3) via APScheduler in-process. Skipped entirely during quiet hours (`HEARTBEAT_QUIET_START=22` to `HEARTBEAT_QUIET_END=7`, Auckland time) — no synthesis, no tokens, no messages.

**What it does each run:**
1. Checks quiet hours — returns early if outside active window
2. Calls `reindex()` — re-indexes vault if HEAD has changed
3. Fetches conversations from the last `HEARTBEAT_HOURS` hours (for activity/themes)
4. Fetches conversations from the last 24 hours (for snooze detection)
5. Scrolls all `doc_type=todo` Qdrant points from the obsidian collection; deduplicates by content before synthesis
6. Loads previous heartbeat state from Postgres
7. Calls Sonnet (max_tokens=2048) → structured JSON state (upgraded from Haiku April 2026 — better open_loop detection and snooze logic)
8. Strips markdown fences before `json.loads`
9. Code-injects any `ki_commitments` due today into `things_to_mention` (date-aware, not relying on model to notice the date)
10. Overwrites `last_updated` with server time
11. Saves to `heartbeat_states`
12. If `things_to_mention` non-empty and 2hr cooldown has passed and not quiet hours: sends proactive WhatsApp message

**State JSON schema:**
- `open_todos` — actionable todos (max 12); items 14+ days old with no recent activity are graduated to `backlog_todos` instead
- `backlog_todos` — stale todos kept for reference but excluded from active triage
- `open_loops` — unresolved questions and pending decisions; items 21+ days old get explicit DROP instruction at synthesis
- `current_projects` — what Callum is actively working on (max 8 shown); items 21+ days old get move-to-themes-or-drop instruction
- `recent_themes` — recurring topics (max 5)
- `things_to_mention` — up to 5 items worth proactively raising (never includes snoozed items; date-due `ki_commitments` injected here)
- `ki_commitments` — `YYYY-MM-DD: description` entries; code-injected into `things_to_mention` on their due date
- `snoozed` — `[{item, reason, until}]` — items acknowledged/deferred in conversation; carried forward across runs; 24h window ensures acknowledgements don't fall outside the lookback
- `proactive_message` — the actual WhatsApp message written by Sonnet in Ki's voice (conversational prose, not a bullet list); `null` if nothing to raise
- `last_updated`, `proactive_sent_at`

**System prompt injection:** every conversation's `_build_system()` fetches the latest heartbeat state and renders it as a `## Current context` block. `backlog_todos` shown as compact "Backlog (N): ..." section (6 items + overflow). Omitted gracefully if no state exists yet.

**Vault excerpt injection:** auto-injected vault excerpts are filtered to score ≥ 0.35 — below-threshold results are logged but not sent to Claude.

**Proactive messaging:** Baileys runs a Node `http.createServer` on port 3000. `POST /send` validates `INTERNAL_SECRET` and calls `activeSock.sendMessage()`. The api POSTs to `http://baileys:3000/send`.

**Manual trigger:**
```bash
ki heartbeat
```

**Gotchas:**
- `CAST(:state AS jsonb)` required — `::jsonb` breaks SQLAlchemy's bind param parser
- `INTERNAL_SECRET` and `BAILEYS_URL=http://baileys:3000` must be in `.env` and wired into both containers
- API port is not published to host — use `ki heartbeat` (docker exec) not curl from host

---

## Tool / Integration Framework

Two distinct module types live under `api/src/`:

- **`tools/`** — synchronous Python functions registered as Claude tool calls. Ki invokes these during conversation (browser navigation, Obsidian read/write/search). Stateless, request-scoped.
- **`integrations/`** — autonomous background modules. Each has scheduled jobs (APScheduler) and optionally exposes conversation tools. Finance lives here. So will any future data-source integrations.

### Current tools (`api/src/tools/`)

| Tool | Function |
|---|---|
| `search_vault` | Semantic search over Obsidian notes (Qdrant) |
| `read_note` | Read a specific vault note by path |
| `append_to_note` | Append to existing note or today's daily note |
| `write_note` | Create a new note (refuses overwrite without flag) |
| `browser_navigate` | Headless Chromium fetch, stripped to text |
| `browser_act` | LLM-driven browser agent for multi-step tasks |
| `memory_save` | Persist preferences, facts, people to Postgres (Haiku classifies category for reactions) |
| `recall_past_conversations` | Semantic search over Qdrant `conversations` collection (all past turns) |
| `request_confirmation` | Gate for consequential actions (confirm/cancel) |
| `delegate_coding_task` | Send a coding task to the coding-agent service |

**Image support:** Baileys detects `imageMessage`, downloads bytes via `downloadMediaMessage`, base64-encodes and forwards to the API alongside the caption. `agent.respond()` builds a two-block vision content block so Claude sees the image. Falls back to "What's in this image?" if no caption provided.

**Voice message support:** Baileys detects `audioMessage`, downloads the opus/ogg bytes via `downloadMediaMessage`, and forwards to `/message` as `audio: {data, mime_type}`. `agent.respond()` calls `_transcribe_audio()` → OpenAI Whisper `whisper-1`; transcript becomes the user message. `OPENAI_API_KEY` already present; no new credentials needed.

### Current integrations (`api/src/integrations/`)

| Integration | Scheduled | Tools |
|---|---|---|
| `time_tools` *(Ki-authored)* | None | `get_current_time` |
| `reminders` + `reminder_checker` *(Ki-authored)* | Every minute (delivery check) | `set_reminder`, `list_reminders`, `cancel_reminder` |
| `web_search` *(Ki-authored)* | None | `web_search` (Brave Search API; requires `BRAVE_SEARCH_API_KEY`) |
| `image_gen` *(Ki-authored)* | None | `generate_image` (xAI Aurora/Grok; requires `XAI_API_KEY`) |

### Auto-discovery (live)

`loader.py` scans `integrations/` at startup. Every integration file must expose:

```python
REQUIRED_ENV_VARS: list[str]     # integration skipped if any missing
TOOL_SCHEMAS: list[dict]         # Anthropic tool schemas (can be [])
SCHEDULES: list[dict]            # APScheduler jobs (can be [])

async def run_tool(name: str, input: dict) -> str   # tool dispatcher

# Required if SCHEDULES is non-empty:
def get_schedule_fn(name: str): return _SCHEDULE_FNS[name]
```

SCHEDULES format:
```python
SCHEDULES = [
    {"id": "my_sync", "fn": "run_my_sync", "trigger": "cron", "hour": 2, "minute": 0},
    {"id": "my_pulse", "fn": "run_my_pulse", "trigger": "interval", "days": 3},
]
```

`agent.py` builds the TOOLS list dynamically and dispatches to the correct module. `main.py` registers SCHEDULES jobs dynamically. No manual wiring needed.

### Adding a new integration — via Ki

Ask Ki on WhatsApp: *"Add an integration that does X"*. Ki calls `delegate_coding_task`, which sends the task to the `coding-agent` service. The coding-agent:

1. Pulls latest from origin (rebases) so no divergence
2. Runs an agentic loop: reads existing integrations for patterns, writes the file, runs `py_compile`, fixes errors
3. Commits with `"Ki: add filename.py"` and pushes to GitHub
4. Returns `files_written` and `required_env_vars`

Ki then tells you what env vars to add to `.env` on the VPS and to run `ki deploy`. After deploy, the new integration loads automatically via `loader.py`.

**Rollback:** `git revert HEAD` on the VPS → `ki deploy`.

### Adding a new integration — manually

If you prefer to write it yourself:

1. Create `api/src/integrations/myservice.py` with the standard interface above
2. Add env vars to `.env.example` and `.env` on the VPS
3. Push and `ki deploy` — loader picks it up automatically

No changes to `agent.py` or `main.py` needed.

**Consequential action gate** — live for anything that writes/posts/submits. `request_confirmation` tool stores the pending action in Postgres with a 10-minute expiry; "confirm"/"cancel" in the next message executes or aborts it.

---

## Security Baseline

### VPS firewall (DigitalOcean Cloud Firewall)

```
Inbound:  SSH (22) from your IP only
Outbound: All (needed for Claude API, WA, package updates)
```

No ports 80/443 needed until you add Caddy.

### SSH hardening

```
# /etc/ssh/sshd_config
PasswordAuthentication no
PubkeyAuthentication yes
PermitRootLogin no
```

### WhatsApp allowlist

The Baileys service checks `ALLOWED_JID` — it will not respond to any other WhatsApp number. This is your primary guard.

### Secrets

```bash
# On VPS
chmod 600 ~/agent/.env
# Never commit .env — use .env.example with blank values
```

### Container limits

```yaml
# Already in docker-compose.yml above
mem_limit: 512m  # per service
# Playwright gets 1g + shm_size: '512m' if you add it as always-on
```

### Automatic security updates

```bash
apt install unattended-upgrades
dpkg-reconfigure --priority=low unattended-upgrades
```

### Backups

```bash
# Postgres dump — add to crontab
0 3 * * * docker exec agent_postgres pg_dump -U agent agent | gzip > ~/backups/db_$(date +%Y%m%d).sql.gz

# Keep 14 days
find ~/backups/ -name "db_*.sql.gz" -mtime +14 -delete
```

Obsidian vault is already backed up via Git.

---

## Build Order

### Phase 1 — It talks back (Week 1)

- Create `~/agent/` directory structure on the VPS
- Write Baileys container, scan QR, verify messages arrive
- Write FastAPI `/message` endpoint, call Claude, get a reply
- Verify end-to-end: WhatsApp → Baileys → FastAPI → Claude → reply

### Phase 2 — It remembers (Week 2)

- Stand up Postgres, create schema
- Implement conversation history (save turn, retrieve recent N)
- Implement explicit memory CRUD (save/retrieve facts)
- Verify: "what did we talk about yesterday?" works

### Phase 3 — It knows your notes (Week 3)

- Set up Obsidian vault Git sync on the VPS
- Stand up Qdrant
- Write indexer: chunk `.md` files → embed → store in Qdrant
- Implement `obsidian_search` and `obsidian_read` tools
- Verify: "what have I written about X?" returns relevant notes

### Phase 4 — It has a heartbeat (Week 4)

- Implement heartbeat scheduler with APScheduler
- Implement state synthesis prompt (Haiku)
- Store heartbeat state in Postgres
- Inject latest heartbeat state into every conversation's system prompt
- Verify: agent references your current todos/projects without being asked

### Phase 5 — Browser tools (Month 2) ✓

- Add Playwright browser tool
- Implement confirmation gate for consequential actions

### Phase 6 — Ki builds integrations (March 2026) ✓

- Auto-discovery: `loader.py` scans `integrations/` at startup
- `coding-agent` service: Node.js agentic loop, safety-constrained write tools, git commit + push
- `delegate_coding_task` tool: Ki describes what to build → coding-agent writes it → pushes to GitHub
- Verified end-to-end with `time_tools` integration (Ki-authored, live)
- Ki has since authored: `reminders`, `web_search`, `image_gen`

### Phase 7 — Broader self-modification (March 2026) ✓

- coding-agent scope model: `integrations/`, `tools/`, `prompts/` writable freely; core Python requires `scope: "core"` + `request_confirmation` gate
- Ki can edit `identity.md` and `instructions.md` directly without confirmation
- Prompts now DB-first: `_load_prompt()` checks `settings` table before falling back to file; admin UI can edit prompts without coding-agent or redeploy

### Desktop app + Admin UI (March 2026) ✓

- Desktop channel with SSE streaming responses and WhatsApp→desktop bridge
- Admin UI: heartbeat state viewer, memory browser, vault status, runtime settings, prompt editor, session management
- Reactions (👍/❤️/✕) classified by Haiku — category and content written per reaction emoji, not hardcoded

### April 2026 — Memory & system quality improvements ✓

- **Memory quality:** `memory_save` tool description rewritten with concrete per-category examples (including location/movement guidance for `fact`); Haiku classifies reaction memories instead of hardcoding `fact`
- **Recall:** `recall_past_conversations` tool wires up the Qdrant `conversations` collection (previously computed but unused); Ki can semantically search all past exchanges
- **Heartbeat suppression:** `_build_system()` filters heartbeat items that overlap ≥50% with current session content — stops Ki parroting back things just said
- **Duplicate dedup:** 5-second window check in `save_user_message()` and `save_turn()` prevents double-save race conditions
- **Reminder near-match:** `set_reminder` auto-cancels similar reminders within ±2h before inserting — reminders no longer stack on refinement
- **Cross-channel continuity:** When session gap fires, other-channel messages from the last 2h are injected as context — desktop stays aware of recent WhatsApp activity
- **Bug fix:** `respond()` (WhatsApp path) was not awaiting `_build_system()` — Ki on WhatsApp was receiving a coroutine object as the system prompt
- **Heartbeat step-change:** Synthesis upgraded Haiku → Sonnet; vault todos deduplicated; `backlog_todos` tier for stale items; date-aware `ki_commitments`; staleness pruning for loops (21d) and projects (21d)
- **Vault score threshold:** Auto-injected vault excerpts filtered to score ≥ 0.35

---

## Cost Estimate

| Item | Cost |
|---|---|
| DigitalOcean VPS (existing) | $0 additional |
| Claude API — Sonnet (conversations) | ~$5–10/mo |
| Claude API — Haiku (reactions, bridge) | ~$1–2/mo |
| Obsidian Git sync (GitHub private repo) | $0 |
| WhatsApp (Baileys) | $0 |
| **Total** | **~$6–12/mo** |

To control costs: use Haiku for lightweight classification (reactions, bridge summaries). Use Sonnet for conversations and heartbeat synthesis. Add token counting from day one and log per-request usage to Postgres.

---

## Future: Adding Caddy

When you need to receive inbound webhooks (any external service calling *into* your VPS), add Caddy:

```bash
# Install on the VPS (not in Docker — simpler for shared reverse proxy)
apt install -y debian-keyring debian-archive-keyring apt-transport-https
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
apt install caddy
```

```
# /etc/caddy/Caddyfile
agent.yourdomain.com {
    reverse_proxy localhost:8000
}
```

Caddy handles HTTPS automatically (Let's Encrypt). That's it. No Nginx config complexity.

Until then — SSH tunneling is sufficient:

```bash
# Access FastAPI locally for debugging
ssh -L 8000:localhost:8000 user@your-VPS
```

---

## Things to Decide Before You Start

**1. WhatsApp JID format** Your JID is your phone number in international format + `@s.whatsapp.net`. E.g. `447700900000@s.whatsapp.net`. Set this as `ALLOWED_JID` in `.env`.

**2. Obsidian vault location** Is it on iCloud, Obsidian Sync, or a local folder? This determines your Git sync approach. Obsidian Git plugin (community) is the smoothest path.

**3. Heartbeat frequency and delivery** Every 6 hours is a reasonable start. Decide: should the heartbeat silently update state only, or should it proactively message you on WhatsApp when there's something worth flagging?

**4. Context window budget** Decide on limits upfront: last N conversation turns, top K memory results, top K Obsidian results. A good starting point: 20 conversation turns + 5 memory chunks + 3 Obsidian excerpts. You can tune from there.

**5. First integration** Pick whichever causes you the most daily friction. Build that one first. The framework will make the second one much faster.

---

*Last updated: April 2026*
