"""
Heartbeat — runs every HEARTBEAT_HOURS hours.
Synthesises a structured snapshot of the user's current state from recent
conversations, stores it in Postgres, and optionally sends a proactive
WhatsApp message if there's something worth flagging.
"""
import asyncio
import json
import os
import re
import traceback
from datetime import datetime, timezone, timedelta, date as date_type
from pathlib import Path
from zoneinfo import ZoneInfo

import anthropic
import httpx

from . import memory as mem
from .vector_store import client as qdrant

PROMPTS_DIR = Path(__file__).parent.parent / "prompts"

# Scheduled tasks are paused until this date (inclusive). Set to None to resume immediately.
SCHEDULED_TASKS_PAUSED_UNTIL: date_type | None = None


def _load_heartbeat_prompt_template() -> str:
    """Load the heartbeat system prompt template from api/prompts/heartbeat.md."""
    path = PROMPTS_DIR / "heartbeat.md"
    if path.exists():
        from .config import render
        return render(path.read_text(encoding="utf-8").strip())
    raise FileNotFoundError(f"Heartbeat prompt not found at {path}")

HAIKU = "claude-haiku-4-5-20251001"
SONNET = "claude-sonnet-4-6"
OBSIDIAN_COLLECTION = "obsidian"
BAILEYS_URL = os.environ.get("BAILEYS_URL", "http://baileys:3000")
INTERNAL_SECRET = os.environ.get("INTERNAL_SECRET", "")
PROACTIVE_COOLDOWN_HOURS = 2
QUIET_START = int(os.environ.get("HEARTBEAT_QUIET_START", "22"))  # 10pm
QUIET_END = int(os.environ.get("HEARTBEAT_QUIET_END", "7"))       # 7am
USER_TZ = ZoneInfo((os.environ.get("USER_TZ") or "UTC"))

_client = anthropic.AsyncAnthropic(max_retries=3)

# Safe empty state returned when synthesis or JSON parsing fails.
_EMPTY_STATE: dict = {
    "open_todos": [],
    "backlog_todos": [],
    "open_loops": [],
    "ki_commitments": [],
    "current_projects": [],
    "recent_themes": [],
    "things_to_mention": [],
    "snoozed": [],
    "proactive_message": None,
    "energy_level": None,
}


def _todo_still_open_heartbeat(vault_path: str, filename: str, content: str) -> bool:
    """
    Ground-truth check used by the heartbeat: return True only if *content*
    (an indexed todo string of the form "Open todo (YYYY-MM-DD): <text>")
    is still present as an unchecked "- [ ]" item in the source file right now.

    Ticked items (- [x] / - [X]) or missing files return False so that
    completed tasks are never surfaced in the heartbeat open-todo list.

    Scanning rule:
    - Read every line of the source file.
    - Strip the "Open todo (date): " prefix to get the raw todo text.
    - For each line that contains the raw text:
        * If the line starts with "- [ ]" (open checkbox) → return True immediately.
        * If the line starts with "- [x]" or "- [X]" (ticked checkbox) → return
          False immediately. A ticked item is done, full stop.
    - Text found only in non-checkbox prose → keep scanning.
    - Conservative default: if no open-checkbox line is found → return False.
    """
    import re as _re
    from pathlib import Path as _Path
    path = _Path(vault_path) / filename
    if not path.exists():
        return False
    try:
        file_text = path.read_text(errors="replace")
    except Exception:
        return False

    # Strip the indexer-added prefix to recover the raw todo text
    raw = _re.sub(r'^Open todo \(\d{4}-\d{2}-\d{2}\):\s*', '', content).strip()
    if not raw:
        return False

    raw_lower = raw.lower()
    for line in file_text.splitlines():
        stripped = line.strip()
        if raw_lower not in stripped.lower():
            continue
        if _re.match(r'^[-*+]\s+\[ \]\s+', stripped):
            return True   # still open — confirmed
        if _re.match(r'^[-*+]\s+\[[xX]\]\s+', stripped):
            return False  # ticked off — done, stop immediately
        # Text found in non-checkbox prose — keep scanning

    # No open-checkbox line found → done or deleted
    return False


