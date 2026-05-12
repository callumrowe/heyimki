CREATE TABLE pending_actions (
    id          SERIAL PRIMARY KEY,
    action_name TEXT NOT NULL,
    details     TEXT NOT NULL,
    payload     JSONB NOT NULL,
    created_at  TIMESTAMPTZ DEFAULT now(),
    expires_at  TIMESTAMPTZ NOT NULL,
    resolved_at TIMESTAMPTZ,
    confirmed   BOOLEAN
)
