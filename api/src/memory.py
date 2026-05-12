"""
Memory layer — conversation history and explicit facts backed by Postgres,
with semantic search over conversations via Qdrant.
"""
import json
from datetime import datetime, timezone

import sqlalchemy as sa
from qdrant_client.models import PointStruct

from .db import SessionLocal
from .embeddings import embed
from .vector_store import client as qdrant

CONV_COLLECTION = "conversations"


async def save_user_message(user: str, channel: str = "whatsapp") -> None:
    """Persist the user's message immediately so it isn't lost if the API call fails."""
    async with SessionLocal() as session:
        async with session.begin():
            dup = await session.execute(
                sa.text("""
                    SELECT id FROM conversations
                    WHERE role = 'user' AND content = :content AND channel = :channel
                      AND created_at > now() - INTERVAL '5 seconds'
                    LIMIT 1
                """),
                {"content": user, "channel": channel},
            )
            if dup.first():
                print(f"[dedup] skipping duplicate user message: {user[:40]!r}")
                return
            await session.execute(
                sa.text("INSERT INTO conversations (role, content, channel) VALUES (:role, :content, :channel)"),
                {"role": "user", "content": user, "channel": channel},
            )


async def save_turn(user: str, assistant: str, channel: str = "whatsapp", ki_image: dict | None = None, tool_calls: list[dict] | None = None) -> None:
    # User message is already saved by save_user_message() before the API call.
    # Here we only persist the assistant reply and embed the full turn.
    # tool_calls: list of {"name": str, "input": str} — tools called during this turn.
    async with SessionLocal() as session:
        async with session.begin():
            if assistant:
                dup = await session.execute(
                    sa.text("""
                        SELECT id FROM conversations
                        WHERE role = 'assistant' AND content = :content AND channel = :channel
                          AND created_at > now() - INTERVAL '5 seconds'
                        LIMIT 1
                    """),
                    {"content": assistant, "channel": channel},
                )
                if dup.first():
                    print(f"[dedup] skipping duplicate assistant message: {assistant[:40]!r}")
                    return
            result = await session.execute(
                sa.text(
                    "INSERT INTO conversations (role, content, channel, image_data, image_media_type, tool_calls)"
                    " VALUES (:role, :content, :channel, :image_data, :image_media_type, CAST(:tool_calls AS jsonb)) RETURNING id"
                ),
                {
                    "role": "assistant",
                    "content": assistant,
                    "channel": channel,
                    "image_data": ki_image["data"] if ki_image else None,
                    "image_media_type": ki_image["media_type"] if ki_image else None,
                    "tool_calls": json.dumps(tool_calls) if tool_calls else None,
                },
            )
            row_id = result.scalar()

    # Embed the turn (user + assistant together) for semantic retrieval
    try:
        text = f"User: {user}\nAssistant: {assistant}"
        vector = await embed(text)
        await qdrant.upsert(
            collection_name=CONV_COLLECTION,
            points=[PointStruct(
                id=row_id,
                vector=vector,
                payload={"user": user, "assistant": assistant},
            )],
        )
    except Exception as e:
        print(f"Warning: failed to embed conversation turn: {e}")


async def insert_assistant_placeholder(channel: str) -> int:
    """Insert an empty assistant row immediately and return its ID.

    Called at the start of respond_stream() so that if the process is killed
    mid-stream, whatever text was saved via update_assistant_message() remains
    in the DB rather than being lost entirely.
    """
    async with SessionLocal() as session:
        async with session.begin():
            result = await session.execute(
                sa.text(
                    "INSERT INTO conversations (role, content, channel, status)"
                    " VALUES ('assistant', '', :channel, 'streaming') RETURNING id"
                ),
                {"channel": channel},
            )
            return result.scalar()


