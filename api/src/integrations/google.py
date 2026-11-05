"""
Google Gmail and Drive integration for Ki.

Provides tools for reading, searching, and sending Gmail, and for browsing
and reading Google Drive documents.

OAuth 2.0 tokens are stored in the google_tokens table.
Connect via GET /auth/google — auth endpoints live in main.py.
"""
import base64
import os
import re
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

_USER_TZ_NAME = (os.environ.get("USER_TZ") or "UTC")

import httpx
import sqlalchemy as sa

from ..db import SessionLocal

REQUIRED_ENV_VARS = ["GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REDIRECT_URI"]

TOOL_SCHEMAS = [
    {
        "name": "search_gmail",
        "description": (
            "Search the user's Gmail using Gmail query syntax. "
            "Use when asked about emails, messages, or anything in his inbox. "
            "Examples: 'from:alex', 'subject:visa', 'after:2026/04/01', 'is:unread label:inbox'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Gmail search query"},
                "max_results": {"type": "integer", "description": "Max results (default 10)", "default": 10},
            },
            "required": ["query"],
        },
    },
    {
        "name": "read_email",
        "description": (
            "Read the full body, subject, sender, and date of an email by message ID. "
            "Use after search_gmail to get the full content of a specific email."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "message_id": {
                    "type": "string",
                    "description": "Gmail message ID from search_gmail or list_recent_emails results",
                },
            },
            "required": ["message_id"],
        },
    },
    {
        "name": "send_email",
        "description": (
            "Send an email from the user's Gmail account, or reply to an existing email. "
            "Always call request_confirmation before sending — this is irreversible."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Recipient email address"},
                "subject": {"type": "string", "description": "Email subject line"},
                "body": {"type": "string", "description": "Email body (plain text)"},
                "reply_to_message_id": {
                    "type": "string",
                    "description": "Optional Gmail message ID to reply to (preserves thread)",
                },
            },
            "required": ["to", "subject", "body"],
        },
    },
    {
        "name": "list_recent_emails",
        "description": (
            "List recent emails in the user's Gmail inbox with sender, subject, and date. "
            "Use for a quick overview of what's in his inbox without a specific search."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "max_results": {"type": "integer", "description": "Max results (default 20)", "default": 20},
            },
        },
    },
    {
        "name": "search_drive",
        "description": (
            "Search the user's Google Drive files by name or content keywords. "
            "Use when asked about documents, spreadsheets, or any files stored in their Drive."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "File name or content keywords to search for"},
                "max_results": {"type": "integer", "description": "Max results (default 10)", "default": 10},
            },
            "required": ["query"],
        },
    },
    {
        "name": "read_drive_file",
        "description": (
            "Read the contents of a Google Drive file. "
            "Works with Google Docs (exported as plain text), Sheets (as CSV), Presentations, and text files. "
            "Use when the user asks about a document stored in their Drive."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "file_id": {
                    "type": "string",
                    "description": "Google Drive file ID from search_drive or list_drive_files results",
                },
            },
            "required": ["file_id"],
        },
    },
    {
        "name": "list_drive_files",
        "description": (
            "List files in the user's Google Drive, sorted by most recently modified. "
            "Optionally scope to a specific folder using its ID."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "folder_id": {
                    "type": "string",
                    "description": "Optional Drive folder ID to list files within",
                },
                "max_results": {"type": "integer", "description": "Max results (default 20)", "default": 20},
            },
        },
    },
    {
        "name": "update_google_doc",
        "description": (
            "Replace the entire content of a Google Doc with new plain text. "
            "Use when the user wants to update or rewrite a document in their Drive. "
            "Use read_drive_file first if you need to see the current content. "
            "Always call request_confirmation before making significant changes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "file_id": {
                    "type": "string",
                    "description": "Google Drive file ID of the Google Doc to update (from search_drive or list_drive_files)",
                },
                "content": {
                    "type": "string",
                    "description": "New plain text content to replace the entire document with",
                },
            },
            "required": ["file_id", "content"],
        },
    },
    {
        "name": "draft_email",
        "description": (
            "Create a Gmail draft without sending it. "
            "Use when the user wants to compose something to review before sending."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Recipient email address"},
                "subject": {"type": "string", "description": "Email subject line"},
                "body": {"type": "string", "description": "Email body (plain text)"},
            },
            "required": ["to", "subject", "body"],
        },
    },
    {
        "name": "get_upcoming_events",
        "description": (
            "List the user's upcoming Google Calendar events. "
            "Returns event title, start/end times, location, and description snippet. "
            "Times are shown in the event's original timezone."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "calendar_id": {"type": "string", "description": "Calendar ID (default: 'primary')", "default": "primary"},
                "days": {"type": "integer", "description": "How many days ahead to look (default 7)", "default": 7},
                "max_results": {"type": "integer", "description": "Max results (default 20)", "default": 20},
            },
        },
    },
    {
        "name": "search_calendar",
        "description": (
            "Search the user's Google Calendar events by keyword. "
            "Optionally filter by date range using YYYY-MM-DD strings. "
            "Times without a timezone offset are treated as the user's local time."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Keyword to search events for"},
                "start_date": {"type": "string", "description": "Filter from this date (YYYY-MM-DD), optional"},
                "end_date": {"type": "string", "description": "Filter to this date (YYYY-MM-DD), optional"},
                "calendar_id": {"type": "string", "description": "Calendar ID (default: 'primary')", "default": "primary"},
                "max_results": {"type": "integer", "description": "Max results (default 20)", "default": 20},
            },
            "required": ["query"],
        },
    },
    {
        "name": "create_calendar_event",
        "description": (
            "Create a new event in the user's Google Calendar. "
            "Times without a timezone offset are treated as the user's local time. "
            "Always call request_confirmation before creating — this is irreversible."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Event title"},
                "start_datetime": {
                    "type": "string",
                    "description": "Start datetime (ISO 8601, e.g. 2026-04-22T14:00:00); treated as NZ time if no offset",
                },
                "end_datetime": {
                    "type": "string",
                    "description": "End datetime (ISO 8601); treated as NZ time if no offset",
                },
                "description": {"type": "string", "description": "Optional event description"},
                "location": {"type": "string", "description": "Optional location"},
                "attendees": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional list of attendee email addresses",
                },
                "calendar_id": {"type": "string", "description": "Calendar ID (default: 'primary')", "default": "primary"},
            },
            "required": ["title", "start_datetime", "end_datetime"],
        },
    },
    {
        "name": "update_calendar_event",
        "description": (
            "Update an existing Google Calendar event — only the fields you provide are changed. "
            "Times without a timezone offset are treated as the user's local time. "
            "Use get_upcoming_events or search_calendar to find the event ID first."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "event_id": {"type": "string", "description": "Google Calendar event ID"},
                "calendar_id": {"type": "string", "description": "Calendar ID (default: 'primary')", "default": "primary"},
                "title": {"type": "string", "description": "New event title"},
                "start_datetime": {
                    "type": "string",
                    "description": "New start datetime (ISO 8601); treated as NZ time if no offset",
                },
                "end_datetime": {
                    "type": "string",
                    "description": "New end datetime (ISO 8601); treated as NZ time if no offset",
                },
                "description": {"type": "string", "description": "New description"},
                "location": {"type": "string", "description": "New location"},
            },
            "required": ["event_id"],
        },
    },
    {
        "name": "delete_calendar_event",
        "description": (
            "Delete a Google Calendar event by event ID. "
            "Always call request_confirmation before deleting — this is irreversible."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "event_id": {"type": "string", "description": "Google Calendar event ID"},
                "calendar_id": {"type": "string", "description": "Calendar ID (default: 'primary')", "default": "primary"},
            },
            "required": ["event_id"],
        },
    },
    {
        "name": "list_calendars",
        "description": (
            "List all of the user's Google Calendars with their names and IDs. "
            "Use when you need a calendar ID other than 'primary'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
]

SCHEDULES = []

_NOT_CONNECTED_MSG = (
    "Google is not connected. Visit the Ki admin page (System tab) to connect your Google account."
)
_CALENDAR_SCOPE_MSG = (
    "Google Calendar is not authorized — your token is missing the calendar scope. "
    "Go to the Ki admin page (System tab), disconnect Google, then reconnect to grant calendar access."
)
_DOCS_SCOPE_MSG = (
    "Google Docs write access is not authorized — your token is missing the documents scope. "
    "Go to the Ki admin page (System tab), disconnect Google, then reconnect to grant Docs access."
)
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1"
_DRIVE_BASE = "https://www.googleapis.com/drive/v3"
_CALENDAR_BASE = "https://www.googleapis.com/calendar/v3"
_DOCS_BASE = "https://docs.googleapis.com/v1"


# ── Token storage ─────────────────────────────────────────────────────────────

async def _get_token_row() -> dict | None:
    """Return the stored token row, or None if not connected."""
    async with SessionLocal() as session:
        result = await session.execute(sa.text(
            "SELECT access_token, refresh_token, token_expiry, email "
            "FROM google_tokens ORDER BY id DESC LIMIT 1"
        ))
        row = result.first()
    if row is None:
        return None
    return {
        "access_token": row.access_token,
        "refresh_token": row.refresh_token,
        "token_expiry": row.token_expiry,
        "email": row.email,
    }


async def _save_tokens(
    access_token: str,
    refresh_token: str,
    token_expiry: datetime,
    email_addr: str | None = None,
) -> None:
    """Upsert tokens into DB as a single row."""
    async with SessionLocal() as session:
        async with session.begin():
            await session.execute(sa.text("DELETE FROM google_tokens"))
            await session.execute(sa.text("""
                INSERT INTO google_tokens (access_token, refresh_token, token_expiry, email, updated_at)
                VALUES (:at, :rt, :expiry, :email, NOW())
            """), {"at": access_token, "rt": refresh_token, "expiry": token_expiry, "email": email_addr})


async def _refresh_token_pair(refresh_token: str) -> tuple[str, datetime]:
    """Exchange a refresh token for a new access token. Returns (access_token, expiry)."""
    async with httpx.AsyncClient(timeout=15.0) as c:
        r = await c.post(_TOKEN_URL, data={
            "grant_type": "refresh_token",
            "client_id": os.environ["GOOGLE_CLIENT_ID"],
            "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
            "refresh_token": refresh_token,
        })
        r.raise_for_status()
        data = r.json()
    expires_in = data.get("expires_in", 3600)
    expiry = datetime.now(timezone.utc) + timedelta(seconds=expires_in - 60)
    return data["access_token"], expiry


async def _get_valid_token() -> str | None:
    """Return a valid access token, auto-refreshing if expired. None if not connected."""
    row = await _get_token_row()
    if row is None:
        return None

    expiry = row["token_expiry"]
    if expiry and expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)

    if expiry is None or datetime.now(timezone.utc) >= expiry:
        try:
            access_token, new_expiry = await _refresh_token_pair(row["refresh_token"])
            await _save_tokens(access_token, row["refresh_token"], new_expiry, row["email"])
            return access_token
        except Exception as e:
            print(f"[google] token refresh failed: {e}")
            return None

    return row["access_token"]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _header(headers: list[dict], name: str) -> str:
    """Extract a header value by name (case-insensitive)."""
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _decode_body(payload: dict) -> str:
    """Recursively extract readable plain text from a Gmail message payload."""
    parts = payload.get("parts", [])
    mime = payload.get("mimeType", "")

    if parts:
        plain = next((p for p in parts if p.get("mimeType") == "text/plain"), None)
        html = next((p for p in parts if p.get("mimeType") == "text/html"), None)
        # Recurse into nested multipart containers first
        for part in parts:
            if part.get("mimeType", "").startswith("multipart/"):
                nested = _decode_body(part)
                if nested:
                    return nested
        chosen = plain or html
        if chosen:
            return _decode_body(chosen)
        return ""

    data = payload.get("body", {}).get("data", "")
    if not data:
        return ""
    try:
        text = base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
        if mime == "text/html":
            text = re.sub(r"<[^>]+>", " ", text)
            text = re.sub(r"\s+", " ", text).strip()
        return text
    except Exception:
        return ""


