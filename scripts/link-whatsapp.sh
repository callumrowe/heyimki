#!/usr/bin/env bash
# Link a WhatsApp account to Ki, interactively:
#   1. show the QR code and wait until the account Ki will use is linked
#   2. wait for a message from your own phone, to learn your chat ID (JID)
#   3. save it as YOUR_WHATSAPP_JID in .env and restart baileys + api
#
#   scripts/link-whatsapp.sh               link, or re-learn your JID
#   scripts/link-whatsapp.sh --new-account forget the linked account first
#
# Run by setup.sh, and by `bin/ki whatsapp`. Reads the `[setup] ...` lines
# that baileys/src/index.js logs.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
# shellcheck source=lib.sh
source scripts/lib.sh

[[ -f .env ]] || fail "No .env yet. Run ./setup.sh first."

NEW_ACCOUNT=false
case "${1:-}" in
  --new-account) NEW_ACCOUNT=true ;;
  "") ;;
  *) fail "Usage: scripts/link-whatsapp.sh [--new-account]" ;;
esac

# Switch the whatsapp profile on if it isn't yet.
PROFILES="$(get_kv COMPOSE_PROFILES)"
if [[ ",$PROFILES," != *",whatsapp,"* ]]; then
  PROFILES="${PROFILES:+$PROFILES,}whatsapp"
  set_kv COMPOSE_PROFILES "$PROFILES"
  dim "Added the whatsapp profile to COMPOSE_PROFILES in .env."
fi
export COMPOSE_PROFILES="$PROFILES"

CURRENT_JID="$(get_kv YOUR_WHATSAPP_JID)"
if [[ -n "$CURRENT_JID" && "$NEW_ACCOUNT" == false ]]; then
  echo "Ki already answers $CURRENT_JID."
  yes_no "Link again and pick up your chat ID from scratch?" n || exit 0
fi

# baileys only reports who messages it while YOUR_WHATSAPP_JID is empty.
set_kv YOUR_WHATSAPP_JID ""

if [[ "$NEW_ACCOUNT" == true ]]; then
  dim "Forgetting the linked WhatsApp account..."
  docker compose rm -sf baileys >/dev/null 2>&1 || true
  docker volume rm ki_baileys_auth >/dev/null 2>&1 || true   # compose project is named `ki`
fi

dim "Starting Ki..."
docker compose up -d --build >/dev/null

# Stream baileys' logs from just before it (re)starts, through a FIFO so the
# log follower can be stopped cleanly once we have what we need.
TMP="$(mktemp -d)"
FIFO="$TMP/baileys.log"
mkfifo "$FIFO"
LOGS_PID=""

cleanup() {
  if [[ -n "$LOGS_PID" ]]; then
    # `docker compose` runs as a child of `docker`: stop both.
    pkill -P "$LOGS_PID" 2>/dev/null || true
    kill "$LOGS_PID" 2>/dev/null || true
  fi
  rm -rf "$TMP"
}
on_interrupt() {
  echo
  warn "WhatsApp linking stopped. Ki won't answer on WhatsApp until it's finished."
  echo "Run it again any time with: bin/ki whatsapp"
  exit 130
}
trap cleanup EXIT
trap on_interrupt INT TERM

SINCE="$(date -u -v-5S +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -d '5 seconds ago' +%Y-%m-%dT%H:%M:%SZ)"
# Restart baileys so its output (QR code or "connected") lands after $SINCE.
docker compose up -d --no-deps --force-recreate baileys >/dev/null
docker compose logs -f --no-log-prefix --no-color --since "$SINCE" baileys > "$FIFO" 2>&1 &
LOGS_PID=$!

step2() {
  echo
  ok "✓ WhatsApp is linked."
  echo
  bold "Step 2 of 2 — say hello"
  echo "From your own phone, send any message to the WhatsApp account you just linked."
  dim  "Waiting for it... (Ctrl+C to stop)"
}

LINKED=false
IN_QR=false
JID=""
exec 3< "$FIFO"
while IFS= read -r line <&3; do
  if [[ "$IN_QR" == true ]]; then
    if [[ "$line" == "[setup] qr-end" ]]; then
      IN_QR=false
      dim "The code refreshes every 20 seconds or so. Waiting for the scan... (Ctrl+C to stop)"
    else
      printf '%s\n' "$line"
    fi
    continue
  fi

  case "$line" in
    "[setup] qr")
      IN_QR=true
      printf '\033[2J\033[H'   # clear the screen so each new code replaces the last
      bold "Step 1 of 2 — link the account Ki will use"
      echo "Use a WhatsApp account you can dedicate to Ki (not your own: Ki ignores"
      echo "messages that account sends itself). On that phone, open WhatsApp →"
      echo "Settings → Linked devices → Link a device, and scan this code:"
      echo
      ;;
    "WhatsApp connected")
      if [[ "$LINKED" == false ]]; then
        LINKED=true
        step2
      fi
      ;;
    "[setup] logged-out"*)
      LINKED=false
      warn "That account was unlinked. A new code is on its way."
      ;;
    "[setup] jid "*)
      rest="${line#\[setup\] jid }"
      candidate="${rest%% *}"
      name=""
      [[ "$rest" == *" "* ]] && name="${rest#* }"
      echo
      if yes_no "Got a message from ${name:-someone} ($candidate). Is that you?" y; then
        JID="$candidate"
        break
      fi
      dim "OK, still waiting for your message..."
      ;;
  esac
done
exec 3<&-

[[ -n "$JID" ]] || fail "The WhatsApp gateway stopped before linking finished. Check: docker compose logs baileys"

set_kv YOUR_WHATSAPP_JID "$JID"
dim "Saved YOUR_WHATSAPP_JID=$JID to .env. Restarting..."
docker compose up -d baileys api >/dev/null
echo
ok "✓ Done. Message Ki on WhatsApp and it'll answer."