async def update_assistant_message(
    row_id: int,
    content: str,
    status: str = "streaming",
    ki_image: dict | None = None,
    tool_calls: list[dict] | None = None,
) -> None:
    """Update an in-progress assistant message row with accumulated text."""
    async with SessionLocal() as session:
        async with session.begin():
            await session.execute(
                sa.text(
                    "UPDATE conversations"
                    " SET content = :content, status = :status,"
                    " image_data = :image_data, image_media_type = :image_media_type,"
                    " tool_calls = CAST(:tool_calls AS jsonb)"
                    " WHERE id = :id"
                ),
                {
                    "id": row_id,
                    "content": content,
                    "status": status,
                    "image_data": ki_image["data"] if ki_image else None,
                    "image_media_type": ki_image["media_type"] if ki_image else None,
                    "tool_calls": json.dumps(tool_calls) if tool_calls else None,
                },
            )


async def embed_turn(row_id: int, user_text: str, assistant_text: str) -> None:
    """Embed a completed turn into Qdrant for semantic retrieval."""
    try:
        text = f"User: {user_text}\nAssistant: {assistant_text}"
        vector = await embed(text)
        await qdrant.upsert(
            collection_name=CONV_COLLECTION,
            points=[PointStruct(
                id=row_id,
                vector=vector,
                payload={"user": user_text, "assistant": assistant_text},
            )],
        )
    except Exception as e:
        print(f"Warning: failed to embed conversation turn: {e}")


async def get_recent_conversation(limit: int = 20, session_gap_hours: float = 1.0, channel: str | None = None) -> list[dict]:
    """Return the last N turns as a Claude-compatible messages array across all channels.

    Cross-channel by default so Ki is aware of conversations on both desktop and WhatsApp.
    Pass channel to restrict to a single channel (used for display purposes only).

    Returns an empty list if the most recent message (across all channels) is older than
    session_gap_hours, treating inactivity as a session boundary, or if a manual reset
    was triggered after the most recent message.
    """
    reset_before_str = await get_setting("session_reset_before")
    if reset_before_str:
        async with SessionLocal() as s:
            res = await s.execute(sa.text("SELECT MAX(created_at) FROM conversations"))
            max_ts = res.scalar()
        if max_ts:
            reset_before = datetime.fromisoformat(reset_before_str)
            if reset_before.tzinfo is None:
                reset_before = reset_before.replace(tzinfo=timezone.utc)
            max_ts_aware = max_ts.replace(tzinfo=timezone.utc) if max_ts.tzinfo is None else max_ts
            if max_ts_aware <= reset_before:
                return []

    async with SessionLocal() as session:
        if channel:
            sql = sa.text("""
                SELECT role, content FROM (
                    SELECT role, content, created_at
                    FROM conversations
                    WHERE channel = :channel
                    ORDER BY created_at DESC
                    LIMIT :limit
                ) sub
                WHERE (SELECT MAX(created_at) FROM conversations WHERE channel = :channel)
                      > now() - (:gap * INTERVAL '1 hour')
                ORDER BY created_at ASC
            """)
            params = {"limit": limit * 2, "gap": session_gap_hours, "channel": channel}
        else:
            sql = sa.text("""
                SELECT role, content FROM (
                    SELECT role, content, created_at
                    FROM conversations
                    ORDER BY created_at DESC
                    LIMIT :limit
                ) sub
                WHERE (SELECT MAX(created_at) FROM conversations)
                      > now() - (:gap * INTERVAL '1 hour')
                ORDER BY created_at ASC
            """)
            params = {"limit": limit * 2, "gap": session_gap_hours}
        result = await session.execute(sql, params)
        return [{"role": row.role, "content": row.content} for row in result if row.content]


