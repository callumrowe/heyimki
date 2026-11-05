# Ki

**A personal AI assistant you host yourself. She knows your Obsidian vault, remembers what you tell her, and checks in when something needs your attention.**

Ki runs in Docker on your own computer or any VPS. You talk to her in a web app you can install on your phone, or on WhatsApp. She reads and writes your notes, keeps track of your todos, browses the web, and every few hours reviews what's going on and messages you if there's something worth saying.

She's built for one person: you. There are no accounts and no multi-tenancy. It's your server, your keys and your data.

---

## What she does

- **Knows your notes.** Your Obsidian vault is indexed for semantic search. Ki searches it when you ask about projects, people or plans, and pulls relevant excerpts into every conversation.
- **Writes to your vault.** She appends to daily notes, creates new notes, and ticks off `- [ ]` todos when you tell her something's done. She also creates each day's note from your template at midnight.
- **Remembers.** Conversations and the things she learns about you are kept in Postgres. React 👍 to one of her messages and she files what it was about as a memory.
- **Checks in.** The heartbeat runs every few hours outside quiet hours. It works out what's open (todos, loose ends, things you said you'd do) and, if there's something real, sends you a short message in Ki's voice.
- **Uses the web.** She can search, open pages, and act on them in a headless browser. Consequential actions go through a confirmation step.
- **Handles more than text.** Images, PDFs and voice notes (transcribed with Whisper) all work.
- **Can extend herself (optional).** With the coding agent switched on, she can write new integrations and change her own code.
- **Traces everything.** Every model call and tool use is visible in Phoenix.

Ki's personality is a plain Markdown file. Edit [`api/prompts/identity.md`](api/prompts/identity.md) to make her yours.

---

## Quick start

You need [Docker](https://docs.docker.com/get-docker/) with Compose v2, an [Anthropic API key](https://console.anthropic.com), and an [OpenAI API key](https://platform.openai.com) (used for search embeddings and voice transcription).

```bash
git clone https://github.com/callumrowe/heyimki.git ki
cd ki
./setup.sh
```

`setup.sh` asks for your name, timezone, keys and vault, writes `.env`, and starts the stack. Then open **http://localhost:8000/app/** and enter the password it prints (`DESKTOP_SECRET` in `.env`).

Prefer to do it by hand? `cp .env.example .env`, fill it in, then run `docker compose up -d --build`.

### Your vault

Set `KI_VAULT_PATH` in `.env` to the folder of an existing Obsidian vault, or keep the default `./vault`, which setup creates from [`examples/vault`](examples/vault).

- **A plain folder works.** Ki writes files directly and picks up your edits every few minutes (`VAULT_SYNC_MINUTES`).
- **A git repo gets history.** Every change Ki makes is committed.
- **A git repo with a remote stays in sync.** Ki pulls before writing and pushes after. For a private repo over SSH, put a deploy key at `secrets/vault_deploy_key` (see [`secrets/README.md`](secrets/README.md)).

Ki expects daily notes at `daily/YYYY-MM-DD.md`, made from `templates/daily.md`, with a `## Daily notes` section. If your vault is laid out differently, edit `api/prompts/instructions.md`.

---

## Optional parts

Switch these on with `COMPOSE_PROFILES` in `.env` (comma-separated), then run `docker compose up -d`.

| Profile | What it adds |
|---|---|
| `whatsapp` | WhatsApp gateway. Ki answers one WhatsApp chat: yours. |
| `https` | Caddy reverse proxy with automatic Let's Encrypt TLS for `KI_DOMAIN`. Use this on a VPS. |
| `coding-agent` | Ki can edit her own code and rebuild her containers. **Read the warning below first.** |

### WhatsApp

Use a WhatsApp account you can dedicate to Ki, such as a spare number.

1. Add `whatsapp` to `COMPOSE_PROFILES` and run `docker compose up -d`.
2. Run `docker compose logs -f baileys`, then scan the QR code from that account (**Settings → Linked devices**).
3. Message that account from your own phone. The log prints `set YOUR_WHATSAPP_JID=...`.
4. Put that value in `.env` and run `docker compose up -d baileys api`.

### On a VPS

Any Linux server with Docker and about 2 GB of RAM works.

1. Point a DNS A record (for example `ki.example.com`) at the server.
2. Open ports 80 and 443.
3. Run `./setup.sh` on the server and enter that domain when asked.

Caddy fetches the certificate itself. The web app will be at `https://ki.example.com/app/`. The api port stays bound to `127.0.0.1`, so all outside traffic goes through Caddy.

### Coding agent: understand this before you turn it on

The coding agent runs Claude Code with write access to this repo **and the Docker socket**. In practice that means root on the machine. Anyone who can steer Ki can, in effect, run code on your server, and that includes a web page she reads that tries to trick her. All internal calls need `INTERNAL_SECRET`, but the agent itself acts on whatever Ki asks. Only enable it on a machine you're comfortable with that.

By default its commits stay local. Set `CODING_AGENT_GIT_PUSH=true` to push them to the repo's `origin` remote.

### Integrations

These switch on automatically when you set their key in `.env`:

| Integration | Key(s) |
|---|---|
| Web search | `BRAVE_SEARCH_API_KEY` |
| Image generation and voice replies | `XAI_API_KEY` |
| GitHub issues and PRs | `GITHUB_TOKEN`, `GITHUB_REPO` |
| Gmail, Google Calendar and Drive | `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REDIRECT_URI` |
| Logins and 2FA codes for the browser (1Password) | `OP_SERVICE_ACCOUNT_TOKEN` |
| Push notifications | `VAPID_PRIVATE_KEY`, `VAPID_PUBLIC_KEY` (setup.sh generates these) |

Time tools, reminders and daily notes are always on.

---

## Running it

`bin/ki` wraps the common commands. It runs against this folder by default, or against a server over SSH if you set `KI_HOST`.

```bash
bin/ki ps              # service status
bin/ki logs api        # tail logs
bin/ki update          # git pull, rebuild, wait until healthy
bin/ki heartbeat       # run the heartbeat now
bin/ki state           # latest heartbeat state
bin/ki usage           # token usage and estimated cost
bin/ki memories        # what Ki remembers about you
bin/ki help            # everything else
```

Phoenix tracing is at http://localhost:6006. On a server, `bin/ki phoenix` opens an SSH tunnel to it.

---

## Architecture

```
  Web app (PWA)      WhatsApp ──► baileys (optional)
        │                            │
        └──────────► api ◄───────────┘
                (FastAPI agent, scheduler, web app)
                     │
     ┌───────────┬───┴───────┬──────────────┬──────────────┐
  Postgres     Qdrant     browser       your vault    coding-agent
  (history,   (vault +   (headless     (bind mount)   (optional)
   memories,   memory     Chromium)
   state)      search)
```

**Stack:** Python and FastAPI · Node.js (Baileys, coding agent) · Claude (Sonnet for conversation, Haiku for background work) · PostgreSQL · Qdrant · Playwright and browser-use · APScheduler · Phoenix.

Developer notes are in [`CLAUDE.md`](CLAUDE.md).

---

## Running cost

| Item | Typical |
|---|---|
| VPS with 2–4 GB RAM (or your own computer) | $0–20/mo |
| Anthropic API | $10–30/mo |
| OpenAI API (embeddings and Whisper) | ~$1/mo |
