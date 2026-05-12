"""
Daily note creator — runs at midnight (user's local time) to create that day's daily note.

At midnight every night:
  1. Reads templates/daily.md from the vault
  2. Gets today's date in USER_TZ timezone
  3. Checks if daily/YYYY-MM-DD.md already exists — skips if so
  4. Replaces placeholders in the template and writes the note

Ki can also call `create_daily_note` directly when the note doesn't exist yet.
"""
import os
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import sqlalchemy as sa

from ..db import SessionLocal
from ..tools.obsidian import VAULT, _safe_path, _git

REQUIRED_ENV_VARS: list[str] = []

TOOL_SCHEMAS: list[dict] = [
    {
        "name": "create_daily_note",
        "description": (
            "Create a daily note from the vault template for a given date. "
            "Injects calendar events and scheduled tasks into the template placeholders. "
            "Use this before appending to a daily note that doesn't exist yet."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": "ISO date string (YYYY-MM-DD). Defaults to today.",
                }
            },
            "required": [],
        },
    }
]

SCHEDULES = [
    {
        "id": "daily_note_creator",
        "fn": "run_daily_note_creator",
        "trigger": "cron",
        "hour": 0,
        "minute": 0,
    },
]

_SCHEDULE_FNS: dict[str, object] = {
    "run_daily_note_creator": None,  # patched below
}

NZ_TZ = ZoneInfo(os.environ.get("USER_TZ", "Pacific/Auckland"))
UTC = ZoneInfo("UTC")
TEMPLATE_PATH = "templates/daily.md"

# Scheduled tasks are paused until this date (inclusive). Set to None to resume immediately.
SCHEDULED_TASKS_PAUSED_UNTIL: date | None = None


def get_schedule_fn(name: str):
    return _SCHEDULE_FNS[name]


async def _fetch_reminders_for_date(note_date: date) -> list[tuple[int, str]]:
    """Return (id, message) pairs for active uninjected reminders on note_date (NZ time)."""
    midnight_nz = datetime(note_date.year, note_date.month, note_date.day, 0, 0, 0, tzinfo=NZ_TZ)
    next_midnight_nz = midnight_nz + timedelta(days=1)
    async with SessionLocal() as session:
        result = await session.execute(
            sa.text("""
                SELECT id, message FROM reminders
                WHERE remind_at >= :start AND remind_at < :end
                  AND sent = false AND cancelled = false AND note_injected = false
            """),
            {"start": midnight_nz.astimezone(UTC), "end": next_midnight_nz.astimezone(UTC)},
        )
        return [(row[0], row[1]) for row in result.fetchall()]


async def _mark_reminders_note_injected(ids: list[int]) -> None:
    async with SessionLocal() as session:
        await session.execute(
            sa.text("UPDATE reminders SET note_injected = true WHERE id = ANY(:ids)"),
            {"ids": ids},
        )
        await session.commit()


def _replace_placeholder(content: str, placeholder: str, replacement: str) -> str:
    """Replace a placeholder in-place, or remove the line entirely if replacement is empty."""
    if replacement:
        return content.replace(placeholder, replacement)
    return "\n".join(line for line in content.split("\n") if placeholder not in line)


async def create_daily_note(note_date: date | None = None) -> str:
    """
    Create a daily note from the template for the given date (defaults to today).
    Returns a status string. Idempotent — returns early if the note already exists.
    """
    if note_date is None:
        note_date = datetime.now(NZ_TZ).date()

    note_filename = f"daily/{note_date.isoformat()}.md"

    await _git("pull", "--quiet", "--autostash")

    note_path = _safe_path(note_filename)
    if note_path.exists():
        return f"{note_filename} already exists"

    template_path = _safe_path(TEMPLATE_PATH)
    if not template_path.exists():
        return f"Template not found at {TEMPLATE_PATH}"

    content = template_path.read_text(encoding="utf-8")

    try:
        from ._google_calendar import get_schedule_section
        calendar_lines = await get_schedule_section(note_date)
    except Exception as e:
        print(f"[daily_note] calendar fetch skipped (non-fatal): {e}")
        calendar_lines = ""
    content = _replace_placeholder(content, "`<injected calendar goes here>`", calendar_lines)

    scheduled_tasks = ""
    if SCHEDULED_TASKS_PAUSED_UNTIL is None or note_date >= SCHEDULED_TASKS_PAUSED_UNTIL:
        try:
            from ..tools.obsidian import get_scheduled_tasks_section
            scheduled_tasks = await get_scheduled_tasks_section(note_date)
        except Exception as e:
            print(f"[daily_note] scheduled tasks fetch skipped (non-fatal): {e}")

    reminder_rows: list[tuple[int, str]] = []
    try:
        reminder_rows = await _fetch_reminders_for_date(note_date)
    except Exception as e:
        print(f"[daily_note] reminder fetch skipped (non-fatal): {e}")

    content = _replace_placeholder(content, "`<injected scheduled tasks go here>`", scheduled_tasks)

    reminder_bullets = "\n".join(f"- [ ] {msg}" for _, msg in reminder_rows) if reminder_rows else ""
    content = _replace_placeholder(content, "`<injected reminders go here>`", reminder_bullets)

    print(f"[daily_note] creating {note_filename}")
    note_path.parent.mkdir(parents=True, exist_ok=True)
    note_path.write_text(content, encoding="utf-8")

    if reminder_rows:
        try:
            await _mark_reminders_note_injected([r[0] for r in reminder_rows])
            print(f"[daily_note] injected {len(reminder_rows)} reminder(s) into {note_filename}")
        except Exception as e:
            print(f"[daily_note] failed to mark reminders as injected (non-fatal): {e}")

    await _git("add", str(note_path.relative_to(VAULT)))
    _config = VAULT / "config" / "task-config.yaml"
    if _config.exists():
        await _git("add", str(_config.relative_to(VAULT)))
    await _git("commit", "-m", f"ki: create daily note {note_date.isoformat()}")
    await _git("push")

    return f"Created {note_filename}"


async def run_daily_note_creator() -> None:
    """Scheduled job: create today's daily note at midnight NZ time."""
    today = datetime.now(NZ_TZ).date()
    print(f"[daily_note] checking daily/{today.isoformat()}.md")
    result = await create_daily_note(today)
    print(f"[daily_note] {result}")


async def run_tool(name: str, input: dict) -> str:
    if name == "create_daily_note":
        date_str = input.get("date")
        note_date = date.fromisoformat(date_str) if date_str else None
        return await create_daily_note(note_date)
    return f"Unknown tool: {name}"


# Patch forward reference now that the function is defined
_SCHEDULE_FNS["run_daily_note_creator"] = run_daily_note_creator
