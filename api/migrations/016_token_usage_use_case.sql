-- Add use_case column to token_usage to track cost breakdown by feature.
-- Existing rows get 'chat' as a reasonable default (most historical usage is conversation).
ALTER TABLE token_usage ADD COLUMN IF NOT EXISTS use_case TEXT NOT NULL DEFAULT 'chat';
CREATE INDEX IF NOT EXISTS token_usage_use_case_idx ON token_usage(use_case, created_at DESC);