async def _fetch_vault_todos() -> tuple[str, list[str]]:
    """
    Return (formatted string, raw content list) of all genuinely open vault
    todos.

    Every Qdrant point returned is verified against the live source file on
    disk. Items whose checkbox has been ticked off (- [x] / - [X]) are
    silently filtered out so the heartbeat never includes completed tasks.
    """
    from qdrant_client.models import Filter, FieldCondition, MatchValue
    import os as _os
    vault_path = _os.environ.get("OBSIDIAN_VAULT_PATH", "/vault")
    try:
        points, _ = await qdrant.scroll(
            collection_name=OBSIDIAN_COLLECTION,
            scroll_filter=Filter(must=[FieldCondition(key="doc_type", match=MatchValue(value="todo"))]),
            limit=500,  # fetch more since we'll filter some out
            with_payload=True,
            with_vectors=False,
        )
        if not points:
            return "No open vault todos found.", []
        lines = []
        contents = []
        seen_content: set[str] = set()
        skipped = 0
        for p in points:
            date = p.payload.get("date", "")
            content = p.payload.get("content", "").strip()
            filename = p.payload.get("filename", "")
            if "#routine" in content:
                continue
            if content in seen_content:
                continue
            # Hard check: verify the item is still open in the source file.
            # Items with no filename are unverifiable — drop them too.
            if not filename or not _todo_still_open_heartbeat(vault_path, filename, content):
                skipped += 1
                continue
            seen_content.add(content)
            contents.append(content)
            lines.append(f"[{date}] {content}" if date else content)
        if skipped:
            print(f"[heartbeat] filtered out {skipped} completed/stale todo(s) from vault")
        if not lines:
            return "No open vault todos found.", []
        return "\n".join(lines), contents
    except Exception as e:
        print(f"[heartbeat] vault todo fetch failed: {e}")
        return "Vault todos unavailable.", []


def _detect_vault_removals(current_contents: list[str], previous_state: dict | None) -> list[str]:
    """Return todos that were in the previous vault snapshot but are now gone (completed or deleted)."""
    if not previous_state:
        return []
    prev_snapshot = set(previous_state.get("vault_todo_snapshot", []))
    current = set(current_contents)
    return list(prev_snapshot - current)


def _update_item_metadata(state: dict, previous_state: dict | None, today: str) -> None:
    """Track first_seen date and mention_count for todos, loops, and commitments. Mutates state."""
    prev_meta = (previous_state or {}).get("item_metadata", {})
    new_meta = {}
    all_items = (
        state.get("open_todos", [])
        + state.get("backlog_todos", [])
        + state.get("open_loops", [])
        + state.get("ki_commitments", [])
        + state.get("current_projects", [])
    )
    for item in all_items:
        if item in prev_meta:
            new_meta[item] = {
                "first_seen": prev_meta[item]["first_seen"],
                "mention_count": prev_meta[item].get("mention_count", 1) + 1,
            }
        else:
            new_meta[item] = {"first_seen": today, "mention_count": 1}
    state["item_metadata"] = new_meta


def _format_conversations(turns: list[dict]) -> str:
    if not turns:
        return "No conversations in this period."
    lines = []
    for t in turns:
        prefix = "You" if t["role"] == "user" else "Ki"
        lines.append(f"{prefix}: {t['content']}")
    return "\n".join(lines)


def _build_verified_open_set(vault_contents: list[str]) -> set[str]:
    """
    Build a set of the raw todo texts that are currently open in the vault,
    with the 'Open todo (YYYY-MM-DD): ' prefix stripped.  Used for the
    post-synthesis hard guard that evicts completed items the LLM may have
    carried forward from the previous state.
    """
    verified: set[str] = set()
    _prefix_re = re.compile(r'^Open todo \(\d{4}-\d{2}-\d{2}\):\s*')
    for content in vault_contents:
        raw = _prefix_re.sub('', content).strip()
        if raw:
            verified.add(raw.lower())
    return verified


