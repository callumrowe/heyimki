-- Store which tools Ki called (and their primary input) for each assistant turn.
-- Used to diagnose 👎 signals: was the error because Ki answered from model knowledge
-- instead of using a tool, or did she use the wrong source?
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS tool_calls JSONB DEFAULT NULL;
CREATE INDEX IF NOT EXISTS conversations_tool_calls_idx ON conversations(id) WHERE tool_calls IS NOT NULL;