async def _gmail_get(path: str, params: dict | None = None) -> dict:
    token = await _get_valid_token()
    if token is None:
        raise ValueError("not_connected")
    async with httpx.AsyncClient(timeout=20.0) as c:
        r = await c.get(
            f"{_GMAIL_BASE}{path}",
            headers={"Authorization": f"Bearer {token}"},
            params=params or {},
        )
        r.raise_for_status()
        return r.json()


async def _gmail_post(path: str, body: dict) -> dict:
    token = await _get_valid_token()
    if token is None:
        raise ValueError("not_connected")
    async with httpx.AsyncClient(timeout=20.0) as c:
        r = await c.post(
            f"{_GMAIL_BASE}{path}",
            headers={"Authorization": f"Bearer {token}"},
            json=body,
        )
        r.raise_for_status()
        return r.json()


async def _drive_get(path: str, params: dict | None = None, raw: bool = False):
    token = await _get_valid_token()
    if token is None:
        raise ValueError("not_connected")
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.get(
            f"{_DRIVE_BASE}{path}",
            headers={"Authorization": f"Bearer {token}"},
            params=params or {},
        )
        r.raise_for_status()
        return r.text if raw else r.json()


async def _calendar_get(path: str, params: dict | None = None) -> dict:
    token = await _get_valid_token()
    if token is None:
        raise ValueError("not_connected")
    async with httpx.AsyncClient(timeout=20.0) as c:
        r = await c.get(
            f"{_CALENDAR_BASE}{path}",
            headers={"Authorization": f"Bearer {token}"},
            params=params or {},
        )
        r.raise_for_status()
        return r.json()