async def get_recent_conversation_for_display(limit: int = 60, channel: str = "desktop", after: str | None = None) -> list[dict]:
    """Return recent messages for display in the desktop UI (includes timestamps)."""
    from datetime import datetime as _dt
    after_dt = _dt.fromisoformat(after.replace("Z", "+00:00")) if after else None
    async with SessionLocal() as session:
        if after_dt:
            result = await session.execute(
                sa.text("""
                    SELECT role, content, created_at, image_data, image_media_type, voice_audio, tool_calls
                    FROM conversations
                    WHERE channel = :channel AND created_at > :after
                    ORDER BY created_at ASC
                    LIMIT :limit
                """),
                {"limit": limit, "channel": channel, "after": after_dt},
            )
        else:
            result = await session.execute(
                sa.text("""
                    SELECT role, content, created_at, image_data, image_media_type, voice_audio, tool_calls
                    FROM conversations
                    WHERE channel = :channel
                    ORDER BY created_at DESC
                    LIMIT :limit
                """),
                {"limit": limit, "channel": channel},
            )
        rows = list(result)
        if not after:
            rows.reverse()
        out = []
        for row in rows:
            if not row.content and not row.image_data:
                continue
            msg = {"role": row.role, "content": row.content or "", "ts": row.created_at.isoformat()}
            if row.image_data:
                msg["image_data"] = row.image_data
                msg["image_media_type"] = row.image_media_type
            if row.voice_audio:
                msg["voice_audio"] = row.voice_audio
            if row.tool_calls:
                msg["tool_calls"] = row.tool_calls
            out.append(msg)
        return out


async def save_voice_audio(message_id: int, audio_b64: str) -> None:
    """Store base64 MP3 audio for a Ki voice response."""
    async with SessionLocal() as session:
        async with session.begin():
            await session.execute(
                sa.text("UPDATE conversations SET voice_audio = :audio WHERE id = :id"),
                {"audio": audio_b64, "id": message_id},
            )


async def get_all_media(limit: int = 500) -> list[dict]:
    """Return all messages that contain images, across all channels, newest first."""
    async with SessionLocal() as session:
        result = await session.execute(
            sa.text("""
                SELECT id, role, content, created_at, image_data, image_media_type, channel
                FROM conversations
                WHERE image_data IS NOT NULL
                ORDER BY created_at DESC
                LIMIT :limit
            """),
            {"limit": limit},
        )
        out = []
        for row in result:
            out.append({
                "id": row.id,
                "role": row.role,
                "content": row.content or "",
                "ts": row.created_at.isoformat(),
                "image_data": row.image_data,
                "image_media_type": row.image_media_type,
                "channel": row.channel,
            })
        return out


async def delete_media(message_id: int) -> bool:
    """Null out image data for a conversation row, removing it from the media grid."""
    async with SessionLocal() as session:
        async with session.begin():
            result = await session.execute(
                sa.text(
                    "UPDATE conversations SET image_data = NULL, image_media_type = NULL"
                    " WHERE id = :id AND image_data IS NOT NULL"
                    " RETURNING id"
                ),
                {"id": message_id},
            )
            return result.fetchone() is not None


async def minutes_since_last_message() -> float | None:
    """Return minutes since the most recent conversation turn, or None if no history."""
    async with SessionLocal() as session:
        result = await session.execute(sa.text(
            "SELECT EXTRACT(EPOCH FROM (now() - MAX(created_at))) / 60 FROM conversations"
        ))
        val = result.scalar()
    return float(val) if val is not None else None


async def get_recent_other_channel(current_channel: str, hours: float = 2.0, limit: int = 10) -> list[dict]:
    """Return recent messages from channels other than current_channel, for cross-channel context injection."""
    async with SessionLocal() as session:
        result = await session.execute(
            sa.text("""
                SELECT role, content, channel FROM conversations
                WHERE channel != :channel
                  AND created_at > now() - (:hours * INTERVAL '1 hour')
                  AND content IS NOT NULL AND content != ''
                ORDER BY created_at ASC
                LIMIT :limit
            """),
            {"channel": current_channel, "hours": hours, "limit": limit},
        )
        return [{"role": row.role, "content": row.content, "channel": row.channel} for row in result]


async def search_conversations(query: str, limit: int = 3) -> list[dict]:
    """Semantic search over past conversation turns."""
    try:
        vector = await embed(query)
        response = await qdrant.query_points(
            collection_name=CONV_COLLECTION,
            query=vector,
            limit=limit,
        )
        return [
            {
                "user": r.payload["user"],
                "assistant": r.payload["assistant"],
                "score": round(r.score, 3),
            }
            for r in response.points
        ]
    except Exception as e:
        print(f"Warning: conversation search failed: {e}")
        return []


