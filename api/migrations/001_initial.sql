CREATE TABLE IF NOT EXISTS conversations (
    id         SERIAL PRIMARY KEY,
    role       TEXT        NOT NULL,
    content    TEXT        NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS memories (
    id         SERIAL PRIMARY KEY,
    category   TEXT        NOT NULL,
    content    TEXT        NOT NULL,
    source     TEXT        NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS heartbeat_states (
    id         SERIAL PRIMARY KEY,
    state      JSONB       NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS token_usage (
    id            SERIAL PRIMARY KEY,
    model         TEXT NOT NULL,
    input_tokens  INT  NOT NULL,
    output_tokens INT  NOT NULL,
    created_at    TIMESTAMPTZ DEFAULT now()
);