async def _calendar_post(path: str, body: dict) -> dict:
    token = await _get_valid_token()
    if token is None:
        raise ValueError("not_connected")
    async with httpx.AsyncClient(timeout=20.0) as c:
        r = await c.post(
            f"{_CALENDAR_BASE}{path}",
            headers={"Authorization": f"Bearer {token}"},
            json=body,
        )
        r.raise_for_status()
        return r.json()


async def _calendar_patch(path: str, body: dict) -> dict:
    token = await _get_valid_token()
    if token is None:
        raise ValueError("not_connected")
    async with httpx.AsyncClient(timeout=20.0) as c:
        r = await c.patch(
            f"{_CALENDAR_BASE}{path}",
            headers={"Authorization": f"Bearer {token}"},
            json=body,
        )
        r.raise_for_status()
        return r.json()


async def _calendar_delete(path: str) -> None:
    token = await _get_valid_token()
    if token is None:
        raise ValueError("not_connected")
    async with httpx.AsyncClient(timeout=20.0) as c:
        r = await c.delete(
            f"{_CALENDAR_BASE}{path}",
            headers={"Authorization": f"Bearer {token}"},
        )
        r.raise_for_status()


async def _docs_get(path: str) -> dict:
    token = await _get_valid_token()
    if token is None:
        raise ValueError("not_connected")
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.get(
            f"{_DOCS_BASE}{path}",
            headers={"Authorization": f"Bearer {token}"},
        )
        r.raise_for_status()
        return r.json()


