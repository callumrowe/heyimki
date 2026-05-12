"""
Reminders integration — lets Ki set, list, and cancel scheduled reminders for Callum.

Tools:
  set_reminder    — schedule a reminder at a given local datetime
  list_reminders  — show all unsent, uncancelled reminders (in user's local timezone)
  cancel_reminder — cancel a reminder by id

No environment variables required. Reminder delivery is handled by the
separate reminder_checker integration (scheduled job).
"""
import logging
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import sqlalchemy as sa

from ..db import SessionLocal

log = logging.getLogger(__name__)

REQUIRED_ENV_VARS: list[str] = []

USER_TZ = ZoneInfo(os.environ.get("USER_TZ", "Pacific/Auckland"))
UTC     = ZoneInfo("UTC")

TOOL_SCHEMAS = [
    {
        "name": "set_reminder",
        "description": (
            "Schedule a reminder message to be sent to Callum at a specific time. "
            "Use this when Callum asks to be reminded about something at a future date/time. "
            "remind_at should be an ISO 8601 datetime string in the user's local timezone — "
            "e.g. '2025-06-15T15:00:00'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "What to remind Callum about.",
                },
                "remind_at": {
                    "type": "string",
                    "description": (
                        "When to send the reminder, as an ISO 8601 datetime in the user's local timezone, "
                        "e.g. '2025-06-15T15:00:00'."
                    ),
                },
            },
            "required": ["message", "remind_at"],
        },
    },
    {
        "name": "list_reminders",
        "description": (
            "List all upcoming (unsent, uncancelled) reminders. "
            "Returns reminder ids, messages, and scheduled times in the user's local timezone."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "cancel_reminder",
        "description": "Cancel a scheduled reminder by its id.",
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {
                    "type": "integer",
                    "description": "The id of the reminder to cancel.",
                },
            },
            "required": ["id"],
        },
    },
]

SCHEDULES: list[dict] = []


async def run_tool(name: str, input: dict) -> str:
    if name == "set_reminder":
        return await _set_reminder(input["message"], input.get("remind_at") or input.get("remind_at_nzdt", ""))
    if name == "list_reminders":
        return await _list_reminders()
    if name == "cancel_reminder":
        return await _cancel_reminder(int(input["id"]))
    return f"Unknown reminders tool: {name}"


# ── helpers ───────────────────────────────────────────────────────────────────

