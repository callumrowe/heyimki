"""
Time tools integration.

Exposes two conversation tools:

- get_current_time_user — the current date/time in the user's timezone (USER_TZ).
- get_current_time_in — the current date/time in any IANA timezone, for
  questions like "what time is it in Tokyo?".

No environment variables or scheduled jobs are required.
"""
import os
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_USER_TZ_NAME = (os.environ.get("USER_TZ") or "UTC")

REQUIRED_ENV_VARS: list[str] = []

TOOL_SCHEMAS = [
    {
        "name": "get_current_time_user",
        "description": (
            "Returns the current date and time in the user's local timezone. "
            "Use this when the user asks what time or date it is."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_current_time_in",
        "description": (
            "Returns the current date and time in a given IANA timezone. "
            "Use this when the user asks what time it is somewhere else, or for someone in another place."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "timezone": {
                    "type": "string",
                    "description": "IANA timezone name, e.g. 'America/New_York', 'Europe/London', 'Asia/Tokyo'.",
                },
            },
            "required": ["timezone"],
        },
    },
]

SCHEDULES: list[dict] = []


async def run_tool(name: str, input: dict) -> str:
    if name == "get_current_time_user":
        now = datetime.now(tz=ZoneInfo(_USER_TZ_NAME))
        return now.strftime("%A, %d %B %Y %H:%M %Z")

    if name == "get_current_time_in":
        tz_name = (input.get("timezone") or "").strip()
        try:
            now = datetime.now(tz=ZoneInfo(tz_name))
        except (ZoneInfoNotFoundError, ValueError):
            return f"Unknown timezone: {tz_name!r}. Use an IANA name like 'America/New_York'."
        return now.strftime("%A, %d %B %Y %H:%M %Z")

    return f"Unknown time_tools tool: {name}"
