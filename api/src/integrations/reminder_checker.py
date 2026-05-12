"""
Reminder checker — runs every minute, fires any due reminders.

Queries the reminders table for rows where:
  remind_at <= now() AND sent = false AND cancelled = false

For each match, sends Callum a WhatsApp message via Baileys and marks
the row as sent.
"""
import sqlalchemy as sa
from datetime import datetime, timezone

from ..db import SessionLocal
from ..push import send_to_user

REQUIRED_ENV_VARS: list[str] = []

TOOL_SCHEMAS: list[dict] = []

SCHEDULES = [
    {
        "id": "reminder_checker",
        "fn": "run_reminder_checker",
        "trigger": "interval",
        "minutes": 1,
    },
]

_SCHEDULE_FNS = {
    "run_reminder_checker": None,  # patched below after function definition
}


def get_schedule_fn(name: str):
    return _SCHEDULE_FNS[name]


async def run_reminder_checker() -> None:
    """Check for due reminders and send them."""
    now_utc = datetime.now(timezone.utc)

    async with SessionLocal() as session:
        result = await session.execute(
            sa.text(
                "SELECT id, message FROM reminders "
                "WHERE remind_at <= :now "
                "  AND sent      = false "
                "  AND cancelled = false "
                "ORDER BY remind_at ASC"
            ),
            {"now": now_utc},
        )
        due = result.fetchall()

        if not due:
            return

        print(f"[reminder_checker] {len(due)} reminder(s) due")

        for row in due:
            reminder_id, message = row[0], row[1]
            delivery_text = f"⏰ Reminder: {message}"
            print(f"[reminder_checker] sending reminder {reminder_id}: {delivery_text!r}")
            delivered = await send_to_user(delivery_text)
            if delivered:
                await session.execute(
                    sa.text("UPDATE reminders SET sent = true WHERE id = :id"),
                    {"id": reminder_id},
                )
                print(f"[reminder_checker] ✓ marked reminder {reminder_id} as sent")
            else:
                print(f"[reminder_checker] ✗ failed to deliver reminder {reminder_id} — will retry next minute")

        await session.commit()


# Patch the forward reference now that the function is defined
_SCHEDULE_FNS["run_reminder_checker"] = run_reminder_checker
