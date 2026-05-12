"""
Google Calendar integration for Ki.

Provides tools for reading, searching, and managing Google Calendar events.
All times are handled in the user's local timezone (USER_TZ env var).

Requires https://www.googleapis.com/auth/calendar scope — if the stored token
is missing this scope, re-auth via GET /auth/google (disconnect then reconnect
from the admin UI System tab).

Token management is shared with the Gmail/Drive integration (google.py).
"""
import os
import urllib.parse
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from .google import _get_valid_token

REQUIRED_ENV_VARS = ["GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REDIRECT_URI"]

SCHEDULES = []

_NOT_CONNECTED = (
    "Google is not connected. Visit the Ki admin page (System tab) to connect your Google account."
)
_NO_SCOPE = (
    "Google Calendar is not authorized — your token is missing the calendar scope. "
    "Go to the Ki admin page (System tab), disconnect Google, then reconnect to grant calendar access."
)
_CALENDAR_BASE = "https://www.googleapis.com/calendar/v3"
_TZ_NAME = os.environ.get("USER_TZ", "Pacific/Auckland")
_TZ = ZoneInfo(_TZ_NAME)

TOOL_SCHEMAS = [
    {
        "name": "get_upcoming_events",
        "description": (
            "Get Callum's upcoming Google Calendar events. "
            "Returns events from now to N days ahead (default 7). "
            "Use when asked about what's coming up, his schedule, or calendar events."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "Number of days ahead to look (default 7)",
                    "default": 7,
                },
                "calendar_id": {
                    "type": "string",
                    "description": "Calendar ID — use list_calendars to find non-primary IDs (default: primary)",
                    "default": "primary",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Max events to return (default 20)",
                    "default": 20,
                },
            },
        },
    },
    {
        "name": "search_calendar",
        "description": (
            "Search Callum's Google Calendar events by keyword and/or date range. "
            "Use when looking for specific meetings, appointments, or events by name."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search keywords (searches title, description, location). Omit to list all events in range.",
                },
                "start_date": {
                    "type": "string",
                    "description": "Start date YYYY-MM-DD (default: today)",
                },
                "end_date": {
                    "type": "string",
                    "description": "End date YYYY-MM-DD (default: 30 days ahead)",
                },
                "calendar_id": {
                    "type": "string",
                    "description": "Calendar ID (default: primary)",
                    "default": "primary",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Max results (default 20)",
                    "default": 20,
                },
            },
        },
    },
    {
        "name": "create_calendar_event",
        "description": (
            "Create a new event in Callum's Google Calendar. "
            "Times are treated as the user's local time unless a timezone offset is included."
            "Always confirm with Callum before creating — this creates a real calendar entry."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Event title"},
                "start_datetime": {
                    "type": "string",
                    "description": "Start datetime in ISO 8601 format, e.g. 2026-04-22T14:00:00 (NZ time assumed if no offset given)",
                },
                "end_datetime": {
                    "type": "string",
                    "description": "End datetime in ISO 8601 format, e.g. 2026-04-22T15:00:00",
                },
                "description": {"type": "string", "description": "Event description (optional)"},
                "location": {"type": "string", "description": "Event location (optional)"},
                "attendees": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of attendee email addresses (optional)",
                },
                "calendar_id": {
                    "type": "string",
                    "description": "Calendar ID (default: primary)",
                    "default": "primary",
                },
            },
            "required": ["title", "start_datetime", "end_datetime"],
        },
    },
    {
        "name": "update_calendar_event",
        "description": (
            "Update an existing Google Calendar event by ID. "
            "Only the fields you provide will change — others stay as-is. "
            "Use get_upcoming_events or search_calendar first to find the event ID. "
            "Always confirm with Callum before updating."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "event_id": {"type": "string", "description": "Google Calendar event ID"},
                "calendar_id": {
                    "type": "string",
                    "description": "Calendar ID (default: primary)",
                    "default": "primary",
                },
                "title": {"type": "string", "description": "New event title (optional)"},
                "start_datetime": {
                    "type": "string",
                    "description": "New start datetime ISO 8601 (optional). NZ time assumed if no offset.",
                },
                "end_datetime": {
                    "type": "string",
                    "description": "New end datetime ISO 8601 (optional). NZ time assumed if no offset.",
                },
                "description": {"type": "string", "description": "New event description (optional)"},
                "location": {"type": "string", "description": "New event location (optional)"},
            },
            "required": ["event_id"],
        },
    },
    {
        "name": "delete_calendar_event",
        "description": (
            "Delete a Google Calendar event by ID. This is permanent and cannot be undone. "
            "Always confirm with Callum before deleting."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "event_id": {"type": "string", "description": "Google Calendar event ID"},
                "calendar_id": {
                    "type": "string",
                    "description": "Calendar ID (default: primary)",
                    "default": "primary",
                },
            },
            "required": ["event_id"],
        },
    },
    {
        "name": "list_calendars",
        "description": (
            "List all calendars in Callum's Google account with their IDs and names. "
            "Use this to find the calendar_id for non-primary calendars before creating or searching events."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_dt(s: str) -> datetime | None:
    """Parse an RFC 3339 datetime string, returning a TZ-aware NZ datetime."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_TZ)
        return dt.astimezone(_TZ)
    except Exception:
        return None


def _localize(dt_str: str) -> str:
    """Parse a user-provided datetime string; treat as NZ time if naive. Returns ISO 8601."""
    dt = datetime.fromisoformat(dt_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_TZ)
    return dt.isoformat()


def _fmt_time(dt: datetime) -> str:
    """Format as '9am', '2:30pm', etc."""
    h = dt.hour % 12 or 12
    ampm = "am" if dt.hour < 12 else "pm"
    if dt.minute == 0:
        return f"{h}{ampm}"
    return f"{h}:{dt.minute:02d}{ampm}"


def _fmt_time_schedule(dt: datetime) -> str:
    """Format as '9:00am', '2:30pm' — always includes minutes, no leading zero."""
    h = dt.hour % 12 or 12
    ampm = "am" if dt.hour < 12 else "pm"
    return f"{h}:{dt.minute:02d}{ampm}"


def _fmt_date(dt: datetime) -> str:
    """Format as 'Tue 22 Apr'."""
    return dt.strftime(f"%a {dt.day} %b")


def _format_event(event: dict, include_id: bool = True) -> str:
    """Format a single calendar event as a readable block."""
    summary = event.get("summary") or "(no title)"
    start = event.get("start", {})
    end = event.get("end", {})

    if "date" in start:
        time_str = "all day"
    else:
        start_dt = _parse_dt(start.get("dateTime", ""))
        end_dt = _parse_dt(end.get("dateTime", ""))
        if start_dt and end_dt:
            time_str = f"{_fmt_time(start_dt)}–{_fmt_time(end_dt)}"
        elif start_dt:
            time_str = _fmt_time(start_dt)
        else:
            time_str = start.get("dateTime", "unknown time")

    lines = [f"• {summary} ({time_str})"]

    location = (event.get("location") or "").strip()
    if location:
        lines.append(f"  Location: {location}")

    description = (event.get("description") or "").strip()
    if description:
        truncated = description[:200] + ("…" if len(description) > 200 else "")
        lines.append(f"  {truncated}")

    attendees = event.get("attendees") or []
    others = [a.get("email", "") for a in attendees if not a.get("self") and a.get("email")]
    if others:
        lines.append(f"  With: {', '.join(others[:5])}")

    if include_id:
        lines.append(f"  ID: {event.get('id', 'unknown')}")

    return "\n".join(lines)


def _cal_id(calendar_id: str) -> str:
    """URL-encode a calendar ID for use in path segments."""
    return urllib.parse.quote(calendar_id, safe="")


def _check_scope_error(resp) -> bool:
    """Return True if the response indicates a missing Calendar scope."""
    try:
        msg = str(resp.json().get("error", {}).get("message", "")).lower()
        return "scope" in msg or "insufficient" in msg or "calendar" in msg
    except Exception:
        return False


# ── API wrappers ──────────────────────────────────────────────────────────────

async def _cal_get(path: str, params: dict | None = None) -> dict:
    token = await _get_valid_token()
    if token is None:
        raise ValueError("not_connected")
    async with httpx.AsyncClient(timeout=20.0) as c:
        r = await c.get(
            f"{_CALENDAR_BASE}{path}",
            headers={"Authorization": f"Bearer {token}"},
            params=params or {},
        )
        if r.status_code in (401, 403) and _check_scope_error(r):
            raise PermissionError("calendar_scope")
        r.raise_for_status()
        return r.json()


async def _cal_post(path: str, payload: dict) -> dict:
    token = await _get_valid_token()
    if token is None:
        raise ValueError("not_connected")
    async with httpx.AsyncClient(timeout=20.0) as c:
        r = await c.post(
            f"{_CALENDAR_BASE}{path}",
            headers={"Authorization": f"Bearer {token}"},
            json=payload,
        )
        if r.status_code in (401, 403) and _check_scope_error(r):
            raise PermissionError("calendar_scope")
        r.raise_for_status()
        return r.json()


async def _cal_patch(path: str, payload: dict) -> dict:
    token = await _get_valid_token()
    if token is None:
        raise ValueError("not_connected")
    async with httpx.AsyncClient(timeout=20.0) as c:
        r = await c.patch(
            f"{_CALENDAR_BASE}{path}",
            headers={"Authorization": f"Bearer {token}"},
            json=payload,
        )
        if r.status_code in (401, 403) and _check_scope_error(r):
            raise PermissionError("calendar_scope")
        r.raise_for_status()
        return r.json()


async def _cal_delete(path: str) -> None:
    token = await _get_valid_token()
    if token is None:
        raise ValueError("not_connected")
    async with httpx.AsyncClient(timeout=20.0) as c:
        r = await c.delete(
            f"{_CALENDAR_BASE}{path}",
            headers={"Authorization": f"Bearer {token}"},
        )
        if r.status_code in (401, 403) and _check_scope_error(r):
            raise PermissionError("calendar_scope")
        if r.status_code != 204:
            r.raise_for_status()


# ── Tool implementations ──────────────────────────────────────────────────────

async def _get_upcoming_events(
    days: int = 7, calendar_id: str = "primary", max_results: int = 20
) -> str:
    now = datetime.now(_TZ)
    time_max = now + timedelta(days=days)
    try:
        data = await _cal_get(f"/calendars/{_cal_id(calendar_id)}/events", {
            "timeMin": now.isoformat(),
            "timeMax": time_max.isoformat(),
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": min(max_results, 100),
            "timeZone": _TZ_NAME,
        })
    except ValueError:
        return _NOT_CONNECTED
    except PermissionError:
        return _NO_SCOPE
    except Exception as e:
        return f"Calendar fetch failed: {e}"

    events = data.get("items", [])
    if not events:
        return f"No events in the next {days} day(s)."

    lines = [f"Upcoming events — next {days} day(s), {len(events)} found:\n"]
    for ev in events:
        lines.append(_format_event(ev))
        lines.append("")
    return "\n".join(lines).strip()


async def _search_calendar(
    query: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    calendar_id: str = "primary",
    max_results: int = 20,
) -> str:
    now = datetime.now(_TZ)
    if start_date:
        try:
            time_min = datetime.fromisoformat(start_date).replace(tzinfo=_TZ).isoformat()
        except Exception:
            return f"Invalid start_date format: {start_date!r} — use YYYY-MM-DD"
    else:
        time_min = now.isoformat()

    if end_date:
        try:
            # End of the given day
            time_max = (
                datetime.fromisoformat(end_date).replace(hour=23, minute=59, second=59, tzinfo=_TZ)
            ).isoformat()
        except Exception:
            return f"Invalid end_date format: {end_date!r} — use YYYY-MM-DD"
    else:
        time_max = (now + timedelta(days=30)).isoformat()

    params: dict = {
        "timeMin": time_min,
        "timeMax": time_max,
        "singleEvents": "true",
        "orderBy": "startTime",
        "maxResults": min(max_results, 100),
        "timeZone": _TZ_NAME,
    }
    if query:
        params["q"] = query

    try:
        data = await _cal_get(f"/calendars/{_cal_id(calendar_id)}/events", params)
    except ValueError:
        return _NOT_CONNECTED
    except PermissionError:
        return _NO_SCOPE
    except Exception as e:
        return f"Calendar search failed: {e}"

    events = data.get("items", [])
    label = f"for '{query}'" if query else "in range"
    if not events:
        return f"No events found {label}."

    lines = [f"Found {len(events)} event(s) {label}:\n"]
    for ev in events:
        lines.append(_format_event(ev))
        lines.append("")
    return "\n".join(lines).strip()


async def _create_calendar_event(
    title: str,
    start_datetime: str,
    end_datetime: str,
    description: str | None = None,
    location: str | None = None,
    attendees: list[str] | None = None,
    calendar_id: str = "primary",
) -> str:
    try:
        start_iso = _localize(start_datetime)
        end_iso = _localize(end_datetime)
    except Exception:
        return (
            f"Invalid datetime format. Use ISO 8601, e.g. 2026-04-22T14:00:00 — "
            f"got start={start_datetime!r} end={end_datetime!r}"
        )

    body: dict = {
        "summary": title,
        "start": {"dateTime": start_iso, "timeZone": _TZ_NAME},
        "end": {"dateTime": end_iso, "timeZone": _TZ_NAME},
    }
    if description:
        body["description"] = description
    if location:
        body["location"] = location
    if attendees:
        body["attendees"] = [{"email": e} for e in attendees]

    try:
        event = await _cal_post(f"/calendars/{_cal_id(calendar_id)}/events", body)
    except ValueError:
        return _NOT_CONNECTED
    except PermissionError:
        return _NO_SCOPE
    except Exception as e:
        return f"Failed to create event: {e}"

    event_id = event.get("id", "unknown")
    link = event.get("htmlLink", "")
    start_dt = _parse_dt(event.get("start", {}).get("dateTime", ""))
    time_label = f" on {_fmt_date(start_dt)} at {_fmt_time(start_dt)}" if start_dt else ""
    return f"Event created: '{title}'{time_label} (ID: {event_id})" + (f"\n{link}" if link else "")


async def _update_calendar_event(
    event_id: str,
    calendar_id: str = "primary",
    title: str | None = None,
    start_datetime: str | None = None,
    end_datetime: str | None = None,
    description: str | None = None,
    location: str | None = None,
) -> str:
    patch: dict = {}
    if title is not None:
        patch["summary"] = title
    if description is not None:
        patch["description"] = description
    if location is not None:
        patch["location"] = location
    if start_datetime is not None:
        try:
            patch["start"] = {"dateTime": _localize(start_datetime), "timeZone": _TZ_NAME}
        except Exception:
            return f"Invalid start_datetime format: {start_datetime!r}"
    if end_datetime is not None:
        try:
            patch["end"] = {"dateTime": _localize(end_datetime), "timeZone": _TZ_NAME}
        except Exception:
            return f"Invalid end_datetime format: {end_datetime!r}"

    if not patch:
        return "Nothing to update — provide at least one field to change."

    path = f"/calendars/{_cal_id(calendar_id)}/events/{urllib.parse.quote(event_id, safe='')}"
    try:
        event = await _cal_patch(path, patch)
    except ValueError:
        return _NOT_CONNECTED
    except PermissionError:
        return _NO_SCOPE
    except Exception as e:
        return f"Failed to update event {event_id}: {e}"

    updated_title = event.get("summary", title or event_id)
    return f"Event updated: '{updated_title}' (ID: {event_id})"


async def _delete_calendar_event(event_id: str, calendar_id: str = "primary") -> str:
    path = f"/calendars/{_cal_id(calendar_id)}/events/{urllib.parse.quote(event_id, safe='')}"
    try:
        await _cal_delete(path)
    except ValueError:
        return _NOT_CONNECTED
    except PermissionError:
        return _NO_SCOPE
    except Exception as e:
        return f"Failed to delete event {event_id}: {e}"
    return f"Event {event_id} deleted."


async def _list_calendars() -> str:
    try:
        data = await _cal_get("/users/me/calendarList")
    except ValueError:
        return _NOT_CONNECTED
    except PermissionError:
        return _NO_SCOPE
    except Exception as e:
        return f"Failed to list calendars: {e}"

    cals = data.get("items", [])
    if not cals:
        return "No calendars found."

    lines = [f"Calendars ({len(cals)}):\n"]
    for cal in cals:
        access = cal.get("accessRole", "")
        primary = " [primary]" if cal.get("primary") else ""
        lines.append(
            f"• {cal.get('summary', '(unnamed)')}{primary}\n"
            f"  ID: {cal.get('id', 'unknown')}\n"
            f"  Access: {access}"
        )
    return "\n".join(lines)


# ── Multi-calendar helpers ────────────────────────────────────────────────────

async def _get_all_calendar_ids() -> list[str]:
    """Return all calendar IDs from the user's calendarList. Raises ValueError/PermissionError if not connected."""
    data = await _cal_get("/users/me/calendarList")
    ids = [cal["id"] for cal in data.get("items", []) if cal.get("id")]
    return ids if ids else ["primary"]


async def _fetch_events_all_calendars(params: dict) -> list[dict]:
    """Fetch events from all calendars, deduplicating by event ID."""
    cal_ids = await _get_all_calendar_ids()
    seen: set[str] = set()
    all_events: list[dict] = []
    for cal_id in cal_ids:
        try:
            data = await _cal_get(f"/calendars/{_cal_id(cal_id)}/events", params)
            for ev in data.get("items", []):
                ev_id = ev.get("id")
                if ev_id and ev_id not in seen:
                    seen.add(ev_id)
                    all_events.append(ev)
        except (ValueError, PermissionError):
            raise
        except Exception:
            continue
    return all_events


def _event_sort_key(ev: dict) -> tuple:
    start = ev.get("start", {})
    if "dateTime" in start:
        return (1, start["dateTime"])
    return (0, start.get("date", ""))


# ── Heartbeat helper ──────────────────────────────────────────────────────────

async def get_heartbeat_events() -> str:
    """
    Return a formatted string of today's remaining and tomorrow's calendar events
    for injection into the heartbeat synthesis prompt.
    Returns empty string if Google is not connected, scope is missing, or no events.
    """
    now = datetime.now(_TZ)
    today_end = now.replace(hour=23, minute=59, second=59, microsecond=0)
    tomorrow_start = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow_end = (now + timedelta(days=1)).replace(hour=23, minute=59, second=59, microsecond=0)

    common_params = {
        "singleEvents": "true",
        "orderBy": "startTime",
        "maxResults": "20",
        "timeZone": _TZ_NAME,
    }

    try:
        today_events = await _fetch_events_all_calendars({
            **common_params,
            "timeMin": now.isoformat(),
            "timeMax": today_end.isoformat(),
        })
        tomorrow_events = await _fetch_events_all_calendars({
            **common_params,
            "timeMin": tomorrow_start.isoformat(),
            "timeMax": tomorrow_end.isoformat(),
        })
    except (ValueError, PermissionError):
        return ""
    except Exception as e:
        print(f"[google_calendar] heartbeat fetch failed (non-fatal): {e}")
        return ""

    today_events.sort(key=_event_sort_key)
    tomorrow_events.sort(key=_event_sort_key)

    if not today_events and not tomorrow_events:
        return ""

    sections: list[str] = []
    today_label = f"Today ({_fmt_date(now)})"
    tomorrow_label = f"Tomorrow ({_fmt_date(now + timedelta(days=1))})"

    if today_events:
        lines = [today_label + ":"]
        for ev in today_events:
            lines.append(_format_event(ev, include_id=False))
        sections.append("\n".join(lines))
    else:
        sections.append(f"{today_label}: no events")

    if tomorrow_events:
        lines = [tomorrow_label + ":"]
        for ev in tomorrow_events:
            lines.append(_format_event(ev, include_id=False))
        sections.append("\n".join(lines))
    else:
        sections.append(f"{tomorrow_label}: no events")

    return "\n\n".join(sections)


async def get_schedule_section(target_date=None) -> str:
    """
    Return plain bullet lines for events on a given date.date (defaults to today).
    All-day events appear first; timed events follow as '- H:MMam/pm — Title'.
    Returns empty string if Google is not connected, scope is missing, no events, or fetch fails.
    """
    if target_date is None:
        target_date = datetime.now(_TZ).date()

    day_start = datetime(target_date.year, target_date.month, target_date.day, 0, 0, 0, tzinfo=_TZ)
    day_end = datetime(target_date.year, target_date.month, target_date.day, 23, 59, 59, tzinfo=_TZ)

    try:
        events = await _fetch_events_all_calendars({
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": "50",
            "timeZone": _TZ_NAME,
            "timeMin": day_start.isoformat(),
            "timeMax": day_end.isoformat(),
        })
    except (ValueError, PermissionError):
        return ""
    except Exception as e:
        print(f"[google_calendar] schedule fetch failed (non-fatal): {e}")
        return ""

    if not events:
        return ""

    lines: list[str] = []
    all_day = [ev for ev in events if "date" in ev.get("start", {})]
    timed = sorted(
        [ev for ev in events if "dateTime" in ev.get("start", {})],
        key=lambda ev: ev["start"]["dateTime"],
    )

    for ev in all_day:
        lines.append(f"- {ev.get('summary') or '(no title)'}")

    for ev in timed:
        start_dt = _parse_dt(ev["start"]["dateTime"])
        summary = ev.get("summary") or "(no title)"
        if start_dt:
            lines.append(f"- {_fmt_time_schedule(start_dt)} — {summary}")
        else:
            lines.append(f"- {summary}")

    return "\n".join(lines)


# ── Dispatcher ────────────────────────────────────────────────────────────────

async def run_tool(name: str, input: dict) -> str:
    if name == "get_upcoming_events":
        return await _get_upcoming_events(
            input.get("days", 7),
            input.get("calendar_id", "primary"),
            input.get("max_results", 20),
        )
    if name == "search_calendar":
        return await _search_calendar(
            input.get("query"),
            input.get("start_date"),
            input.get("end_date"),
            input.get("calendar_id", "primary"),
            input.get("max_results", 20),
        )
    if name == "create_calendar_event":
        return await _create_calendar_event(
            input["title"],
            input["start_datetime"],
            input["end_datetime"],
            input.get("description"),
            input.get("location"),
            input.get("attendees"),
            input.get("calendar_id", "primary"),
        )
    if name == "update_calendar_event":
        return await _update_calendar_event(
            input["event_id"],
            input.get("calendar_id", "primary"),
            input.get("title"),
            input.get("start_datetime"),
            input.get("end_datetime"),
            input.get("description"),
            input.get("location"),
        )
    if name == "delete_calendar_event":
        return await _delete_calendar_event(
            input["event_id"],
            input.get("calendar_id", "primary"),
        )
    if name == "list_calendars":
        return await _list_calendars()
    return f"Unknown google_calendar tool: {name}"
