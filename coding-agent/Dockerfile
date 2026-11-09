FROM node:20-slim

# No Docker CLI: the coding agent can't reach Docker. python3 is there so
# Claude Code can syntax-check Python changes.
RUN apt-get update && apt-get install -y --no-install-recommends \
    git python3 ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# Non-root user: Claude Code refuses --dangerously-skip-permissions as root
RUN useradd -m -u 1001 -s /bin/bash ki && \
    mkdir -p /home/ki/.claude /workspace && \
    chown -R ki:ki /home/ki/.claude /workspace

USER ki
RUN git config --global user.name "Ki" && \
    git config --global user.email "ki@localhost"
USER root

RUN npm install -g @anthropic-ai/claude-code

WORKDIR /app

COPY package.json package-lock.json* ./
RUN npm install --omit=dev

COPY src/ ./src/
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