async def _docs_post(path: str, body: dict) -> dict:
    token = await _get_valid_token()
    if token is None:
        raise ValueError("not_connected")
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.post(
            f"{_DOCS_BASE}{path}",
            headers={"Authorization": f"Bearer {token}"},
            json=body,
        )
        r.raise_for_status()
        return r.json()


# ── Tool implementations ──────────────────────────────────────────────────────

async def _search_gmail(query: str, max_results: int = 10) -> str:
    try:
        data = await _gmail_get(
            "/users/me/messages",
            {"q": query, "maxResults": min(max_results, 50)},
        )
    except ValueError:
        return _NOT_CONNECTED_MSG
    except Exception as e:
        return f"Gmail search failed: {e}"

    msgs = data.get("messages", [])
    if not msgs:
        return f"No emails found for: {query}"

    lines = [f"Found {len(msgs)} email(s) matching '{query}':\n"]
    for ref in msgs:
        try:
            m = await _gmail_get(
                f"/users/me/messages/{ref['id']}",
                {"format": "metadata", "metadataHeaders": "From,Subject,Date"},
            )
            hdrs = m.get("payload", {}).get("headers", [])
            lines.append(
                f"ID: {ref['id']}\n"
                f"From: {_header(hdrs, 'From') or 'unknown'}\n"
                f"Subject: {_header(hdrs, 'Subject') or '(no subject)'}\n"
                f"Date: {_header(hdrs, 'Date') or ''}\n"
            )
        except Exception as e:
            lines.append(f"ID: {ref['id']} (detail fetch failed: {e})\n")

    return "\n".join(lines)


async def _read_email(message_id: str) -> str:
    try:
        msg = await _gmail_get(f"/users/me/messages/{message_id}", {"format": "full"})
    except ValueError:
        return _NOT_CONNECTED_MSG
    except Exception as e:
        return f"Failed to read email {message_id}: {e}"

    payload = msg.get("payload", {})
    hdrs = payload.get("headers", [])
    body = _decode_body(payload)
    body = body[:5000] + ("…" if len(body) > 5000 else "")

    return (
        f"From: {_header(hdrs, 'From')}\n"
        f"To: {_header(hdrs, 'To')}\n"
        f"Date: {_header(hdrs, 'Date')}\n"
        f"Subject: {_header(hdrs, 'Subject')}\n\n"
        f"{body}"
    )


