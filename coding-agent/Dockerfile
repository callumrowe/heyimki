FROM node:20-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    git python3 openssh-client curl ca-certificates docker.io && \
    rm -rf /var/lib/apt/lists/* && \
    mkdir -p /usr/local/lib/docker/cli-plugins && \
    curl -fsSL "https://github.com/docker/compose/releases/download/v2.36.0/docker-compose-linux-x86_64" \
      -o /usr/local/lib/docker/cli-plugins/docker-compose && \
    chmod +x /usr/local/lib/docker/cli-plugins/docker-compose

# Create non-root user — Claude Code refuses --dangerously-skip-permissions as root
RUN useradd -m -u 1001 -s /bin/bash ki && \
    mkdir -p /home/ki/.claude && \
    chown -R ki:ki /home/ki/.claude

# Set git identity for both root (build-time ops) and ki (runtime ops)
RUN git config --global user.name "Ki" && \
    git config --global user.email "ki@local"
USER ki
RUN git config --global user.name "Ki" && \
    git config --global user.email "ki@local" && \
    git config --global --add safe.directory /repo
USER root

RUN npm install -g @anthropic-ai/claude-code

WORKDIR /app

COPY package.json package-lock.json* ./
RUN npm install --omit=dev

COPY src/ ./src/
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
