#!/bin/sh
set -e

# /workspace is a named volume (the agent's own clone). Docker creates it
# owned by root, so hand it to the ki user.
chown ki:ki /workspace

# /src.git is the live repo's .git, mounted read-only and owned by the host
# user. Tell git to trust it so the agent can clone and fetch from it.
su ki -s /bin/sh -c 'git config --global --add safe.directory /src.git; git config --global --add safe.directory /workspace' 2>/dev/null || true

# Claude Code refuses --dangerously-skip-permissions as root.
exec su ki -s /bin/sh -c 'exec node /app/src/server.js'