async def _set_reminder(message: str, remind_at: str) -> str:
    log.info("[set_reminder] called — message=%r  remind_at=%r", message, remind_at)
    try:
        # ── datetime parsing ──────────────────────────────────────────────────
        try:
            naive = datetime.fromisoformat(remind_at)
        except ValueError:
            log.warning("[set_reminder] datetime parse failed for %r", remind_at)
            return f"Couldn't parse that datetime: {remind_at!r}. Use ISO 8601, e.g. '2025-06-15T15:00:00'."

        log.info("[set_reminder] parsed naive datetime: %r  (tzinfo=%s)", naive, naive.tzinfo)

        if naive.tzinfo is None:
            local_dt = naive.replace(tzinfo=USER_TZ)
        else:
            local_dt = naive.astimezone(USER_TZ)

        log.info("[set_reminder] local_dt after timezone attachment: %r", local_dt)

        utc_dt = local_dt.astimezone(UTC)
        log.info("[set_reminder] utc_dt after UTC conversion: %r", utc_dt)

        # ── near-duplicate check ──────────────────────────────────────────────
        window_start = utc_dt - timedelta(hours=2)
        window_end = utc_dt + timedelta(hours=2)

        def _word_overlap(a: str, b: str) -> float:
            stop = {"the", "a", "an", "to", "for", "and", "or", "in", "on", "at", "me", "my",
                    "you", "your", "i", "it", "is", "be", "do", "get", "let"}
            wa = {w.lower().strip(".,!?") for w in a.split() if len(w) > 2 and w.lower() not in stop}
            wb = {w.lower().strip(".,!?") for w in b.split() if len(w) > 2 and w.lower() not in stop}
            if not wa or not wb:
                return 0.0
            return len(wa & wb) / min(len(wa), len(wb))

        superseded_ids = []
        try:
            async with SessionLocal() as check_session:
                existing_result = await check_session.execute(
                    sa.text("""
                        SELECT id, message FROM reminders
                        WHERE sent = false AND cancelled = false
                          AND remind_at BETWEEN :start AND :end
                    """),
                    {"start": window_start, "end": window_end},
                )
                for row in existing_result.fetchall():
                    if _word_overlap(message, row[1]) >= 0.5:
                        superseded_ids.append(row[0])
            log.info("[set_reminder] duplicate-check complete — superseded_ids=%s", superseded_ids)
        except Exception as dup_exc:
            log.exception("[set_reminder] exception during duplicate check: %s", dup_exc)
            raise

        if superseded_ids:
            try:
                async with SessionLocal() as cancel_session:
                    await cancel_session.execute(
                        sa.text("UPDATE reminders SET cancelled = true WHERE id = ANY(:ids)"),
                        {"ids": superseded_ids},
                    )
                    await cancel_session.commit()
                log.info("[set_reminder] cancelled superseded reminders: %s", superseded_ids)
            except Exception as cancel_exc:
                log.exception("[set_reminder] exception cancelling superseded reminders: %s", cancel_exc)
                raise

        # ── INSERT ────────────────────────────────────────────────────────────
        try:
            async with SessionLocal() as session:
                log.info(
                    "[set_reminder] executing INSERT — msg=%r  at=%r",
                    message, utc_dt,
                )
                result = await session.execute(
                    sa.text(
                        "INSERT INTO reminders (message, remind_at) "
                        "VALUES (:msg, :at) RETURNING id"
                    ),
                    {"msg": message, "at": utc_dt},
                )
                row = result.fetchone()
                log.info("[set_reminder] INSERT RETURNING row: %r", row)
                await session.commit()
                log.info("[set_reminder] session.commit() completed successfully")
        except Exception as insert_exc:
            log.exception("[set_reminder] exception during INSERT/commit: %s", insert_exc)
            raise

        if row is None:
            log.error("[set_reminder] INSERT returned no row — RETURNING id produced nothing!")
            return "Internal error: reminder INSERT returned no id. Check DB logs."

        log.info("[set_reminder] reminder saved successfully — id=%s", row[0])

        # ── format response ───────────────────────────────────────────────────
        time_str = local_dt.strftime("%-I:%M%p").lower().rstrip("m") + "m"
        date_str = local_dt.strftime("%A %-d %B")
        if local_dt.date() == datetime.now(USER_TZ).date():
            when = f"{time_str} today"
        else:
            when = f"{time_str} on {date_str}"

        superseded_note = f" (replaced {len(superseded_ids)} similar reminder{'s' if len(superseded_ids) > 1 else ''})" if superseded_ids else ""
        return f"Reminder set for {when} — I'll ping you then.{superseded_note} (id: {row[0]})"

    except Exception as top_exc:
        log.exception("[set_reminder] UNHANDLED exception in _set_reminder: %s", top_exc)
        raise


async def _list_reminders() -> str:
    async with SessionLocal() as session:
        result = await session.execute(
            sa.text(
                "SELECT id, message, remind_at FROM reminders "
                "WHERE sent = false AND cancelled = false "
                "ORDER BY remind_at ASC"
            )
        )
        rows = result.fetchall()

    if not rows:
        return "No upcoming reminders."

    lines = []
    for row in rows:
        remind_at_nz = row[2].astimezone(USER_TZ)
        time_str = remind_at_nz.strftime("%-I:%M%p").lower().rstrip("m") + "m"
        date_str = remind_at_nz.strftime("%A %-d %B %Y")
        lines.append(f"[{row[0]}] {date_str} at {time_str} — {row[1]}")

    return "\n".join(lines)


async def _cancel_reminder(reminder_id: int) -> str:
    async with SessionLocal() as session:
        result = await session.execute(
            sa.text(
                "UPDATE reminders SET cancelled = true "
                "WHERE id = :id AND sent = false AND cancelled = false "
                "RETURNING id, message"
            ),
            {"id": reminder_id},
        )
        row = result.fetchone()
        await session.commit()

    if row is None:
        return f"No active reminder found with id {reminder_id}."

    return f"Cancelled reminder {row[0]}: \"{row[1]}\"."
