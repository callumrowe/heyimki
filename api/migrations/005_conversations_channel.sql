-- Add channel column to conversations so desktop and WhatsApp threads are kept separate.
-- Existing rows default to 'whatsapp' — the only channel that existed before this migration.
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS channel TEXT NOT NULL DEFAULT 'whatsapp';
CREATE INDEX IF NOT EXISTS conversations_channel_idx ON conversations(channel, created_at DESC);
