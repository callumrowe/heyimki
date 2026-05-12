CREATE TABLE IF NOT EXISTS google_tokens (
    id           SERIAL PRIMARY KEY,
    access_token  TEXT        NOT NULL,
    refresh_token TEXT        NOT NULL,
    token_expiry  TIMESTAMPTZ,
    email         TEXT,
    created_at    TIMESTAMPTZ DEFAULT NOW(),
    updated_at    TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS google_tokens_updated_idx ON google_tokens (updated_at DESC);