async def save_memory(category: str, content: str, source: str) -> None:
    async with SessionLocal() as session:
        async with session.begin():
            existing = await session.execute(
                sa.text("SELECT id FROM memories WHERE category = :category AND content = :content LIMIT 1"),
                {"category": category, "content": content},
            )
            if existing.first():
                print(f"[memory] skipping duplicate: {content[:60]}")
                return
            await session.execute(
                sa.text("""
                    INSERT INTO memories (category, content, source)
                    VALUES (:category, :content, :source)
                """),
                {"category": category, "content": content, "source": source},
            )


async def get_memories(category: str | None = None) -> list[dict]:
    async with SessionLocal() as session:
        if category:
            result = await session.execute(
                sa.text("""
                    SELECT id, category, content FROM memories
                    WHERE category = :cat
                    ORDER BY updated_at DESC LIMIT 50
                """),
                {"cat": category},
            )
        else:
            result = await session.execute(
                sa.text("SELECT id, category, content FROM memories WHERE category NOT IN ('todo', 'tool_rule') ORDER BY updated_at DESC LIMIT 50")
            )
        return [{"id": row.id, "category": row.category, "content": row.content} for row in result]


async def get_push_subscriptions() -> list[dict]:
    async with SessionLocal() as session:
        result = await session.execute(
            sa.text("SELECT endpoint, p256dh, auth FROM push_subscriptions ORDER BY id")
        )
        return [{"endpoint": r.endpoint, "p256dh": r.p256dh, "auth": r.auth} for r in result.fetchall()]


async def save_push_subscription(endpoint: str, p256dh: str, auth: str) -> None:
    async with SessionLocal() as session:
        async with session.begin():
            await session.execute(
                sa.text("""
                    INSERT INTO push_subscriptions (endpoint, p256dh, auth)
                    VALUES (:endpoint, :p256dh, :auth)
                    ON CONFLICT (endpoint) DO UPDATE
                    SET p256dh = EXCLUDED.p256dh, auth = EXCLUDED.auth
                """),
                {"endpoint": endpoint, "p256dh": p256dh, "auth": auth},
            )


async def delete_push_subscriptions(endpoints: list[str]) -> None:
    if not endpoints:
        return
    async with SessionLocal() as session:
        async with session.begin():
            await session.execute(
                sa.text("DELETE FROM push_subscriptions WHERE endpoint = ANY(:endpoints)"),
                {"endpoints": endpoints},
            )


async def get_apns_tokens() -> list[str]:
    async with SessionLocal() as session:
        result = await session.execute(
            sa.text("SELECT token FROM apns_tokens ORDER BY id")
        )
        return [r.token for r in result.fetchall()]


async def save_apns_token(token: str) -> None:
    async with SessionLocal() as session:
        async with session.begin():
            await session.execute(
                sa.text("""
                    INSERT INTO apns_tokens (token, updated_at)
                    VALUES (:token, NOW())
                    ON CONFLICT (token) DO UPDATE SET updated_at = NOW()
                """),
                {"token": token},
            )


async def delete_apns_token(token: str) -> None:
    async with SessionLocal() as session:
        async with session.begin():
            await session.execute(
                sa.text("DELETE FROM apns_tokens WHERE token = :token"),
                {"token": token},
            )


async def get_tool_rules() -> list[dict]:
    """Return tool-selection rules learned from 👎 corrections, most recent first."""
    async with SessionLocal() as session:
        result = await session.execute(
            sa.text("SELECT id, content FROM memories WHERE category = 'tool_rule' ORDER BY updated_at DESC LIMIT 20")
        )
        return [{"id": row.id, "content": row.content} for row in result]


async def update_memory(memory_id: int, new_content: str) -> bool:
    async with SessionLocal() as session:
        async with session.begin():
            result = await session.execute(
                sa.text("""
                    UPDATE memories SET content = :content, updated_at = now()
                    WHERE id = :id
                """),
                {"id": memory_id, "content": new_content},
            )
            return result.rowcount > 0


