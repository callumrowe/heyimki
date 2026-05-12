# ki

**A personal AI that lives on your server, talks to you on WhatsApp, and actually knows what's going on in your life.**

Not a chatbot. Not a wrapper around ChatGPT. Ki is a self-hosted agent with persistent memory, access to your notes and the web — and a heartbeat that keeps her up to date even when you haven't messaged in days.

---

## What it is

Ki runs on any VPS. You talk to her on WhatsApp, just like messaging a person. She remembers everything, checks in proactively, and can take real actions — browsing the web, reading and writing to your Obsidian vault, and even writing her own new integrations.

She has a defined personality. She has opinions. She pushes back. She's not trying to be your assistant — she's trying to be useful.

---

## Features

### WhatsApp-native
Talk to Ki the same way you'd message anyone. Text, images, voice notes — all handled. No app to download, no interface to learn.

### Persistent memory
Every conversation is stored. Ki remembers what you told her last week, what you're working on, what you've deferred. She doesn't ask you to repeat yourself.

### Obsidian vault integration
Your notes are indexed into a vector database on startup. Every message Ki receives is enriched with semantically relevant vault excerpts. She can read notes, write to them, and append to your daily notes in the correct format.

### Proactive heartbeat
Ki synthesises her state every few hours during the day — open todos, ongoing projects, things worth mentioning — and sends you a WhatsApp message if there's something worth saying. No pinging overnight. No hollow check-ins. Only when she has something real.

### Browser access
Ki can navigate the web and act on pages via Playwright and browser-use. Consequential actions (form submissions, purchases) go through a confirmation gate — she asks first.

### Self-extending
Ki can write her own integrations. Describe what you want, she delegates to a sandboxed coding agent, the code is committed to the repo, and a deploy brings it live. She can also edit her own prompts and, behind a confirmation gate, her own core Python files.

### Full observability
Every LLM call and tool use is traced in Phoenix. Token usage is tracked in Postgres and queryable with `ki usage`.

---

## Architecture

```
Your Phone (WhatsApp)
         │
         ▼
   ┌───────────┐
   │  Baileys  │  Node.js WA gateway
   └─────┬─────┘
         │ HTTP (internal Docker network)
         ▼
   ┌───────────┐     ┌────────────┐
   │  FastAPI  │◄───►│  Postgres  │  conversation history, memories, state
   │  (agent)  │     └────────────┘
   └─────┬─────┘
         │
   ┌─────┴──────────────────────────────────┐
   │              Tool Router               │
   ├──────────┬──────────────┬──────────────┤
   ▼          ▼              ▼              ▼
 Qdrant   Playwright   Integrations   coding-agent
 (vault,  (browser)    (calendar,     (writes new
  memory)              time, ...)      integrations)
```

Everything runs in Docker Compose. Nothing is exposed to the internet. The WhatsApp connection, agent brain, databases, and scheduler all communicate over an internal network.

**Stack:** Python + FastAPI · Node.js (Baileys, coding-agent) · Claude API (Sonnet + Haiku) · PostgreSQL · Qdrant · Playwright · APScheduler · Phoenix

---

## Self-hosting

Ki is designed for one person. There's no multi-tenant architecture, no auth layer, no SaaS. It's yours to run, modify, and extend.

**What you need:**
- A VPS (DigitalOcean $6/mo VPS works fine)
- A second WhatsApp account (or a number you can dedicate)
- An Anthropic API key
- An OpenAI API key (embeddings only)

**Deploy:**
```bash
git clone git@github.com:<your-username>/ki.git /opt/ki
cd /opt/ki
cp .env.example .env   # fill in your secrets
docker compose up --build -d
```

Scan the QR code in Baileys logs to connect WhatsApp. That's it.

**Local management via the `ki` CLI:**
```bash
ki deploy       # git pull + rebuild on the VPS
ki logs         # tail all service logs
ki heartbeat    # trigger a heartbeat run immediately
ki memories     # list stored memories
ki state        # latest heartbeat state JSON
ki usage        # token usage last 30 days
ki ssh          # shell on the VPS
```

---

## Estimated running cost

| Item | Cost |
|---|---|
| VPS (2GB) | ~$10–20/mo |
| Anthropic API (typical usage) | ~$10–30/mo |
| OpenAI API (embeddings only) | ~$1/mo |
| **Total** | **~$30–50/mo** |

---

## Personality

Ki's character is defined in `api/prompts/identity.md` — a plain Markdown file, no code. Edit it to tune her voice, values, and communication style. She's designed to have opinions, push back, and care — not to be a polite tool with no point of view.

Operational rules (timezone, formatting, tool use) live in `api/prompts/instructions.md`.

---

## Status

All phases are live and running in production:

- [x] WhatsApp ↔ Claude pipe with persistent Postgres memory
- [x] Obsidian vault indexed into Qdrant with semantic search
- [x] Proactive heartbeat with synthesised state
- [x] Browser tools with consequential action gate
- [x] Image and voice note support
- [x] Ki writes her own integrations (coding-agent)
- [x] Ki edits her own prompts and core files (with confirmation gate)
- [x] Full LLM observability via Phoenix
- [x] `ki` CLI for local management

---

## Docs

- [`personal-ai-agent-runbook.md`](personal-ai-agent-runbook.md) — full architecture and build runbook
- [`plan.md`](plan.md) — detailed build plan and phase history
- [`CLAUDE.md`](CLAUDE.md) — implementation notes and operational details
- [`.env.example`](.env.example) — all environment variables with descriptions