async def _send_email(
    to: str, subject: str, body: str, reply_to_message_id: str | None = None
) -> str:
    try:
        final_subject = subject
        thread_id = None

        if reply_to_message_id:
            try:
                orig = await _gmail_get(
                    f"/users/me/messages/{reply_to_message_id}",
                    {"format": "metadata", "metadataHeaders": "Subject,Message-ID"},
                )
                orig_hdrs = orig.get("payload", {}).get("headers", [])
                orig_subject = _header(orig_hdrs, "Subject")
                orig_msg_id = _header(orig_hdrs, "Message-ID")
                if orig_subject and not subject.lower().startswith("re:"):
                    final_subject = f"Re: {orig_subject}"
                thread_id = orig.get("threadId")
            except Exception as e:
                print(f"[google] reply context fetch failed: {e}")

        msg = MIMEText(body, "plain")
        msg["to"] = to
        msg["subject"] = final_subject
        if reply_to_message_id:
            try:
                orig_hdrs = (await _gmail_get(
                    f"/users/me/messages/{reply_to_message_id}",
                    {"format": "metadata", "metadataHeaders": "Message-ID"},
                )).get("payload", {}).get("headers", [])
                orig_msg_id = _header(orig_hdrs, "Message-ID")
                if orig_msg_id:
                    msg["In-Reply-To"] = orig_msg_id
                    msg["References"] = orig_msg_id
            except Exception:
                pass

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
        send_body: dict = {"raw": raw}
        if thread_id:
            send_body["threadId"] = thread_id

        await _gmail_post("/users/me/messages/send", send_body)
        return f"Email sent to {to} — subject: {final_subject}"

    except ValueError:
        return _NOT_CONNECTED_MSG
    except Exception as e:
        return f"Failed to send email: {e}"


async def _draft_email(to: str, subject: str, body: str) -> str:
    try:
        msg = MIMEText(body, "plain")
        msg["to"] = to
        msg["subject"] = subject
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
        result = await _gmail_post("/users/me/drafts", {"message": {"raw": raw}})
        draft_id = result.get("id", "unknown")
        return f"Draft created (ID: {draft_id}) — to: {to}, subject: {subject}"
    except ValueError:
        return _NOT_CONNECTED_MSG
    except Exception as e:
        return f"Failed to create draft: {e}"


async def _list_recent_emails(max_results: int = 20) -> str:
    try:
        data = await _gmail_get(
            "/users/me/messages",
            {"labelIds": "INBOX", "maxResults": min(max_results, 50)},
        )
    except ValueError:
        return _NOT_CONNECTED_MSG
    except Exception as e:
        return f"Failed to list inbox: {e}"

    msgs = data.get("messages", [])
    if not msgs:
        return "Inbox is empty."

    lines = [f"Recent inbox ({len(msgs)} emails):\n"]
    for ref in msgs:
        try:
            m = await _gmail_get(
                f"/users/me/messages/{ref['id']}",
                {"format": "metadata", "metadataHeaders": "From,Subject,Date"},
            )
            hdrs = m.get("payload", {}).get("headers", [])
            lines.append(
                f"ID: {ref['id']}\n"
                f"From: {_header(hdrs, 'From') or 'unknown'}\n"
                f"Subject: {_header(hdrs, 'Subject') or '(no subject)'}\n"
                f"Date: {_header(hdrs, 'Date') or ''}\n"
            )
        except Exception as e:
            lines.append(f"ID: {ref['id']} (error: {e})\n")

    return "\n".join(lines)


