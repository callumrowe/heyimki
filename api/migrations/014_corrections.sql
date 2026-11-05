-- Add signal column to conversations for thumbs_up / thumbs_down / correction signals
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS signal TEXT DEFAULT NULL;

-- Corrections table: captures when the user corrects something Ki said
CREATE TABLE IF NOT EXISTS corrections (
    id                  SERIAL PRIMARY KEY,
    conversation_id     INT REFERENCES conversations(id),
    prior_assistant_msg TEXT,   -- the assistant message being corrected
    correction_text     TEXT,   -- what the user said that was the correction
    created_at          TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS corrections_conversation_id_idx ON corrections(conversation_id);
CREATE INDEX IF NOT EXISTS corrections_created_at_idx ON corrections(created_at DESC);
CREATE INDEX IF NOT EXISTS conversations_signal_idx ON conversations(signal) WHERE signal IS NOT NULL;
