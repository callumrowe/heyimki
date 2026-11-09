CREATE TABLE IF NOT EXISTS active_session (
    channel     TEXT PRIMARY KEY,
    agent_name  TEXT NOT NULL,
    started_at  TIMESTAMPTZ DEFAULT now(),
    context     JSONB DEFAULT '{}'
);