async def _search_drive(query: str, max_results: int = 10) -> str:
    safe_q = query.replace("'", "\\'")
    drive_q = f"(name contains '{safe_q}' or fullText contains '{safe_q}') and trashed = false"
    try:
        data = await _drive_get("/files", {
            "q": drive_q,
            "pageSize": min(max_results, 50),
            "fields": "files(id,name,mimeType,modifiedTime,size)",
            "orderBy": "modifiedTime desc",
        })
    except ValueError:
        return _NOT_CONNECTED_MSG
    except Exception as e:
        return f"Drive search failed: {e}"

    files = data.get("files", [])
    if not files:
        return f"No Drive files found matching: {query}"

    lines = [f"Found {len(files)} file(s) matching '{query}':\n"]
    for f in files:
        modified = (f.get("modifiedTime") or "")[:10]
        size = f.get("size", "")
        size_str = f" ({int(size):,} bytes)" if size else ""
        lines.append(
            f"ID: {f['id']}\n"
            f"Name: {f['name']}\n"
            f"Type: {f['mimeType']}\n"
            f"Modified: {modified}{size_str}\n"
        )

    return "\n".join(lines)


_GAPPS_EXPORT_TYPES = {
    "application/vnd.google-apps.document":     "text/plain",
    "application/vnd.google-apps.spreadsheet":  "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
}


async def _read_drive_file(file_id: str) -> str:
    try:
        meta = await _drive_get(f"/files/{file_id}", {"fields": "id,name,mimeType"})
        mime = meta.get("mimeType", "")
        name = meta.get("name", "file")

        if mime in _GAPPS_EXPORT_TYPES:
            export_mime = _GAPPS_EXPORT_TYPES[mime]
            content = await _drive_get(
                f"/files/{file_id}/export", {"mimeType": export_mime}, raw=True
            )
        elif mime.startswith("text/") or mime == "application/json":
            content = await _drive_get(f"/files/{file_id}", {"alt": "media"}, raw=True)
        else:
            return (
                f"Cannot read '{name}' (type: {mime}). "
                "Readable types: Google Docs, Sheets, Presentations, and plain text files."
            )

        content = content[:8000] + ("…" if len(content) > 8000 else "")
        return f"File: {name}\nType: {mime}\n\n{content}"

    except ValueError:
        return _NOT_CONNECTED_MSG
    except Exception as e:
        return f"Failed to read Drive file {file_id}: {e}"


async def _list_drive_files(folder_id: str | None = None, max_results: int = 20) -> str:
    q = (
        f"'{folder_id}' in parents and trashed = false"
        if folder_id
        else "trashed = false"
    )
    try:
        data = await _drive_get("/files", {
            "q": q,
            "pageSize": min(max_results, 100),
            "fields": "files(id,name,mimeType,modifiedTime)",
            "orderBy": "modifiedTime desc",
        })
    except ValueError:
        return _NOT_CONNECTED_MSG
    except Exception as e:
        return f"Failed to list Drive files: {e}"

    files = data.get("files", [])
    if not files:
        return "No files found."

    lines = [f"Drive files ({len(files)}):\n"]
    for f in files:
        modified = (f.get("modifiedTime") or "")[:10]
        lines.append(
            f"ID: {f['id']}\n"
            f"Name: {f['name']}\n"
            f"Type: {f['mimeType']}\n"
            f"Modified: {modified}\n"
        )

    return "\n".join(lines)


def _parse_nz_dt(dt_str: str) -> str:
    """Return RFC3339 datetime string; if no tz offset, treats as user's local time."""
    if not dt_str:
        return dt_str
    if "+" in dt_str or dt_str.endswith("Z"):
        return dt_str
    try:
        return datetime.fromisoformat(dt_str).replace(tzinfo=ZoneInfo(_USER_TZ_NAME)).isoformat()
    except Exception:
        return dt_str


def _fmt_event(event: dict) -> str:
    title = event.get("summary", "(no title)")
    start = event.get("start", {})
    end = event.get("end", {})
    start_str = start.get("dateTime") or start.get("date") or ""
    end_str = end.get("dateTime") or end.get("date") or ""
    location = event.get("location", "")
    raw_desc = event.get("description") or ""
    snippet = raw_desc[:150] + ("…" if len(raw_desc) > 150 else "")

    line = f"• {title}\n  Start: {start_str}\n  End: {end_str}"
    if location:
        line += f"\n  Location: {location}"
    if snippet:
        line += f"\n  Description: {snippet}"
    return line


