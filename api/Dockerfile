FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl git openssh-client gnupg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ── 1Password CLI ─────────────────────────────────────────────────────────────
RUN curl -sS https://downloads.1password.com/linux/keys/1password.asc \
      | gpg --dearmor --output /usr/share/keyrings/1password-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/1password-archive-keyring.gpg] \
https://downloads.1password.com/linux/debian/$(dpkg --print-architecture) stable main" \
      | tee /etc/apt/sources.list.d/1password.list \
    && apt-get update \
    && apt-get install -y 1password-cli \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Playwright still installed so browser-use can import it, but we do NOT
# launch a local Chromium — the persistent browser container handles that.
RUN playwright install chromium --with-deps

COPY src/ ./src/
COPY migrations/ ./migrations/
COPY prompts/ ./prompts/
COPY web/ ./web/

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
