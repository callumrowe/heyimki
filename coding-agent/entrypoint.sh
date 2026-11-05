#!/bin/sh
set -e

# Optional: SSH keys for pushing commits (only used when CODING_AGENT_GIT_PUSH=true).
# Mount a directory containing a deploy key at /secrets/ssh to enable SSH pushes.
if [ -d /secrets/ssh ]; then
  mkdir -p /home/ki/.ssh
  cp -r /secrets/ssh/. /home/ki/.ssh/
  chown -R ki:ki /home/ki/.ssh
  chmod 700 /home/ki/.ssh
  chmod 600 /home/ki/.ssh/* 2>/dev/null || true
fi

# Self-deploys run `docker compose` against the host's Docker daemon, which needs
# host paths. Make the host path resolve to the /repo mount inside this container.
if [ -n "$REPO_HOST_PATH" ] && [ "$REPO_HOST_PATH" != "/repo" ] && [ ! -e "$REPO_HOST_PATH" ]; then
  mkdir -p "$(dirname "$REPO_HOST_PATH")"
  ln -s /repo "$REPO_HOST_PATH"
fi

# Match the Docker socket GID dynamically so ki can run docker commands
if [ -S /var/run/docker.sock ]; then
  DOCKER_GID=$(stat -c '%g' /var/run/docker.sock)
  # Reuse the group if that GID already exists (Docker Desktop's socket is GID 0)
  DOCKER_GROUP=$(getent group "$DOCKER_GID" | cut -d: -f1)
  if [ -z "$DOCKER_GROUP" ]; then
    groupadd -g "$DOCKER_GID" dockerhost
    DOCKER_GROUP=dockerhost
  fi
  usermod -aG "$DOCKER_GROUP" ki 2>/dev/null || true
fi

# The ki user must be able to write the repo. If the checkout belongs to a
# regular host user, run as that UID so file ownership on the host is
# unchanged. Claude Code refuses to run as root, so a root-owned checkout
# (typical on a VPS) is handed to ki instead.
REPO_UID=$(stat -c '%u' /repo 2>/dev/null || echo 0)
if [ "$REPO_UID" != "0" ] && [ "$REPO_UID" != "$(id -u ki)" ]; then
  usermod -o -u "$REPO_UID" ki 2>/dev/null || true
  chown -R ki /home/ki 2>/dev/null || true
elif [ "$REPO_UID" = "0" ]; then
  chown -R ki:ki /repo 2>/dev/null || true
fi

# Ensure git trusts the bind-mounted repo (safe.directory for the ki user).
su ki -s /bin/sh -c 'git config --global --add safe.directory /repo' 2>/dev/null || true

# Start the Node server as ki
exec su ki -s /bin/sh -c 'exec node /app/src/server.js'