async def delete_memory(memory_id: int) -> bool:
    async with SessionLocal() as session:
        async with session.begin():
            result = await session.execute(
                sa.text("DELETE FROM memories WHERE id = :id"),
                {"id": memory_id},
            )
            return result.rowcount > 0


async def get_conversations_since(hours: float) -> list[dict]:
    """Return all conversation rows from the last N hours, oldest first."""
    async with SessionLocal() as session:
        result = await session.execute(
            sa.text("""
                SELECT role, content FROM conversations
                WHERE created_at > now() - (:hours * INTERVAL '1 hour')
                ORDER BY created_at ASC
            """),
            {"hours": hours},
        )
        return [{"role": row.role, "content": row.content} for row in result]


async def get_last_turn_time():
    """Return the UTC datetime of the most recent conversation turn, or None."""
    from datetime import datetime, timezone
    async with SessionLocal() as session:
        result = await session.execute(
            sa.text("SELECT MAX(created_at) AS last_turn FROM conversations")
        )
        row = result.first()
        if row and row.last_turn:
            ts = row.last_turn
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            return ts
        return None


async def save_heartbeat_state(state: dict) -> None:
    async with SessionLocal() as session:
        async with session.begin():
            await session.execute(
                sa.text("INSERT INTO heartbeat_states (state) VALUES (CAST(:state AS jsonb))"),
                {"state": json.dumps(state)},
            )


async def get_pending_reminder_messages() -> list[str]:
    """Return the message text of all pending (unsent, uncancelled) reminders."""
    async with SessionLocal() as session:
        result = await session.execute(
            sa.text(
                "SELECT message FROM reminders "
                "WHERE sent = false AND cancelled = false "
                "ORDER BY remind_at ASC"
            )
        )
        return [row.message for row in result]


async def get_latest_heartbeat_state() -> dict | None:
    async with SessionLocal() as session:
        result = await session.execute(
            sa.text("SELECT state FROM heartbeat_states ORDER BY created_at DESC LIMIT 1")
        )
        row = result.first()
        return row.state if row else None


async def get_all_heartbeat_states() -> list[dict]:
    async with SessionLocal() as session:
        result = await session.execute(
            sa.text("SELECT id, state, created_at FROM heartbeat_states ORDER BY created_at DESC")
        )
        return [{"id": row.id, "state": row.state, "created_at": row.created_at.isoformat()} for row in result]


async def get_setting(key: str, default: str | None = None) -> str | None:
    """Read a value from the indexer_state KV table."""
    async with SessionLocal() as session:
        result = await session.execute(
            sa.text("SELECT value FROM indexer_state WHERE key = :key"),
            {"key": key},
        )
        row = result.first()
        return row.value if row else default


async def set_setting(key: str, value: str) -> None:
    """Upsert a value in the indexer_state KV table."""
    async with SessionLocal() as session:
        async with session.begin():
            await session.execute(
                sa.text("""
                    INSERT INTO indexer_state (key, value) VALUES (:key, :value)
                    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
                """),
                {"key": key, "value": value},
            )


async def get_prompt(name: str) -> str | None:
    """Read a prompt from the DB (stored as prompt_{name} in indexer_state)."""
    return await get_setting(f"prompt_{name}")


async def set_prompt(name: str, content: str) -> None:
    """Persist a prompt to the DB."""
    await set_setting(f"prompt_{name}", content)


async def log_token_usage(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_creation_tokens: int = 0,
    cache_read_tokens: int = 0,
    use_case: str = "chat",
) -> None:
    async with SessionLocal() as session:
        async with session.begin():
            await session.execute(
                sa.text("""
                    INSERT INTO token_usage (model, input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens, use_case)
                    VALUES (:model, :input, :output, :cache_creation, :cache_read, :use_case)
                """),
                {
                    "model": model,
                    "input": input_tokens,
                    "output": output_tokens,
                    "cache_creation": cache_creation_tokens,
                    "cache_read": cache_read_tokens,
                    "use_case": use_case,
                },
            )
