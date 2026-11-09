CREATE TABLE IF NOT EXISTS reminders (
    id          SERIAL PRIMARY KEY,
    message     TEXT        NOT NULL,
    remind_at   TIMESTAMPTZ NOT NULL,
    sent        BOOLEAN     NOT NULL DEFAULT false,
    cancelled   BOOLEAN     NOT NULL DEFAULT false,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
