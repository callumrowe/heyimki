"""
Consequential action gate.

When Claude wants to take an irreversible or real-world action it calls
request_confirmation(), which saves a pending_action row and returns a prompt.
On the next user message, respond() checks for a pending action and dispatches
"confirm" or "cancel" before the normal agent loop runs.
"""
import json
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa

from .db import engine

EXPIRY_MINUTES = 10


async def get_pending_action() -> dict | None:
    """Return the most recent unexpired, unresolved pending action or None."""
    async with engine.connect() as conn:
        row = await conn.execute(sa.text("""
            SELECT id, action_name, details, payload
            FROM pending_actions
            WHERE expires_at > now() AND resolved_at IS NULL
            ORDER BY created_at DESC
            LIMIT 1
        """))
        r = row.fetchone()
    if r:
        return {"id": r[0], "action_name": r[1], "details": r[2], "payload": r[3]}
    return None


async def save_pending_action(action_name: str, details: str, payload: dict) -> int:
    """Persist a pending action and return its id."""
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=EXPIRY_MINUTES)
    async with engine.begin() as conn:
        row = await conn.execute(
            sa.text("""
                INSERT INTO pending_actions (action_name, details, payload, expires_at)
                VALUES (:action_name, :details, CAST(:payload AS jsonb), :expires_at)
                RETURNING id
            """),
            {
                "action_name": action_name,
                "details": details,
                "payload": json.dumps(payload),
                "expires_at": expires_at,
            },
        )
        return row.scalar()


async def resolve_pending_action(action_id: int, confirmed: bool) -> None:
    """Mark a pending action as resolved."""
    async with engine.begin() as conn:
        await conn.execute(
            sa.text("""
                UPDATE pending_actions
                SET resolved_at = now(), confirmed = :confirmed
                WHERE id = :id
            """),
            {"id": action_id, "confirmed": confirmed},
        )


async def execute_pending_action(action: dict) -> str:
    """Dispatch and execute a confirmed pending action. Returns a result string."""
    name = action["action_name"]
    payload = action["payload"]

    if name == "browser_navigate":
        from .tools.browser import navigate
        return await navigate(payload["url"])

    if name == "browser_act":
        from .tools.browser import browser_act
        return await browser_act(payload["task"])

    if name == "deploy_code":
        from .self_deploy import apply
        return await apply(payload["base"], payload["head"])

    return f"No executor registered for action '{name}'."