async def _fetch_scheduled_tasks_due() -> str:
    """
    Read task-config.yaml from the vault and return a formatted block
    listing any overdue or due-soon tracked tasks (weekly, fortnightly, interval).
    Daily/weekday tasks are skipped — they're routine and injected every day.
    Returns empty string on any error or when nothing is due.
    """
    if SCHEDULED_TASKS_PAUSED_UNTIL is not None:
        from datetime import datetime as _dt
        if _dt.now(USER_TZ).date() < SCHEDULED_TASKS_PAUSED_UNTIL:
            return ""

    import os as _os
    import re as _re
    from pathlib import Path as _Path
    from .tools.obsidian import _parse_task_config, _parse_interval_days, _try_parse_date

    vault_path = _os.environ.get("OBSIDIAN_VAULT_PATH", "/vault")
    config_path = _Path(vault_path) / "config" / "task-config.yaml"
    if not config_path.exists():
        return ""

    try:
        config_text = config_path.read_text(errors="replace")
        config = _parse_task_config(config_text)
        if not config or not isinstance(config, dict):
            return ""
    except Exception as e:
        print(f"[heartbeat] task config read failed (non-fatal): {e}")
        return ""

    today_local = datetime.now(USER_TZ).date()
    overdue: list[str] = []
    due_soon: list[str] = []

    def _check(task_str: str, last_done_str: str, interval_days: int) -> None:
        last_done = _try_parse_date(last_done_str) if last_done_str not in ("never", "", None) else None
        due_date = today_local if last_done is None else (last_done + timedelta(days=interval_days))
        days_until = (due_date - today_local).days
        if days_until < 0:
            overdue.append(f"{task_str} (overdue by {-days_until}d)")
        elif days_until <= 3:
            due_soon.append(f"{task_str} (due in {days_until}d)")

    # Weekly tasks — interval 7 days
    for entry in (config.get("weekly") or []):
        if isinstance(entry, dict) and entry.get("task") and "last_done" in entry:
            _check(entry["task"], entry.get("last_done", "never"), 7)

    # Fortnightly tasks — interval 14 days
    fortnightly = config.get("fortnightly", {})
    if isinstance(fortnightly, dict):
        for item in (fortnightly.get("tasks") or []):
            if isinstance(item, dict) and item.get("task"):
                _check(item["task"], item.get("last_done", "never"), 14)

    # Interval tasks — custom interval (e.g. "10 weeks", "6 months")
    for item in (config.get("interval") or []):
        if not isinstance(item, dict):
            continue
        task_str = item.get("task", "")
        every_str = item.get("every", "")
        if not task_str or not every_str:
            continue
        interval_days = _parse_interval_days(every_str)
        if interval_days is None:
            continue
        _check(task_str, item.get("last_done", "never"), interval_days)

    if not overdue and not due_soon:
        return ""

    lines: list[str] = []
    if overdue:
        lines.append("Overdue:")
        lines.extend(f"  - {t}" for t in overdue)
    if due_soon:
        lines.append("Due within 3 days:")
        lines.extend(f"  - {t}" for t in due_soon)
    return "\n".join(lines)


