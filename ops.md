# Ops — Things To Do On The VPS

Manual steps that can't live in code. Work through these as needed.

---

## Phase 1 — First Deploy

- [x] SSH into the VPS
- [x] `git clone git@github.com:<your-username>/ki.git /opt/ki`
- [x] `cd /opt/ki && cp .env.example .env && chmod 600 .env`
- [x] Fill in `.env`:
  - `ANTHROPIC_API_KEY` — from console.anthropic.com
  - `YOUR_WHATSAPP_JID` is pre-filled as `<your-number>@s.whatsapp.net` — confirm this is correct
  - Leave everything else blank for now (not needed until later phases)
- [x] Confirm no existing Docker network is named `ki_internal`: `docker network ls`
- [x] `docker compose up --build -d`
- [x] `docker compose logs -f baileys` — scan the QR code with WhatsApp Account 2
- [x] Send a message to yourself from that WhatsApp account, confirm a reply comes back
- [x] `docker compose logs -f api` — confirm no errors

---

## Phase 2 — Postgres Setup

- [x] `mkdir /opt/backups`
- [x] Add to crontab (`crontab -e`):
  ```
  0 3 * * * docker exec ki-postgres-1 pg_dump -U agent agent | gzip > /opt/backups/db_$(date +\%Y\%m\%d).sql.gz
  find /opt/backups -name "db_*.sql.gz" -mtime +14 -delete
  ```

---

## Phase 3 — Vault Setup

- [x] Generate deploy key: `ssh-keygen -t ed25519 -f /root/.ssh/ki_vault_deploy_key -N "" -C "VPS-ki-vault"`
- [x] Print the public key and add it to `<your-username>/ki-vault` → Settings → Deploy Keys (write access on):
      `cat /root/.ssh/ki_vault_deploy_key.pub`
- [x] Clone the vault: `GIT_SSH_COMMAND="ssh -i /root/.ssh/ki_vault_deploy_key" git clone git@github.com:<your-username>/ki-vault.git /opt/vault`
- [x] `git -C /opt/vault config user.name "ki" && git -C /opt/vault config user.email "ki@local"`
- [x] On your Mac: install the Obsidian Git community plugin in the ki-vault vault, set auto-commit + auto-push to 1 minute
- [x] Verify a test note commit appears in `<your-username>/ki-vault` on GitHub before continuing

---

## Phase 4 — Heartbeat Setup

- [x] Add `INTERNAL_SECRET` to `.env`: `echo "INTERNAL_SECRET=$(openssl rand -hex 32)" >> /opt/ki/.env`
- [x] Add `HEARTBEAT_HOURS=3` to `.env`: `echo "HEARTBEAT_HOURS=3" >> /opt/ki/.env`
- [x] Rebuild both containers: `docker compose up --build -d`

**Manual trigger** (runs immediately without waiting):

```bash
docker compose exec api python3 -c "import urllib.request; req = urllib.request.Request('http://localhost:8000/heartbeat/trigger', method='POST'); r = urllib.request.urlopen(req); print(r.read().decode())"
```

---

## Force a Full Vault Reindex

The indexer skips re-embedding if the vault git HEAD hasn't changed since last run.
To force it to re-embed everything (e.g. after changing preprocessing logic):

```bash
# On the VPS — clear the stored commit hash so the indexer thinks nothing is indexed
docker exec ki-postgres-1 psql -U agent agent -c "DELETE FROM indexer_state WHERE key = 'vault_commit';"

# Restart the api container so the indexer runs on startup
docker compose -f /opt/ki/docker-compose.yml restart api

# Watch the indexer output
docker compose -f /opt/ki/docker-compose.yml logs -f api
```

You should see lines like:

```
Indexer: last indexed=none, current=<hash>
Indexer: embedding N changed vault file(s)
Indexer: done (commit <hash>)
```

---

## One-Time VPS Hardening

- [x] `apt install unattended-upgrades && dpkg-reconfigure --priority=low unattended-upgrades`
