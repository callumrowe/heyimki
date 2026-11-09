CREATE TABLE IF NOT EXISTS captcha_requests (
    id SERIAL PRIMARY KEY,
    screenshot_b64 TEXT,
    task_description TEXT,
    answer TEXT,
    status VARCHAR(20) DEFAULT 'pending',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    answered_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS captcha_requests_status_idx ON captcha_requests (status);
CREATE INDEX IF NOT EXISTS captcha_requests_created_at_idx ON captcha_requests (created_at DESC);
