-- APNs device tokens for native iOS push notifications (Capacitor app)
CREATE TABLE IF NOT EXISTS apns_tokens (
    id SERIAL PRIMARY KEY,
    token TEXT UNIQUE NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS apns_tokens_token_idx ON apns_tokens (token);