async def _synthesise(
    recent_convos: str,
    extended_convos: str,
    vault_todos: str,
    vault_removed: list[str],
    previous_state: dict | None,
    pending_reminders: list[str] | None = None,
    vault_contents: list[str] | None = None,
    calendar_events: str | None = None,
    scheduled_tasks_block: str | None = None,
    recent_12h_text: str = "",
) -> dict:
    prev = json.dumps(previous_state, indent=2) if previous_state else "None yet."
    today = datetime.now(USER_TZ).strftime("%Y-%m-%d")
    today_display = datetime.now(USER_TZ).strftime("%A, %d %B %Y")

    # Build an explicit block of currently-active snoozed items so Haiku can't miss them
    active_snoozes = []
    if previous_state:
        for s in previous_state.get("snoozed", []):
            ra = s.get("resurface_after")
            if ra and ra <= today:
                continue  # expired — eligible to resurface
            active_snoozes.append(s.get("item", ""))
    snooze_block = ""
    if active_snoozes:
        snooze_block = "\n\nACTIVELY SNOOZED ITEMS — HARD RULE: These MUST NOT appear in things_to_mention. Do not raise them, hint at them, or reference them in the proactive message under any circumstances:\n" + "\n".join(f"- {s}" for s in active_snoozes)

    # Build reminder suppression block — items the user has already set a reminder for
    # should NEVER appear in the heartbeat message; the reminder itself will handle them.
    reminder_block = ""
    if pending_reminders:
        reminder_block = (
            "\n\nPENDING REMINDERS — HARD RULE: The user has already set a reminder for each of these. "
            "Do NOT surface them in things_to_mention, do NOT mention them in the proactive_message, "
            "and do NOT include them in open_loops or ki_commitments. "
            "The reminder will handle them — the heartbeat must not double-nag:\n"
            + "\n".join(f"- {r}" for r in pending_reminders)
        )

    vault_removed_block = ""
    if vault_removed:
        vault_removed_block = "\n\nVault todos removed since last heartbeat (completed or deleted by user):\n" + "\n".join(f"- {t}" for t in vault_removed)
        vault_removed_block += "\nFor each removed todo: remove it from open_todos. If it was in open_loops or ki_commitments, close that loop. If it looks like a genuine accomplishment, you may note it briefly in recent_themes."

    # Build staleness context for items that have been around a long time
    stale_items: set[str] = set()
    prev_meta = (previous_state or {}).get("item_metadata", {})
    for item, meta in prev_meta.items():
        first_seen = meta.get("first_seen", "")
        try:
            days_old = (date_type.fromisoformat(today) - date_type.fromisoformat(first_seen)).days
            if days_old >= 14:
                stale_items.add(item)
        except Exception:
            pass

    stale_loops: set[str] = set()
    stale_projects: set[str] = set()
    for item, meta in prev_meta.items():
        first_seen = meta.get("first_seen", "")
        try:
            days_old = (date_type.fromisoformat(today) - date_type.fromisoformat(first_seen)).days
            if days_old >= 21:
                # Loops and projects get a longer grace period than todos
                if item in (previous_state or {}).get("open_loops", []):
                    stale_loops.add(item)
                if item in (previous_state or {}).get("current_projects", []):
                    stale_projects.add(item)
        except Exception:
            pass

    stale_block = ""
    stale_parts = []
    if stale_items:
        stale_parts.append(
            "TODOS OPEN FOR 14+ DAYS — move to backlog_todos unless there is clear evidence of active work or urgency:\n"
            + "\n".join(f"- {s}" for s in sorted(stale_items))
        )
    if stale_loops:
        stale_parts.append(
            "LOOPS OPEN FOR 21+ DAYS — these are probably resolved or irrelevant. DROP them from open_loops unless "
            "there is clear evidence in recent conversations that they are still genuinely unresolved:\n"
            + "\n".join(f"- {s}" for s in sorted(stale_loops))
        )
    if stale_projects:
        stale_parts.append(
            "PROJECTS TRACKED FOR 21+ DAYS WITH NO RECENT ACTIVITY — if there is no mention of active work on these "
            "in recent conversations, move them to recent_themes or drop them from current_projects entirely:\n"
            + "\n".join(f"- {s}" for s in sorted(stale_projects))
        )
    if stale_parts:
        stale_block = "\n\n" + "\n\n".join(stale_parts)

    # Build calendar context block if events were fetched
    calendar_block = ""
    if calendar_events and calendar_events.strip():
        calendar_block = (
            "\n\nCalendar — today and tomorrow (include any time-sensitive meetings or "
            "appointments in things_to_mention if the user needs to prepare or act):\n"
            + calendar_events
        )

    # Build scheduled tasks block for tracked tasks that are overdue or due soon
    scheduled_block = ""
    if scheduled_tasks_block and scheduled_tasks_block.strip():
        scheduled_block = (
            "\n\nScheduled task schedule — overdue or due within 3 days (surface relevant ones "
            "in things_to_mention if they haven't been mentioned recently):\n"
            + scheduled_tasks_block
        )

    # Build the 12-hour suppression block — topics discussed in the last 12 hours that
    # the model must not re-raise in things_to_mention unless there's a hard 24h deadline.
    recent_12h_suppression_block = ""
    _no_convos = "No conversations in this period."
    if recent_12h_text and recent_12h_text.strip() and recent_12h_text.strip() != _no_convos:
        recent_12h_suppression_block = (
            "\n\nHARD SUPPRESSION RULE — DO NOT RE-RAISE RECENTLY DISCUSSED TOPICS:\n"
            "The following conversations happened in the last 12 hours. The user ALREADY KNOWS "
            "everything in them. You MUST NOT put any of these topics in things_to_mention "
            "UNLESS all three conditions are true:\n"
            "  1. The item has a hard deadline or required action within the next 24 hours\n"
            "  2. The item genuinely cannot wait until the next conversation\n"
            "  3. The item text itself includes explicit rationale — WHY it must be raised RIGHT NOW "
            "despite being recently discussed (e.g. \"Ki costs — raising because Anthropic bill due tonight\") "
            "— not just the topic name\n"
            "Parroting back what was just discussed is the single most useless thing the heartbeat can do. "
            "When in doubt: LEAVE IT OUT.\n\n"
            + recent_12h_text
        )

    # Load the prompt template from file and substitute runtime values.
    # The file uses {placeholder} syntax for the dynamic sections.
    _tmpl = _load_heartbeat_prompt_template()
    # Strip the human-readable "Note:" section at the bottom (everything after "---")
    _tmpl_body = _tmpl.split("\n---\n")[0].strip()

    prompt = (
        _tmpl_body
        .replace("{today_display}", today_display)
        .replace("{today}", today)
        .replace("{snooze_block}", snooze_block)
        .replace("{reminder_block}", reminder_block)
        .replace("{vault_removed_block}", vault_removed_block)
        .replace("{stale_block}", stale_block)
        .replace("{recent_convos}", recent_convos)
        .replace("{recent_12h_suppression_block}", recent_12h_suppression_block)
        .replace("{extended_convos}", extended_convos)
        .replace("{vault_todos}", vault_todos)
        .replace("{calendar_block}", calendar_block)
        .replace("{scheduled_tasks_block}", scheduled_block)
        .replace("{prev}", prev)
    )

    resp = await _client.messages.create(
        model=SONNET,
        max_tokens=6144,
        messages=[{"role": "user", "content": prompt}],
    )
    await mem.log_token_usage(SONNET, resp.usage.input_tokens, resp.usage.output_tokens, use_case="heartbeat")

    text = resp.content[0].text.strip()
    # Strip markdown fences if the model wrapped the JSON
    if text.startswith("```"):
        text = re.sub(r'^```[a-z]*\n?', '', text)
        text = re.sub(r'\n?```$', '', text)
    text = text.strip()

    try:
        state = json.loads(text)
    except json.JSONDecodeError as exc:
        preview = text[:500] + ("…" if len(text) > 500 else "")
        stop_reason = getattr(resp, "stop_reason", "unknown")
        print(
            f"[heartbeat] ⚠️  JSON parse failed — stop_reason={stop_reason!r} "
            f"input_tokens={resp.usage.input_tokens} output_tokens={resp.usage.output_tokens} "
            f"exc={exc} | raw response (first 500 chars): {preview!r}"
        )
        # Return a safe default so the caller can still persist / return something
        # meaningful rather than crashing or silently returning nothing.
        fallback = dict(_EMPTY_STATE)
        fallback["last_updated"] = datetime.now(USER_TZ).isoformat()
        fallback["_parse_error"] = str(exc)
        fallback["_raw_preview"] = preview
        return fallback

    for key in ("open_todos", "backlog_todos", "open_loops", "ki_commitments", "current_projects", "recent_themes", "things_to_mention", "snoozed"):
        state.setdefault(key, [])
    state.setdefault("proactive_message", None)
    state.setdefault("energy_level", None)
    state["last_updated"] = datetime.now(USER_TZ).isoformat()

    # Hard post-synthesis guard: evict completed todos the LLM may have carried
    # forward from prev state.  We have the verified live set from _fetch_vault_todos;
    # any open_todo / backlog_todo whose text does NOT appear there is treated as done.
    #
    # Match strategy: case-insensitive substring — an open_todos entry like
    # "Book dentist appointment" matches a vault content string like
    # "Open todo (2025-01-15): Book dentist appointment".
    if vault_contents:
        verified_open = _build_verified_open_set(vault_contents)
        evicted_open = []
        kept_open = []
        for item in state.get("open_todos", []):
            # Accept the item if any verified open todo contains its text (case-insensitive).
            item_lower = item.lower()
            if any(item_lower in v or v in item_lower for v in verified_open):
                kept_open.append(item)
            else:
                evicted_open.append(item)
        if evicted_open:
            print(f"[heartbeat] post-synthesis: evicted {len(evicted_open)} completed/stale open_todo(s): {evicted_open}")
            # Null the proactive_message — the LLM text may reference these completed items.
            # It will be rebuilt cleanly from things_to_mention at send time.
            state["proactive_message"] = None
        state["open_todos"] = kept_open

        # Same check for backlog_todos — they came from the vault too.
        evicted_backlog = []
        kept_backlog = []
        for item in state.get("backlog_todos", []):
            item_lower = item.lower()
            if any(item_lower in v or v in item_lower for v in verified_open):
                kept_backlog.append(item)
            else:
                evicted_backlog.append(item)
        if evicted_backlog:
            print(f"[heartbeat] post-synthesis: evicted {len(evicted_backlog)} completed/stale backlog_todo(s): {evicted_backlog}")
            # Null the proactive_message for the same reason.
            state["proactive_message"] = None
        state["backlog_todos"] = kept_backlog

        # Hard guard: also evict things_to_mention entries that appear to be
        # completed vault todos.  The LLM sometimes copies a resolved todo
        # directly into things_to_mention even though it's no longer in the
        # verified open set.  Any things_to_mention entry whose text closely
        # matches a vault todo item (i.e. one of the Qdrant-indexed strings)
        # but does NOT appear in verified_open must be dropped.
        #
        # We build a separate set of ALL vault todo raw texts (open AND
        # recently-ticked — but since verified_open only contains currently-
        # open ones, anything that matches a vault-todo pattern but is absent
        # from verified_open is treated as completed).
        #
        # Match strategy: if ANY word-level overlap ≥ 50% of the item's
        # words with a verified open todo → keep; if the item's words overlap
        # significantly with ANY previously-known vault todo BUT it's not in
        # verified_open → evict.  We use the vault_contents list for the
        # "was ever a vault todo" check and verified_open for the "still open" check.
        _word_re = re.compile(r'\b\w{4,}\b')

        def _significant_words(s: str) -> set[str]:
            return {w.lower() for w in _word_re.findall(s)}

        def _is_verified_open_todo(item: str) -> bool:
            """Return True if item overlaps with any verified-open vault todo."""
            item_words = _significant_words(item)
            if not item_words:
                return True  # can't judge, keep it
            for v in verified_open:
                v_words = _significant_words(v)
                overlap = len(item_words & v_words)
                if overlap / max(len(item_words), 1) >= 0.5:
                    return True
            return False

        def _was_vault_todo(item: str, all_vault_raw: set[str]) -> bool:
            """Return True if item looks like it came from a vault todo."""
            item_words = _significant_words(item)
            if not item_words:
                return False
            for v in all_vault_raw:
                v_words = _significant_words(v)
                overlap = len(item_words & v_words)
                if overlap / max(len(item_words), 1) >= 0.5:
                    return True
            return False

        # Build the full set of vault todo raw texts (prefix stripped) —
        # these are ALL the todos the vault ever had in this scroll batch.
        _prefix_re = re.compile(r'^Open todo \(\d{4}-\d{2}-\d{2}\):\s*')
        all_vault_raw: set[str] = set()
        for vc in (vault_contents or []):
            stripped_vc = _prefix_re.sub('', vc).strip()
            if stripped_vc:
                all_vault_raw.add(stripped_vc.lower())

        evicted_ttm = []
        kept_ttm = []
        for item in state.get("things_to_mention", []):
            # If the item looks like a vault todo AND is not in verified_open → evict.
            if _was_vault_todo(item, all_vault_raw) and not _is_verified_open_todo(item):
                evicted_ttm.append(item)
            else:
                kept_ttm.append(item)
        if evicted_ttm:
            print(f"[heartbeat] post-synthesis: evicted {len(evicted_ttm)} completed/stale things_to_mention item(s): {evicted_ttm}")
        state["things_to_mention"] = kept_ttm

        # Always null proactive_message when ANY things_to_mention items were
        # evicted — the LLM-written text may still mention the completed todo
        # by name even if other non-todo items remain.  The message will be
        # rebuilt from the clean things_to_mention list at send time.
        if evicted_ttm:
            state["proactive_message"] = None
            if not kept_ttm:
                print("[heartbeat] post-synthesis: nulled proactive_message (all things_to_mention were completed todos)")
            else:
                print(f"[heartbeat] post-synthesis: nulled proactive_message (contained {len(evicted_ttm)} completed todo(s)); will rebuild from {len(kept_ttm)} remaining item(s)")

    # Code-level: surface date-based ki_commitments when their date arrives.
    # Format "YYYY-MM-DD: description" is parsed and injected into things_to_mention
    # deterministically — not left to the model to figure out.
    _date_pattern = re.compile(r'^(\d{4}-\d{2}-\d{2}):\s*(.+)$')
    for commitment in state.get("ki_commitments", []):
        m = _date_pattern.match(commitment.strip())
        if not m:
            continue
        remind_date, description = m.group(1), m.group(2).strip()
        try:
            days_until = (date_type.fromisoformat(remind_date) - date_type.fromisoformat(today)).days
        except Exception:
            continue
        if 0 <= days_until <= 1:
            # Due today or tomorrow — inject at front of things_to_mention
            if description not in state["things_to_mention"]:
                state["things_to_mention"].insert(0, description)
                print(f"[heartbeat] code-injected commitment due in {days_until}d: {description!r}")

    # Hard post-processing: forcibly drop snoozed items whose resurface_after date has passed.
    # The model should have dropped these itself, but often lazily carries them forward.
    if state.get("snoozed"):
        pruned = []
        for s in state["snoozed"]:
            ra = s.get("resurface_after")
            if ra and ra <= today:
                print(f"[heartbeat] force-dropped expired snooze: {s.get('item')!r} (resurface_after={ra})")
            else:
                pruned.append(s)
        state["snoozed"] = pruned

    # Hard post-processing guard: strip any things_to_mention that match an active snooze.
    # The model already receives this as a prompt constraint, but this is the safety net.
    # Also check the NEW state's snoozed list — Haiku sometimes both snoozed something AND
    # put it in things_to_mention in the same run. The snooze should always win.
    def _words(s: str) -> set[str]:
        return {w.lower() for w in re.findall(r'\w+', s) if len(w) > 3}

    def _snooze_blocks(item: str, snooze_list: list[str]) -> bool:
        """Return True if item overlaps enough with any snooze entry to be suppressed.
        Uses item-relative ratio so short things_to_mention phrases can match
        against longer snooze descriptions without the ratio being diluted."""
        item_words = _words(item)
        if not item_words:
            return False
        for snooze in snooze_list:
            snooze_words = _words(snooze)
            overlap = len(item_words & snooze_words)
            # Require 40% of the item's words to match the snooze description.
            # Using item length (not snooze length) prevents long snooze descriptions
            # from diluting the ratio for short things_to_mention phrases.
            if overlap / max(len(item_words), 1) >= 0.4:
                return True
        return False

    # Build snooze list from new state too (not just previous)
    new_active_snoozes = []
    for s in state.get("snoozed", []):
        ra = s.get("resurface_after")
        if ra and ra <= today:
            continue
        new_active_snoozes.append(s.get("item", ""))

    all_snoozes = list(dict.fromkeys(active_snoozes + new_active_snoozes))  # dedupe, preserve order

    if all_snoozes and state.get("things_to_mention"):
        filtered = []
        for item in state["things_to_mention"]:
            if _snooze_blocks(item, all_snoozes):
                print(f"[heartbeat] stripped snoozed item from things_to_mention: {item!r}")
            else:
                filtered.append(item)
        state["things_to_mention"] = filtered
        if not filtered:
            state["proactive_message"] = None

    # Hard post-processing guard: strip things_to_mention that already have a pending
    # reminder. The LLM was told this in the prompt, but enforce it in code too.
    if pending_reminders and state.get("things_to_mention"):
        filtered = []
        for item in state["things_to_mention"]:
            if _snooze_blocks(item, pending_reminders):
                print(f"[heartbeat] stripped reminder-covered item from things_to_mention: {item!r}")
            else:
                filtered.append(item)
        state["things_to_mention"] = filtered
        if not filtered:
            state["proactive_message"] = None

    # Post-synthesis guard: strip things_to_mention that overlap significantly with
    # conversations from the last 12 hours, unless the item contains an explicit
    # deadline/urgency signal. This catches cases where the LLM re-raises something
    # that was actively discussed moments before the heartbeat ran.
    _no_convos_marker = "No conversations in this period."
    if (
        recent_12h_text
        and recent_12h_text.strip()
        and recent_12h_text.strip() != _no_convos_marker
        and state.get("things_to_mention")
    ):
        recent_words = _significant_words(recent_12h_text)
        _urgency_re = re.compile(
            r'\b(due|deadline|urgent|asap|tonight|24h|hour|imminent|overdue|expires?|by \d)\b'
            r'|\d{4}-\d{2}-\d{2}',
            re.IGNORECASE,
        )
        filtered_recent = []
        stripped_recent = []
        for item in state["things_to_mention"]:
            item_words = _significant_words(item)
            if not item_words:
                filtered_recent.append(item)
                continue
            overlap = len(item_words & recent_words)
            overlap_ratio = overlap / max(len(item_words), 1)
            has_urgency = bool(_urgency_re.search(item))
            if overlap_ratio >= 0.5 and not has_urgency:
                stripped_recent.append(item)
            else:
                filtered_recent.append(item)
        if stripped_recent:
            print(
                f"[heartbeat] recent-convo guard: stripped {len(stripped_recent)} item(s) "
                f"discussed in last 12h: {stripped_recent}"
            )
            state["things_to_mention"] = filtered_recent
            state["proactive_message"] = None

    return state


