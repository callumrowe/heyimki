CREATE TABLE IF NOT EXISTS schedule_runs (
    id          SERIAL PRIMARY KEY,
    job_id      TEXT        NOT NULL,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    status      TEXT        NOT NULL DEFAULT 'running',
    error       TEXT
);

CREATE INDEX IF NOT EXISTS schedule_runs_job_id_idx ON schedule_runs (job_id, started_at DESC);
