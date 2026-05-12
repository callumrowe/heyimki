#!/bin/bash
# Keeps UFW in sync with Cloudflare's published IP ranges for ports 80/443.
# Run weekly via cron. State file tracks the last-known set of IPs so only
# the delta (adds + removes) is applied — no full rule rebuild needed.
set -euo pipefail

STATE_FILE="/etc/cloudflare-ufw-ips.txt"
LOG_TAG="sync-cloudflare-fw"

log() { logger -t "$LOG_TAG" "$*"; echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $*"; }

# Fetch current ranges with a short timeout so a Cloudflare outage can't
# leave us with no rules. Abort entirely if either fetch fails.
V4=$(curl -sf --max-time 15 https://www.cloudflare.com/ips-v4) \
    || { log "ERROR: failed to fetch IPv4 ranges — aborting, no changes made"; exit 1; }
V6=$(curl -sf --max-time 15 https://www.cloudflare.com/ips-v6) \
    || { log "ERROR: failed to fetch IPv6 ranges — aborting, no changes made"; exit 1; }

NEW_IPS=$(printf '%s\n%s' "$V4" "$V6" | grep -v '^$' | sort)
OLD_IPS=""
[ -f "$STATE_FILE" ] && OLD_IPS=$(sort "$STATE_FILE")

if [ "$NEW_IPS" = "$OLD_IPS" ]; then
    log "No changes to Cloudflare IP ranges"
    exit 0
fi

ADDED=0
REMOVED=0

# Add IPs that are in the new list but not the old
while IFS= read -r ip; do
    [ -z "$ip" ] && continue
    if ! echo "$OLD_IPS" | grep -qxF "$ip"; then
        log "Adding $ip"
        ufw allow from "$ip" to any port 80  comment "cloudflare" 2>&1 || true
        ufw allow from "$ip" to any port 443 comment "cloudflare" 2>&1 || true
        ADDED=$((ADDED + 1))
    fi
done <<< "$NEW_IPS"

# Remove IPs that were in the old list but dropped from the new
if [ -n "$OLD_IPS" ]; then
    while IFS= read -r ip; do
        [ -z "$ip" ] && continue
        if ! echo "$NEW_IPS" | grep -qxF "$ip"; then
            log "Removing $ip"
            ufw delete allow from "$ip" to any port 80  2>&1 || true
            ufw delete allow from "$ip" to any port 443 2>&1 || true
            REMOVED=$((REMOVED + 1))
        fi
    done <<< "$OLD_IPS"
fi

echo "$NEW_IPS" > "$STATE_FILE"
log "Done — added $ADDED, removed $REMOVED IP range(s)"
