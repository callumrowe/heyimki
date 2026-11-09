# Ki

**A personal AI assistant you host yourself.** Ki knows your Obsidian vault, remembers what you tell it, and checks in when something needs your attention.

Ki runs in Docker on your own computer or a VPS, and you talk to it through an installable web app or WhatsApp. It's built for one person. There are no accounts: it's your server, your keys and your data.

## What Ki does

- **Knows your notes.** Your vault is indexed for semantic search, and relevant excerpts are pulled into every conversation.
- **Writes to your vault.** It adds to daily notes, creates notes, ticks off `- [ ]` todos, and makes each day's note from your template.
- **Remembers.** Conversations and facts about you are kept in Postgres. React 👍 to a message to save it as a memory.
- **Checks in.** Every few hours (outside quiet hours) a heartbeat reviews your todos and loose ends, and messages you if there's something worth saying.
- **Uses the web.** It searches, opens pages and acts on them in a headless browser, and asks before doing anything consequential.
- **Handles images, PDFs and voice notes.**
- **Can extend itself.** The coding agent (on by default) changes Ki's own code, and you approve each change before it goes live.

Ki's personality is a Markdown file: edit [`api/prompts/identity.md`](api/prompts/identity.md) to make it yours.

## Quick start

You need [Docker](https://docs.docker.com/get-docker/) with Compose v2, an [Anthropic API key](https://console.anthropic.com) and an [OpenAI API key](https://platform.openai.com) (for search embeddings and voice transcription).

```bash
git clone https://github.com/callumrowe/heyimki.git ki
cd ki
./setup.sh
```

`setup.sh` asks for your name, timezone, keys and vault, writes `.env`, and starts everything. Open **http://localhost:8000/app/** and log in with the password it prints (`DESKTOP_SECRET` in `.env`).

To configure by hand instead, copy `.env.example` to `.env`, fill it in, and run `docker compose up -d --build`.

### Your vault

Point `KI_VAULT_PATH` at an existing Obsidian vault, or keep the default `./vault`, which is created from [`examples/vault`](examples/vault).

| Vault type | What happens |
|---|---|
| Plain folder | Ki writes files directly and picks up your edits every `VAULT_SYNC_MINUTES`. |
| Git repo | Every change Ki makes is committed. |
| Git repo with a remote | Ki also pulls before writing and pushes after. For a private SSH remote, see [`secrets/README.md`](secrets/README.md). |

Ki expects daily notes at `daily/YYYY-MM-DD.md`, made from `templates/daily.md`, with a `## Daily notes` section. If your vault is laid out differently, edit [`api/prompts/instructions.md`](api/prompts/instructions.md).

## Optional extras

Add profiles to `COMPOSE_PROFILES` in `.env` (comma-separated), then run `docker compose up -d`.

| Profile | Adds |
|---|---|
| `whatsapp` | A WhatsApp gateway that answers your chat only |
| `https` | Caddy with automatic TLS for `KI_DOMAIN`, for use on a VPS |
| `coding-agent` | Lets Ki change its own code, with your approval (see below). `setup.sh` turns it on by default. |

**WhatsApp.** Ki needs a WhatsApp account of its own, such as a spare number. It ignores messages an account sends to itself, so it can't be yours. Say yes to WhatsApp in `./setup.sh` and it walks you through linking:

1. Scan the QR code it shows from Ki's phone (**Settings → Linked devices → Link a device**).
2. Message Ki from your own phone. Setup checks that the message is from you, saves your chat ID as `YOUR_WHATSAPP_JID` and restarts.

Run `bin/ki whatsapp` to add WhatsApp later or to re-link, and `bin/ki whatsapp --new-account` to switch accounts. If Ki's phone unlinks it, the gateway shows a fresh QR code, and `bin/ki whatsapp` walks you through scanning it.

**On a VPS.** Any Linux server with Docker and about 2 GB of RAM will do. Point a DNS record (e.g. `ki.example.com`) at it, open ports 80 and 443, and run `./setup.sh` with that domain. The app is then at `https://ki.example.com/app/`. The api port stays on `127.0.0.1`, so all outside traffic goes through Caddy.

**Coding agent.** Ask Ki for a change and it hands the task to Claude Code, then shows you what changed and waits for you to confirm before deploying.

- **What the agent can reach:**
  - Its own clone of the repo, in a Docker volume. It reads your `.git` read-only, so it never writes your checkout or git hooks, and it never sees `.env`, `secrets/` or your vault.
  - The api and the internet. It has no Docker access and can't reach the database, the browser or WhatsApp.
  - It has its own `CODING_AGENT_SECRET`, so it can't talk to the api as you.
- **What happens when you confirm:**
  - Web app and prompt changes go live straight away.
  - `api/src` changes restart the api. If the new code won't start, Ki rolls back to the previous commit by itself and tells you.
  - Changes to Dockerfiles, packages, `docker-compose.yml` or migrations need you to run `docker compose up -d --build`.
- **What you're still trusting:** once deployed, code runs inside the api, which holds your keys, vault and database. Ki flags changes to files that control how it runs on your machine (compose, Dockerfiles, `setup.sh`, `bin/`), so read those first. Commits stay local; push them yourself.

### Integrations

Each of these turns on when its key is set in `.env`:

| Integration | Key(s) |
|---|---|
| Web search | `BRAVE_SEARCH_API_KEY` |
| Image generation, voice replies | `XAI_API_KEY` |
| GitHub issues and PRs | `GITHUB_TOKEN`, `GITHUB_REPO` |
| Gmail, Calendar, Drive | `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REDIRECT_URI` |
| Browser logins and 2FA (1Password) | `OP_SERVICE_ACCOUNT_TOKEN` |
| Push notifications | `VAPID_PRIVATE_KEY`, `VAPID_PUBLIC_KEY` (generated by `setup.sh`) |

Time tools, reminders and daily notes are always on.

## Managing Ki

`bin/ki` wraps the common commands. It acts on this folder, or on a server over SSH if `KI_HOST` is set.

```bash
bin/ki ps           # service status
bin/ki logs api     # tail logs
bin/ki update       # pull, rebuild, wait until healthy
bin/ki heartbeat    # run the heartbeat now
bin/ki usage        # token usage and estimated cost
bin/ki help         # everything else
```

Every model call and tool use is traced in Phoenix at http://localhost:6006 (`bin/ki phoenix` tunnels to it on a server).

## How it fits together

```
  Web app (PWA)      WhatsApp ──► baileys (optional)
        │                            │
        └──────────► api ◄───────────┘
                (FastAPI agent, scheduler, web app)
                     │
     ┌───────────┬───┴───────┬──────────────┬──────────────┐
  Postgres     Qdrant     browser       your vault    coding-agent
  (history,   (vault +   (headless     (bind mount)   (own clone,
   memories,   memory     Chromium)                    no Docker)
   state)      search)
```

Conversations use Claude Sonnet and background work uses Haiku. Developer notes are in [`CLAUDE.md`](CLAUDE.md).

## Running cost

Roughly $10–50 a month: $0–20 for a 2–4 GB VPS (or nothing on your own computer), $10–30 for the Anthropic API, and about $1 for OpenAI.