async def _get_upcoming_events(
    calendar_id: str = "primary", days: int = 7, max_results: int = 20
) -> str:
    now = datetime.now(timezone.utc)
    time_min = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    time_max = (now + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        data = await _calendar_get(
            f"/calendars/{calendar_id}/events",
            {
                "timeMin": time_min,
                "timeMax": time_max,
                "singleEvents": "true",
                "orderBy": "startTime",
                "maxResults": min(max_results, 50),
            },
        )
    except ValueError:
        return _NOT_CONNECTED_MSG
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 403:
            return _CALENDAR_SCOPE_MSG
        return f"Calendar request failed: {e}"
    except Exception as e:
        return f"Failed to get upcoming events: {e}"

    items = data.get("items", [])
    if not items:
        return f"No events found in the next {days} day(s)."

    lines = [f"Upcoming events ({len(items)}) in the next {days} day(s):\n"]
    lines.extend(_fmt_event(ev) for ev in items)
    return "\n\n".join(lines)


async def _search_calendar(
    query: str,
    start_date: str | None = None,
    end_date: str | None = None,
    calendar_id: str = "primary",
    max_results: int = 20,
) -> str:
    params: dict = {
        "q": query,
        "singleEvents": "true",
        "orderBy": "startTime",
        "maxResults": min(max_results, 50),
    }
    if start_date:
        params["timeMin"] = f"{start_date}T00:00:00Z"
    if end_date:
        params["timeMax"] = f"{end_date}T23:59:59Z"

    try:
        data = await _calendar_get(f"/calendars/{calendar_id}/events", params)
    except ValueError:
        return _NOT_CONNECTED_MSG
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 403:
            return _CALENDAR_SCOPE_MSG
        return f"Calendar search failed: {e}"
    except Exception as e:
        return f"Calendar search failed: {e}"

    items = data.get("items", [])
    if not items:
        return f"No calendar events found matching: {query}"

    lines = [f"Found {len(items)} event(s) matching '{query}':\n"]
    for ev in items:
        start = ev.get("start", {})
        start_str = start.get("dateTime") or start.get("date") or ""
        end = ev.get("end", {})
        end_str = end.get("dateTime") or end.get("date") or ""
        location = ev.get("location", "")
        line = (
            f"ID: {ev.get('id', '')}\n"
            f"Title: {ev.get('summary', '(no title)')}\n"
            f"Start: {start_str}\n"
            f"End: {end_str}"
        )
        if location:
            line += f"\nLocation: {location}"
        lines.append(line)

    return "\n\n".join(lines)


async def _create_calendar_event(
    title: str,
    start_datetime: str,
    end_datetime: str,
    description: str | None = None,
    location: str | None = None,
    attendees: list[str] | None = None,
    calendar_id: str = "primary",
) -> str:
    body: dict = {
        "summary": title,
        "start": {"dateTime": _parse_nz_dt(start_datetime), "timeZone": _USER_TZ_NAME},
        "end": {"dateTime": _parse_nz_dt(end_datetime), "timeZone": _USER_TZ_NAME},
    }
    if description:
        body["description"] = description
    if location:
        body["location"] = location
    if attendees:
        body["attendees"] = [{"email": e} for e in attendees]

    try:
        result = await _calendar_post(f"/calendars/{calendar_id}/events", body)
    except ValueError:
        return _NOT_CONNECTED_MSG
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 403:
            return _CALENDAR_SCOPE_MSG
        return f"Failed to create calendar event: {e}"
    except Exception as e:
        return f"Failed to create calendar event: {e}"

    event_id = result.get("id", "unknown")
    link = result.get("htmlLink", "")
    return f"Event created: '{title}'\nID: {event_id}\nLink: {link}"


async def _update_calendar_event(
    event_id: str,
    calendar_id: str = "primary",
    title: str | None = None,
    start_datetime: str | None = None,
    end_datetime: str | None = None,
    description: str | None = None,
    location: str | None = None,
) -> str:
    try:
        _event = await _calendar_get(f"/calendars/{calendar_id}/events/{event_id}")
    except ValueError:
        return _NOT_CONNECTED_MSG
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 403:
            return _CALENDAR_SCOPE_MSG
        return f"Failed to fetch event {event_id}: {e}"
    except Exception as e:
        return f"Failed to fetch event {event_id}: {e}"

    patch: dict = {}
    if title is not None:
        patch["summary"] = title
    if description is not None:
        patch["description"] = description
    if location is not None:
        patch["location"] = location
    if start_datetime is not None:
        patch["start"] = {"dateTime": _parse_nz_dt(start_datetime), "timeZone": _USER_TZ_NAME}
    if end_datetime is not None:
        patch["end"] = {"dateTime": _parse_nz_dt(end_datetime), "timeZone": _USER_TZ_NAME}

    if not patch:
        return "No changes specified."

    try:
        result = await _calendar_patch(f"/calendars/{calendar_id}/events/{event_id}", patch)
    except ValueError:
        return _NOT_CONNECTED_MSG
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 403:
            return _CALENDAR_SCOPE_MSG
        return f"Failed to update event {event_id}: {e}"
    except Exception as e:
        return f"Failed to update event {event_id}: {e}"

    updated_title = result.get("summary", event_id)
    link = result.get("htmlLink", "")
    return f"Event updated: '{updated_title}'\nID: {event_id}\nLink: {link}"


async def _delete_calendar_event(event_id: str, calendar_id: str = "primary") -> str:
    try:
        await _calendar_delete(f"/calendars/{calendar_id}/events/{event_id}")
    except ValueError:
        return _NOT_CONNECTED_MSG
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 403:
            return _CALENDAR_SCOPE_MSG
        return f"Failed to delete event {event_id}: {e}"
    except Exception as e:
        return f"Failed to delete event {event_id}: {e}"

    return f"Event {event_id} deleted."


async def _list_calendars() -> str:
    try:
        data = await _calendar_get("/users/me/calendarList")
    except ValueError:
        return _NOT_CONNECTED_MSG
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 403:
            return _CALENDAR_SCOPE_MSG
        return f"Failed to list calendars: {e}"
    except Exception as e:
        return f"Failed to list calendars: {e}"

    items = data.get("items", [])
    if not items:
        return "No calendars found."

    lines = [f"Calendars ({len(items)}):\n"]
    for cal in items:
        name = cal.get("summary", "(unnamed)")
        cal_id = cal.get("id", "")
        primary = " (primary)" if cal.get("primary") else ""
        lines.append(f"• {name}{primary}\n  ID: {cal_id}")

    return "\n\n".join(lines)


async def _update_google_doc(file_id: str, content: str) -> str:
    try:
        doc = await _docs_get(f"/documents/{file_id}")
    except ValueError:
        return _NOT_CONNECTED_MSG
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 403:
            return _DOCS_SCOPE_MSG
        return f"Failed to read document {file_id}: {e}"
    except Exception as e:
        return f"Failed to read document {file_id}: {e}"

    title = doc.get("title", file_id)
    body_content = doc.get("body", {}).get("content", [])
    doc_end = body_content[-1].get("endIndex", 2) if body_content else 2

    requests = []
    # Delete all existing content except the required final paragraph marker (index doc_end-1)
    if doc_end > 2:
        requests.append({
            "deleteContentRange": {
                "range": {"startIndex": 1, "endIndex": doc_end - 1}
            }
        })
    requests.append({
        "insertText": {
            "location": {"index": 1},
            "text": content,
        }
    })

    try:
        await _docs_post(f"/documents/{file_id}:batchUpdate", {"requests": requests})
    except ValueError:
        return _NOT_CONNECTED_MSG
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 403:
            return _DOCS_SCOPE_MSG
        return f"Failed to update document '{title}': {e}"
    except Exception as e:
        return f"Failed to update document '{title}': {e}"

    return f"Document '{title}' updated successfully ({len(content):,} characters written)."


async def run_tool(name: str, input: dict) -> str:
    if name == "search_gmail":
        return await _search_gmail(input["query"], input.get("max_results", 10))
    if name == "read_email":
        return await _read_email(input["message_id"])
    if name == "send_email":
        return await _send_email(
            input["to"], input["subject"], input["body"],
            input.get("reply_to_message_id"),
        )
    if name == "draft_email":
        return await _draft_email(input["to"], input["subject"], input["body"])
    if name == "list_recent_emails":
        return await _list_recent_emails(input.get("max_results", 20))
    if name == "search_drive":
        return await _search_drive(input["query"], input.get("max_results", 10))
    if name == "read_drive_file":
        return await _read_drive_file(input["file_id"])
    if name == "list_drive_files":
        return await _list_drive_files(input.get("folder_id"), input.get("max_results", 20))
    if name == "update_google_doc":
        return await _update_google_doc(input["file_id"], input["content"])
    if name == "get_upcoming_events":
        return await _get_upcoming_events(
            input.get("calendar_id", "primary"),
            input.get("days", 7),
            input.get("max_results", 20),
        )
    if name == "search_calendar":
        return await _search_calendar(
            input["query"],
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
    return f"Unknown Google tool: {name}"
