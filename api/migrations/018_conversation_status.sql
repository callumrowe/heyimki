-- Track streaming state of assistant messages so partial content is preserved on restart.
-- Values: streaming (in flight), partial (interrupted), done (complete).
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'done';
