"""
Time tools integration.

Exposes two conversation tools:

- get_current_time_user — returns the current date/time in the user's local timezone (USER_TZ env var).
  Use when Callum asks "what time is it?" or similar questions about his own time.

- get_current_time_newyork — returns the current date/time in America/New_York.
  Use when Callum asks about Becky's time, or what time it is in New York or Philadelphia.

No environment variables or scheduled jobs are required.
"""
import os
from datetime import datetime
from zoneinfo import ZoneInfo

_USER_TZ_NAME = os.environ.get("USER_TZ", "Pacific/Auckland")

REQUIRED_ENV_VARS: list[str] = []

TOOL_SCHEMAS = [
    {
        "name": "get_current_time_user",
        "description": (
            "Returns the current date and time in the user's local timezone. "
            "Use this when Callum asks what time or date it is, or asks about his own local time."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_current_time_newyork",
        "description": (
            "Returns the current date and time in the America/New_York timezone. "
            "Use this when Callum asks about Becky's time, or what time it is in New York or Philadelphia."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
]

SCHEDULES: list[dict] = []


async def run_tool(name: str, input: dict) -> str:
    if name == "get_current_time_user":
        now = datetime.now(tz=ZoneInfo(_USER_TZ_NAME))
        return now.strftime("%A, %d %B %Y %H:%M %Z")

    if name == "get_current_time_newyork":
        now = datetime.now(tz=ZoneInfo("America/New_York"))
        return now.strftime("%A, %d %B %Y %H:%M %Z")

    return f"Unknown time_tools tool: {name}"
