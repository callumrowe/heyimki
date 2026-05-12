#!/bin/sh
set -e

# Copy SSH keys from the read-only root mount so the ki user can push to GitHub
if [ -d /root/.ssh ]; then
  cp -r /root/.ssh /home/ki/.ssh
  chown -R ki:ki /home/ki/.ssh
  chmod 700 /home/ki/.ssh
  chmod 600 /home/ki/.ssh/* 2>/dev/null || true
  # IdentityFile paths in the copied config still point to /root/.ssh — rewrite them
  sed -i 's|/root/\.ssh/|/home/ki/.ssh/|g' /home/ki/.ssh/config 2>/dev/null || true
fi

# Match the Docker socket GID dynamically so ki can run docker commands
if [ -S /var/run/docker.sock ]; then
  DOCKER_GID=$(stat -c '%g' /var/run/docker.sock)
  groupadd -g "$DOCKER_GID" dockerhost 2>/dev/null || true
  usermod -aG dockerhost ki 2>/dev/null || true
fi

# Give ki ownership of the bind-mounted repo
chown -R ki:ki /repo 2>/dev/null || true

# Ensure git trusts the bind-mounted repo (safe.directory for the ki user)
su ki -s /bin/sh -c 'git config --global --add safe.directory /repo' 2>/dev/null || true

# Start the Node server as ki
exec su ki -s /bin/sh -c 'exec node /app/src/server.js'
