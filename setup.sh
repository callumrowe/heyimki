#!/usr/bin/env bash
# Interactive first-time setup for Ki: writes .env, prepares a vault, starts the stack.
#   ./setup.sh           create .env (refuses to overwrite an existing one)
#   ./setup.sh --force   start over with a fresh .env
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

# shellcheck source=scripts/lib.sh
source scripts/lib.sh

detect_tz() {
  if [[ -n "${TZ:-}" && "$TZ" == */* ]]; then echo "$TZ"; return; fi
  local link
  link="$(readlink /etc/localtime 2>/dev/null || true)"
  if [[ "$link" == *zoneinfo/* ]]; then echo "${link##*zoneinfo/}"; return; fi
  if [[ -f /etc/timezone ]]; then cat /etc/timezone; return; fi
  echo "UTC"
}

# ── Preflight ─────────────────────────────────────────────────────────────────
command -v docker >/dev/null 2>&1 || fail "Docker is not installed. Get it from https://docs.docker.com/get-docker/"
docker compose version >/dev/null 2>&1 || fail "Docker Compose v2 is required (the 'docker compose' command)."

if [[ -f .env && "${1:-}" != "--force" ]]; then
  fail ".env already exists. Edit it directly, or run ./setup.sh --force to start over."
fi

bold "Ki setup"
dim  "Answers go into .env. You can change any of them later by editing that file."
echo

# ── You ───────────────────────────────────────────────────────────────────────
USER_NAME="$(ask "Your first name")"
USER_TZ="$(ask "Your timezone" "$(detect_tz)")"
echo

# ── Keys ──────────────────────────────────────────────────────────────────────
dim "Ki needs an Anthropic key (https://console.anthropic.com) and an OpenAI key"
dim "(https://platform.openai.com — used for search embeddings and voice notes)."
ANTHROPIC_API_KEY="$(ask_secret "Anthropic API key")"
[[ -n "$ANTHROPIC_API_KEY" ]] || fail "An Anthropic API key is required."
OPENAI_API_KEY="$(ask_secret "OpenAI API key")"
[[ -n "$OPENAI_API_KEY" ]] || fail "An OpenAI API key is required."
echo

# ── Vault ─────────────────────────────────────────────────────────────────────
dim "Ki reads and writes an Obsidian vault. Point her at yours, or press Enter"
dim "to start with a fresh example vault in ./vault."
KI_VAULT_PATH="$(ask "Path to your Obsidian vault" "./vault")"
KI_VAULT_PATH="${KI_VAULT_PATH/#\~/$HOME}"
if [[ ! -d "$KI_VAULT_PATH" ]]; then
  # The default needs no confirmation; any other missing path gets asked about.
  if [[ "$KI_VAULT_PATH" == "./vault" ]] \
     || yes_no "No folder at $KI_VAULT_PATH. Create it from the example vault?" y; then
    mkdir -p "$(dirname "$KI_VAULT_PATH")"
    cp -R examples/vault "$KI_VAULT_PATH"
    find "$KI_VAULT_PATH" -name .gitkeep -delete
    echo "Created $KI_VAULT_PATH from examples/vault."
  else
    fail "No folder at $KI_VAULT_PATH. Create it, or run setup again with another path."
  fi
fi
if [[ ! -f "$KI_VAULT_PATH/templates/daily.md" ]]; then
  warn "No templates/daily.md in your vault — copy examples/vault/templates/daily.md there"
  warn "if you want Ki to create daily notes."
fi
echo

# ── Optional parts ────────────────────────────────────────────────────────────
PROFILES=()
LINK_LATER=false
if yes_no "Connect WhatsApp? (you'll scan a QR code with the account Ki should use)" n; then
  PROFILES+=("whatsapp")
fi

echo
if [[ -d .git ]]; then
  dim "The coding agent lets Ki change its own code. It works in its own copy of"
  dim "this repo with no Docker access, and you confirm every change before it goes live."
  if yes_no "Enable the coding agent?" y; then
    PROFILES+=("coding-agent")
  fi
else
  dim "Skipping the coding agent: it needs this folder to be a git clone."
fi

echo
dim "On a server with a domain name, Ki can serve itself over HTTPS (Caddy +"
dim "Let's Encrypt). Leave empty when running on your own computer."
KI_DOMAIN="$(ask "Domain for HTTPS (e.g. ki.example.com)" "")"
if [[ -n "$KI_DOMAIN" ]]; then
  PROFILES+=("https")
fi

COMPOSE_PROFILES="$(IFS=,; echo "${PROFILES[*]:-}")"

# ── Write .env ────────────────────────────────────────────────────────────────
cp .env.example .env
chmod 600 .env
DESKTOP_SECRET="$(gen_secret)"
set_kv USER_NAME "$USER_NAME"
set_kv USER_TZ "$USER_TZ"
set_kv ANTHROPIC_API_KEY "$ANTHROPIC_API_KEY"
set_kv OPENAI_API_KEY "$OPENAI_API_KEY"
set_kv DB_PASSWORD "$(gen_secret)"
set_kv INTERNAL_SECRET "$(gen_secret)"
set_kv CODING_AGENT_SECRET "$(gen_secret)"
set_kv DESKTOP_SECRET "$DESKTOP_SECRET"
set_kv KI_VAULT_PATH "$KI_VAULT_PATH"
set_kv COMPOSE_PROFILES "$COMPOSE_PROFILES"
set_kv KI_DOMAIN "$KI_DOMAIN"
if [[ -n "$KI_DOMAIN" ]]; then
  set_kv GOOGLE_REDIRECT_URI "https://$KI_DOMAIN/auth/google/callback"
fi

echo
bold "Wrote .env"
dim  "Profiles: ${COMPOSE_PROFILES:-none}"
echo

if ! yes_no "Build and start Ki now? (the first build takes several minutes)" y; then
  echo "Start later with: docker compose up -d --build"
  exit 0
fi

docker compose up -d --build

# Web push keys need the api image, which now exists.
if VAPID="$(docker compose run --rm --no-deps -T api python3 -m src.gen_vapid 2>/dev/null)"; then
  set_kv VAPID_PRIVATE_KEY "$(printf '%s\n' "$VAPID" | sed -n 's/^VAPID_PRIVATE_KEY=//p')"
  set_kv VAPID_PUBLIC_KEY  "$(printf '%s\n' "$VAPID" | sed -n 's/^VAPID_PUBLIC_KEY=//p')"
  docker compose up -d api >/dev/null
fi

if [[ ",$COMPOSE_PROFILES," == *",whatsapp,"* ]]; then
  echo
  if yes_no "Link WhatsApp now? (have the phone Ki will use, and your own, to hand)" y; then
    # Ctrl+C stops the linking step only; setup still prints the summary below.
    trap 'true' INT
    scripts/link-whatsapp.sh || LINK_LATER=true
    trap - INT
  else
    LINK_LATER=true
  fi
fi
echo
bold "Ki is starting."
if [[ -n "$KI_DOMAIN" ]]; then
  echo "  Web app:  https://$KI_DOMAIN/app/   (point DNS at this server and open ports 80/443)"
else
  echo "  Web app:  http://localhost:8000/app/"
fi
echo "  Password: $DESKTOP_SECRET   (DESKTOP_SECRET in .env)"
echo "  Logs:     docker compose logs -f api"
if [[ "${LINK_LATER:-false}" == true ]]; then
  echo "  WhatsApp: not linked yet. Link it with: bin/ki whatsapp"
fi
