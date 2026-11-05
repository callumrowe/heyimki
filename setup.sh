#!/usr/bin/env bash
# Interactive first-time setup for Ki: writes .env, prepares a vault, starts the stack.
#   ./setup.sh           create .env (refuses to overwrite an existing one)
#   ./setup.sh --force   start over with a fresh .env
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"
REPO_DIR="$(pwd)"

bold()  { printf '\033[1m%s\033[0m\n' "$*"; }
dim()   { printf '\033[2m%s\033[0m\n' "$*"; }
warn()  { printf '\033[33m%s\033[0m\n' "$*"; }
fail()  { printf '\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

ask() {        # ask "Prompt" default → echoes the answer
  local prompt="$1" default="${2:-}" reply
  if [[ -n "$default" ]]; then read -r -p "$prompt [$default]: " reply || true
  else read -r -p "$prompt: " reply || true
  fi
  printf '%s' "${reply:-$default}"
}

ask_secret() { # ask_secret "Prompt" → echoes the answer without showing it
  local reply
  read -r -s -p "$1: " reply || true
  echo >&2
  printf '%s' "$reply"
}

yes_no() {     # yes_no "Question" y|n → returns 0 for yes
  local default="$2" reply hint="[y/N]"
  [[ "$default" == "y" ]] && hint="[Y/n]"
  read -r -p "$1 $hint " reply || true
  reply="${reply:-$default}"
  [[ "$reply" =~ ^[Yy] ]]
}

gen_secret() {
  if command -v openssl >/dev/null 2>&1; then openssl rand -hex 32
  else python3 -c "import secrets; print(secrets.token_hex(32))"
  fi
}

set_kv() {     # set_kv KEY VALUE — replace KEY=... in .env
  KEY="$1" VAL="$2" awk 'BEGIN { k = ENVIRON["KEY"]; v = ENVIRON["VAL"] }
    index($0, k "=") == 1 { print k "=" v; found = 1; next } { print }
    END { if (!found) print k "=" v }' .env > .env.tmp
  cat .env.tmp > .env   # rewrite in place so .env keeps its 600 permissions
  rm -f .env.tmp
}

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
if [[ "$KI_VAULT_PATH" == "./vault" && ! -d vault ]]; then
  cp -R examples/vault vault
  find vault -name .gitkeep -delete
  echo "Created ./vault from examples/vault."
elif [[ ! -d "$KI_VAULT_PATH" ]]; then
  fail "No folder at $KI_VAULT_PATH."
fi
if [[ ! -f "$KI_VAULT_PATH/templates/daily.md" ]]; then
  warn "No templates/daily.md in your vault — copy examples/vault/templates/daily.md there"
  warn "if you want Ki to create daily notes."
fi
echo

# ── Optional parts ────────────────────────────────────────────────────────────
PROFILES=()
if yes_no "Connect WhatsApp? (you'll scan a QR code with the account Ki should use)" n; then
  PROFILES+=("whatsapp")
fi

echo
warn "The coding agent lets Ki change her own code and rebuild her containers."
warn "It has write access to this folder and to Docker, so anyone who can talk"
warn "to Ki (or trick her through a web page) can effectively run code here."
if yes_no "Enable the coding agent?" n; then
  PROFILES+=("coding-agent")
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
set_kv DESKTOP_SECRET "$DESKTOP_SECRET"
set_kv KI_VAULT_PATH "$KI_VAULT_PATH"
set_kv COMPOSE_PROFILES "$COMPOSE_PROFILES"
set_kv KI_DOMAIN "$KI_DOMAIN"
set_kv KI_REPO_DIR "$REPO_DIR"
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

echo
bold "Ki is starting."
if [[ -n "$KI_DOMAIN" ]]; then
  echo "  Web app:  https://$KI_DOMAIN/app/   (point DNS at this server and open ports 80/443)"
else
  echo "  Web app:  http://localhost:8000/app/"
fi
echo "  Password: $DESKTOP_SECRET   (DESKTOP_SECRET in .env)"
echo "  Logs:     docker compose logs -f api"
if [[ ",$COMPOSE_PROFILES," == *",whatsapp,"* ]]; then
  echo
  bold "WhatsApp pairing"
  echo "  1. Run: docker compose logs -f baileys   and scan the QR code"
  echo "     (WhatsApp → Settings → Linked devices) with the account Ki should use."
  echo "  2. Message that account from your own phone. The log prints your JID."
  echo "  3. Put it in .env as YOUR_WHATSAPP_JID=..., then: docker compose up -d baileys api"
fi