async def _send_proactive(text: str) -> None:
    # Ki app is primary — always deliver here first
    await mem.save_turn(user="", assistant=text, channel="desktop")
    try:
        from .push import send_push_to_all
        await send_push_to_all(text.strip()[:120], "")
    except Exception as e:
        print(f"[heartbeat] push notification failed (non-fatal): {e}")

    # WhatsApp fallback — non-blocking
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"{BAILEYS_URL}/send",
                json={"text": text, "secret": INTERNAL_SECRET},
                timeout=10,
            )
        if r.is_success:
            await mem.save_turn(user="", assistant=text, channel="whatsapp")
        else:
            print(f"[heartbeat] WhatsApp send failed (non-fatal): HTTP {r.status_code}")
    except Exception as e:
        print(f"[heartbeat] WhatsApp send failed (non-fatal): {e}")


async def run_heartbeat(hours: int = 3) -> dict:
    """Synthesise state and optionally send a proactive message. Returns new state."""
    now_local = datetime.now(USER_TZ)
    hour = now_local.hour
    if QUIET_START <= hour or hour < QUIET_END:
        print(f"[heartbeat] skipped — quiet hours ({hour:02d}:00 {USER_TZ}, quiet {QUIET_START}–{QUIET_END})")
        return {}

    print(f"[heartbeat] running (window={hours}h)")

    try:
        from .indexer import reindex
        await reindex()
    except Exception as e:
        print(f"[heartbeat] reindex failed: {e}")

    turns = await mem.get_conversations_since(hours)
    recent_convos = _format_conversations(turns)

    # Dynamic extended window: at least 24h, but stretch to cover since last conversation
    # so acknowledgements/deferrals don't fall outside the lookback window after gaps.
    extended_hours = 24.0
    try:
        last_turn_time = await mem.get_last_turn_time()
        if last_turn_time:
            hours_since_last = (datetime.now(timezone.utc) - last_turn_time).total_seconds() / 3600
            extended_hours = max(24.0, min(hours_since_last + 2.0, 72.0))
    except Exception as e:
        print(f"[heartbeat] get_last_turn_time failed: {e}")

    extended_turns = await mem.get_conversations_since(extended_hours)
    extended_convos = _format_conversations(extended_turns)

    # 12-hour window used exclusively for the recent-discussion suppression guard
    recent_12h_turns = await mem.get_conversations_since(12)
    recent_12h_text = _format_conversations(recent_12h_turns)

    vault_todos_str, vault_contents = await _fetch_vault_todos()
    previous_state = await mem.get_latest_heartbeat_state()
    vault_removed = _detect_vault_removals(vault_contents, previous_state)
    if vault_removed:
        print(f"[heartbeat] vault removals detected: {vault_removed}")

    today = datetime.now(USER_TZ).strftime("%Y-%m-%d")

    # Fetch pending reminders so the heartbeat doesn't re-surface things already queued
    pending_reminders: list[str] = []
    try:
        pending_reminders = await mem.get_pending_reminder_messages()
        if pending_reminders:
            print(f"[heartbeat] {len(pending_reminders)} pending reminder(s) will be suppressed from heartbeat output")
    except Exception as e:
        print(f"[heartbeat] pending reminder fetch failed (non-fatal): {e}")

    # Fetch today's and tomorrow's calendar events for synthesis context
    calendar_events: str = ""
    try:
        from .integrations.google_calendar import get_heartbeat_events
        calendar_events = await get_heartbeat_events()
        if calendar_events:
            print(f"[heartbeat] calendar events fetched for synthesis context")
    except Exception as e:
        print(f"[heartbeat] calendar fetch failed (non-fatal): {e}")

    # Fetch scheduled task context (overdue/due-soon tracked tasks)
    scheduled_tasks_due: str = ""
    try:
        scheduled_tasks_due = await _fetch_scheduled_tasks_due()
        if scheduled_tasks_due:
            print(f"[heartbeat] scheduled tasks due/overdue detected for synthesis context")
    except Exception as e:
        print(f"[heartbeat] task config fetch failed (non-fatal): {e}")

    try:
        state = await _synthesise(
            recent_convos, extended_convos, vault_todos_str, vault_removed,
            previous_state,
            pending_reminders=pending_reminders,
            vault_contents=vault_contents,
            calendar_events=calendar_events or None,
            scheduled_tasks_block=scheduled_tasks_due or None,
            recent_12h_text=recent_12h_text,
        )
    except Exception as e:
        print(
            f"[heartbeat] ⚠️  synthesis raised {type(e).__name__}: {e}\n"
            + traceback.format_exc()
        )
        return {}

    # Post-processing: track first_seen/mention_count metadata and vault snapshot
    _update_item_metadata(state, previous_state, today)
    state["vault_todo_snapshot"] = vault_contents

    await mem.save_heartbeat_state(state)
    print(
        f"[heartbeat] done — todos={len(state.get('open_todos', []))} "
        f"loops={len(state.get('open_loops', []))} "
        f"commitments={len(state.get('ki_commitments', []))} "
        f"proactive={len(state.get('things_to_mention', []))}"
        + (" [parse_error]" if "_parse_error" in state else "")
    )

    # Proactive message — only if there's something to say and cooldown has passed.
    # Skip if this run produced a parse error — we don't want to send stale/bad data.
    if state.get("things_to_mention") and INTERNAL_SECRET and "_parse_error" not in state:
        last_sent = (previous_state or {}).get("proactive_sent_at")
        cooldown_ok = True
        if last_sent:
            try:
                elapsed = datetime.now(timezone.utc) - datetime.fromisoformat(last_sent)
                cooldown_ok = elapsed >= timedelta(hours=PROACTIVE_COOLDOWN_HOURS)
            except Exception:
                pass

        if not cooldown_ok:
            print(f"[heartbeat] proactive skipped — cooldown active (last sent: {last_sent})")
        else:
            message = state.get("proactive_message") or "\n".join(f"• {t}" for t in state["things_to_mention"])
            try:
                await _send_proactive(message)
                state["proactive_sent_at"] = datetime.now(USER_TZ).isoformat()
                # Auto-snooze everything just raised so next heartbeat starts clean
                already_snoozed = {s["item"] for s in state.get("snoozed", [])}
                resurface = (datetime.now(USER_TZ) + timedelta(days=3)).strftime("%Y-%m-%d")
                for item in state.get("things_to_mention", []):
                    if item not in already_snoozed:
                        state["snoozed"].append({
                            "item": item,
                            "reason": "Already raised in proactive message",
                            "until": "something changes or the user brings it up",
                            "resurface_after": resurface,
                        })
                state["things_to_mention"] = []
                await mem.save_heartbeat_state(state)
                print("[heartbeat] proactive message sent")
            except Exception as e:
                print(f"[heartbeat] proactive send failed: {e}")

    return state
