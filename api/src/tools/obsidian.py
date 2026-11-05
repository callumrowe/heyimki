"""
Obsidian vault tools — search, read, append, write.

The vault is any Obsidian vault folder. Git is optional:
  - plain folder          → files are written directly, no git at all
  - git repo, no remote   → every write is committed (local history only)
  - git repo with remote  → pull before writing, commit, then push
Set VAULT_GIT_SYNC=off to skip pull/push even when a remote exists.
"""
import asyncio
import os
import re
import subprocess
import unicodedata
from datetime import date
from pathlib import Path

from ..embeddings import embed
from ..vector_store import client as qdrant

VAULT = Path(os.environ.get("OBSIDIAN_VAULT_PATH", "/vault"))
COLLECTION = "obsidian"
# Optional SSH deploy key for pushing/pulling a private vault repo over SSH.
SSH_KEY = os.environ.get("VAULT_SSH_KEY", "/secrets/vault_deploy_key")
VAULT_GIT_SYNC = os.environ.get("VAULT_GIT_SYNC", "auto").strip().lower()
GIT_ENV = {
    # Commits need an identity; the container has none by default.
    "GIT_AUTHOR_NAME": "Ki",
    "GIT_AUTHOR_EMAIL": "ki@localhost",
    "GIT_COMMITTER_NAME": "Ki",
    "GIT_COMMITTER_EMAIL": "ki@localhost",
    **os.environ,
}
if os.path.isfile(SSH_KEY):
    GIT_ENV["GIT_SSH_COMMAND"] = f"ssh -i {SSH_KEY} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"

# ---------------------------------------------------------------------------
# String normalisation helpers
# ---------------------------------------------------------------------------

# Map "fancy" Unicode punctuation to plain ASCII equivalents so that a query
# containing e.g. an em dash or curly quotes still matches file content that
# uses a regular hyphen or straight quotes (and vice-versa).
_UNICODE_TO_ASCII = str.maketrans(
    {
        "\u2013": "-",   # en dash → hyphen
        "\u2014": "-",   # em dash → hyphen
        "\u2015": "-",   # horizontal bar → hyphen
        "\u2018": "'",   # left single quotation mark
        "\u2019": "'",   # right single quotation mark / apostrophe
        "\u201a": "'",   # single low-9 quotation mark
        "\u201b": "'",   # single high-reversed-9 quotation mark
        "\u201c": '"',   # left double quotation mark
        "\u201d": '"',   # right double quotation mark
        "\u201e": '"',   # double low-9 quotation mark
        "\u2026": "...", # ellipsis
        "\u00a0": " ",   # non-breaking space
        "\u2010": "-",   # hyphen (proper Unicode hyphen)
        "\u2011": "-",   # non-breaking hyphen
        "\u2012": "-",   # figure dash
        "\u00ad": "",    # soft hyphen (remove)
    }
)


def _normalise(text: str) -> str:
    """
    Normalise a string for fuzzy comparison:
    - NFC Unicode normalisation
    - Replace fancy Unicode punctuation with ASCII equivalents
    - Collapse multiple spaces
    - Lowercase
    """
    text = unicodedata.normalize("NFC", text)
    text = text.translate(_UNICODE_TO_ASCII)
    text = re.sub(r"\s+", " ", text).strip().lower()
    return text


def vault_is_git() -> bool:
    return (VAULT / ".git").exists()


def vault_has_remote() -> bool:
    if VAULT_GIT_SYNC in ("off", "false", "0", "no") or not vault_is_git():
        return False
    result = subprocess.run(
        ["git", "-C", str(VAULT), "remote"],
        env=GIT_ENV, capture_output=True, text=True, timeout=10,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def _git_sync(*args: str) -> None:
    """Blocking git subprocess call — always run via _git() in async context.

    No-op when the vault isn't a git repo; pull/push are skipped when it has
    no remote. A commit with nothing to commit is not an error.
    """
    if not args or not vault_is_git():
        return
    if args[0] in ("pull", "push", "fetch") and not vault_has_remote():
        return
    if args[0] == "commit":
        result = subprocess.run(
            ["git", "-C", str(VAULT), *args],
            env=GIT_ENV, capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0 and "nothing to commit" not in (result.stdout + result.stderr):
            raise subprocess.CalledProcessError(result.returncode, result.args, result.stdout, result.stderr)
        return
    subprocess.run(["git", "-C", str(VAULT), *args], env=GIT_ENV, check=True, timeout=30)


async def _git(*args: str) -> None:
    """Non-blocking wrapper: runs the git subprocess in a thread pool executor."""
    await asyncio.to_thread(_git_sync, *args)


async def pull_vault() -> None:
    """Pull remote vault changes, if the vault has a remote. Errors are logged, not raised."""
    try:
        await _git("pull", "--quiet", "--autostash")
    except Exception as e:
        print(f"[vault] pull failed: {e}")


def _safe_path(filename: str) -> Path:
    """Resolve filename relative to vault, guard against path traversal."""
    resolved = (VAULT / filename).resolve()
    if not str(resolved).startswith(str(VAULT.resolve())):
        raise ValueError(f"Path traversal rejected: {filename}")
    return resolved


def _all_vault_files() -> list[str]:
    """Return all .md file paths in the vault, relative to VAULT root."""
    if not VAULT.exists():
        return []
    return [
        str(p.relative_to(VAULT))
        for p in VAULT.rglob("*.md")
        if not any(part.startswith(".") for part in p.parts)
    ]


def _fuzzy_find(filename: str, candidates: list[str]) -> tuple[str, float] | None:
    """
    Find the closest matching filename using rapidfuzz (preferred) or
    difflib as a fallback.  Returns (best_match, score_0_to_100) or None
    if no sufficiently close match is found.
    """
    norm_query = _normalise(filename)

    try:
        from rapidfuzz import process as rf_process, fuzz

        # Score against normalised filenames but return the original candidate.
        result = rf_process.extractOne(
            norm_query,
            candidates,
            scorer=fuzz.token_sort_ratio,
            processor=_normalise,
            score_cutoff=55,          # 0-100; below this we ignore the match
        )
        if result:
            match, score, _ = result
            return match, score

    except ImportError:
        # rapidfuzz not available — fall back to difflib
        import difflib

        close = difflib.get_close_matches(
            norm_query,
            [_normalise(c) for c in candidates],
            n=1,
            cutoff=0.55,
        )
        if close:
            norm_hit = close[0]
            # Map back to original candidate (first one whose normalised form matches)
            for c in candidates:
                if _normalise(c) == norm_hit:
                    return c, 70.0   # difflib doesn't give a 0-100 score; use a placeholder

    return None


# ---------------------------------------------------------------------------
# Public tools
# ---------------------------------------------------------------------------

def _todo_still_open(filename: str, content: str) -> bool:
    """
    Ground-truth check: return True only if *content* (an indexed todo string
    of the form "Open todo (YYYY-MM-DD): <text>") is still present as an
    unchecked "- [ ]" item in the actual source file right now.

    This filters out stale Qdrant points for todos that have been ticked off
    in the vault since they were last indexed.

    Scanning rule:
    - Read every line of the source file.
    - Strip the "Open todo (date): " prefix to get the raw todo text.
    - For each line that contains the raw text:
        * If the line starts with "- [ ]" (open checkbox) → return True immediately.
        * If the line starts with "- [x]" or "- [X]" (ticked checkbox) → return
          False immediately. A ticked item is done, full stop.
    - If the text is found only in non-checkbox prose → keep scanning.
    - If no open-checkbox line is ever found → return False (conservative default:
      never surface an item we cannot confirm is open).
    """
    import re as _re
    path = _safe_path(filename)
    if not path.exists():
        return False
    try:
        file_text = path.read_text(errors="replace")
    except Exception:
        return False

    # Strip the indexer-added prefix to get back to the raw todo text
    raw = _re.sub(r'^Open todo \(\d{4}-\d{2}-\d{2}\):\s*', '', content).strip()
    if not raw:
        return False

    raw_lower = raw.lower()
    for line in file_text.splitlines():
        stripped = line.strip()
        if raw_lower not in stripped.lower():
            continue
        # Found the text on this line — check the checkbox state.
        if _re.match(r'^[-*+]\s+\[ \]\s+', stripped):
            return True   # still open — confirmed
        if _re.match(r'^[-*+]\s+\[[xX]\]\s+', stripped):
            return False  # ticked off — done, stop immediately
        # Text found in non-checkbox prose — keep scanning.

    # No open-checkbox line found anywhere → done or deleted.
    return False


async def search_vault(query: str, limit: int = 5, doc_type: str | None = None) -> list[dict]:
    """
    Semantic search over indexed vault notes. Returns ranked excerpts.
    Optionally filter by doc_type: 'note', 'daily_note', or 'todo'.
    The query is normalised before embedding so that special characters
    (em dashes, smart quotes, etc.) don't skew the vector.

    When doc_type='todo', every returned item is verified against the source
    file on disk. Items whose checkbox has been ticked (- [x] / - [X]) are
    silently filtered out so Ki never surfaces a completed task as open.
    """
    from qdrant_client.models import Filter, FieldCondition, MatchValue

    # Normalise the query so fancy punctuation doesn't fragment tokens
    normalised_query = _normalise(query)

    vector = await embed(normalised_query)
    query_filter = (
        Filter(must=[FieldCondition(key="doc_type", match=MatchValue(value=doc_type))])
        if doc_type else None
    )

    # When filtering for todos we may need to discard some results after
    # the source-file verification, so fetch extra candidates to compensate.
    fetch_limit = limit * 3 if doc_type == "todo" else limit

    response = await qdrant.query_points(
        collection_name=COLLECTION,
        query=vector,
        query_filter=query_filter,
        limit=fetch_limit,
    )

    results = []
    skipped_ticked = 0
    for r in response.points:
        r_doc_type = r.payload.get("doc_type", "note")
        content = r.payload["content"]

        # Hard filter: verify every todo against the live file on disk.
        # If the checkbox has been ticked (- [x] / - [X]) or the file is gone,
        # exclude the result entirely — it is done and must never be surfaced.
        if r_doc_type == "todo":
            filename = r.payload["filename"]
            if not _todo_still_open(filename, content):
                skipped_ticked += 1
                continue  # ticked off or gone — skip it

        results.append({
            "filename": r.payload["filename"],
            "doc_type": r_doc_type,
            "date": r.payload.get("date"),
            "heading": r.payload.get("heading", ""),
            "excerpt": content[:400],
            "score": round(r.score, 3),
        })
        if len(results) >= limit:
            break

    if skipped_ticked:
        print(f"[search_vault] filtered out {skipped_ticked} ticked/completed todo(s) from results (query={query!r})")

    return results


async def read_note(filename: str) -> str:
    """
    Read a vault note by relative path (e.g. 'folder/note.md').

    If the exact path doesn't exist, automatically scans all vault files and
    returns the closest fuzzy match instead, with a note explaining the
    substitution.  This handles em dashes vs regular dashes, smart quotes,
    slight spelling differences, etc.
    """
    path = _safe_path(filename)
    if path.exists():
        return path.read_text(errors="replace")

    # ── Fuzzy fallback ──────────────────────────────────────────────────────
    print(f"[read_note] '{filename}' not found — attempting fuzzy match")
    candidates = _all_vault_files()

    if not candidates:
        return f"Note not found: {filename}"

    match = _fuzzy_find(filename, candidates)
    if match is None:
        print(f"[read_note] no close match found for '{filename}' among {len(candidates)} files")
        return (
            f"Note not found: {filename}\n\n"
            f"No close filename match found either. "
            f"Try search_vault to locate the right note by content."
        )

    matched_filename, score = match
    matched_path = _safe_path(matched_filename)
    if not matched_path.exists():
        return f"Note not found: {filename}"

    print(f"[read_note] fuzzy match '{filename}' → '{matched_filename}' (score={score:.0f})")
    content = matched_path.read_text(errors="replace")
    header = (
        f"[Note: '{filename}' was not found exactly. "
        f"Returning closest match: '{matched_filename}' (similarity {score:.0f}/100)]\n\n"
    )
    return header + content


def _insert_before_hr(text: str, content: str) -> str:
    """Insert content into the ## Daily notes section of a daily note.

    Finds the '## Daily notes' heading and inserts before the next '---' or '##'
    separator that closes that section. Falls back to inserting before the last
    '---' if no '## Daily notes' heading exists.
    """
    lines = text.splitlines()

    # Find the ## Daily notes heading (handles emoji prefix like "## ✏️ Daily notes")
    daily_notes_idx = -1
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("##") and stripped.endswith("Daily notes"):
            daily_notes_idx = i
            break

    if daily_notes_idx != -1:
        # Find the next --- or ## heading that closes this section
        insert_at = len(lines)
        for i in range(daily_notes_idx + 1, len(lines)):
            stripped = lines[i].strip()
            if stripped == "---" or (stripped.startswith("## ") and i > daily_notes_idx + 1):
                insert_at = i
                break
        lines.insert(insert_at, content)
        return "\n".join(lines) + "\n"

    # Fallback: insert before the last --- separator
    last_hr = -1
    for i, line in enumerate(lines):
        if line.strip() == "---":
            last_hr = i
    if last_hr == -1:
        return text + f"\n{content}\n"
    lines.insert(last_hr, content)
    return "\n".join(lines) + "\n"


def _try_parse_date(s: str) -> date | None:
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def _parse_task_config(yaml_text: str) -> dict:
    """
    Pure-Python parser for the fixed task-config YAML structure.
    Handles: top-level list keys (daily, weekdays), dict keys with scalar or list values
    (biweekly, fortnightly), list-of-dicts (weekly, interval), and dict items within
    sub-lists (task objects with last_done in biweekly/fortnightly).
    No pyyaml dependency.
    """
    result: dict = {}
    current_top_key: str | None = None
    top_is_list: bool = False
    current_sub_key: str | None = None
    current_list_item: dict | None = None      # top-level list-of-dicts item (indent 2)
    current_list_item_sub_key: str | None = None
    current_sub_list_item: dict | None = None  # dict item within a top-level dict's sub-list

    for line in yaml_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        indent = len(line) - len(line.lstrip(" "))

        if indent == 0:
            if stripped.endswith(":"):
                current_top_key = stripped[:-1]
                current_sub_key = None
                current_list_item = None
                current_list_item_sub_key = None
                current_sub_list_item = None
                top_is_list = False
        elif indent == 2 and current_top_key is not None:
            current_sub_list_item = None  # leaving any sub-list context
            if stripped.startswith("- "):
                value = stripped[2:].strip()
                if ":" in value:
                    # List-of-dicts item: "- key: value"
                    if not isinstance(result.get(current_top_key), list):
                        result[current_top_key] = []
                        top_is_list = True
                    colon_pos = value.index(":")
                    k = value[:colon_pos].strip()
                    v = value[colon_pos + 1:].strip()
                    current_list_item = {k: v} if v else {k: []}
                    current_list_item_sub_key = None if v else k
                    result[current_top_key].append(current_list_item)
                else:
                    # Simple string list item
                    if not top_is_list:
                        result[current_top_key] = []
                        top_is_list = True
                    result[current_top_key].append(value)
                    current_list_item = None
                    current_list_item_sub_key = None
            elif ":" in stripped:
                # Dict sub-key (non-list item)
                current_list_item = None
                current_list_item_sub_key = None
                if not isinstance(result.get(current_top_key), dict):
                    result[current_top_key] = {}
                    top_is_list = False
                colon_pos = stripped.index(":")
                key = stripped[:colon_pos].strip()
                value = stripped[colon_pos + 1:].strip()
                if value:
                    if value.startswith("[") and value.endswith("]"):
                        inner = value[1:-1]
                        result[current_top_key][key] = [item.strip() for item in inner.split(",") if item.strip()]
                    else:
                        result[current_top_key][key] = value
                    current_sub_key = None
                else:
                    result[current_top_key][key] = []
                    current_sub_key = key
        elif indent == 4 and current_top_key is not None:
            if current_list_item is not None:
                # Inside a top-level list-of-dicts item (weekly, interval entries)
                if ":" in stripped and not stripped.startswith("- "):
                    colon_pos = stripped.index(":")
                    key = stripped[:colon_pos].strip()
                    value = stripped[colon_pos + 1:].strip()
                    if value:
                        current_list_item[key] = value
                        current_list_item_sub_key = None
                    else:
                        current_list_item[key] = []
                        current_list_item_sub_key = key
            elif current_sub_key is not None:
                # Inside a top-level dict's sub-list (biweekly.tasks, fortnightly.tasks)
                if stripped.startswith("- "):
                    current_sub_list_item = None
                    value = stripped[2:].strip()
                    top_dict = result.get(current_top_key)
                    if isinstance(top_dict, dict):
                        if not isinstance(top_dict.get(current_sub_key), list):
                            top_dict[current_sub_key] = []
                        if ":" in value:
                            # Dict item (e.g. "- task: Clean bathrooms #routine")
                            colon_pos = value.index(":")
                            k = value[:colon_pos].strip()
                            v = value[colon_pos + 1:].strip()
                            sub_item: dict = {k: v} if v else {k: []}
                            top_dict[current_sub_key].append(sub_item)
                            current_sub_list_item = sub_item
                        else:
                            top_dict[current_sub_key].append(value)
        elif indent == 6 and current_top_key is not None:
            if current_sub_list_item is not None:
                # Sub-keys of a dict item in a top-level dict's sub-list (e.g. last_done)
                if ":" in stripped and not stripped.startswith("- "):
                    colon_pos = stripped.index(":")
                    key = stripped[:colon_pos].strip()
                    value = stripped[colon_pos + 1:].strip()
                    current_sub_list_item[key] = value
            elif current_list_item is not None and current_list_item_sub_key is not None:
                # Items in a sub-list of a top-level list item (old weekly tasks format)
                if stripped.startswith("- "):
                    value = stripped[2:].strip()
                    if not isinstance(current_list_item.get(current_list_item_sub_key), list):
                        current_list_item[current_list_item_sub_key] = []
                    current_list_item[current_list_item_sub_key].append(value)

    return result


def _parse_interval_days(every_str: str) -> int | None:
    """Parse '10 weeks' or '6 months' into a number of days."""
    m = re.match(r"(\d+)\s+(weeks?|months?)", every_str.strip(), re.IGNORECASE)
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2).lower().rstrip("s")
    if unit == "week":
        return n * 7
    if unit == "month":
        return n * 30
    return None


def _update_last_done_in_config(config_text: str, task_str: str, new_date: str) -> str:
    """Find 'task: {task_str}' in the config YAML text and update its last_done value."""
    lines = config_text.splitlines(keepends=True)
    result: list[str] = []
    found_task = False
    for line in lines:
        stripped = line.strip()
        if not found_task:
            if stripped == f"task: {task_str}" or stripped == f"- task: {task_str}":
                found_task = True
                result.append(line)
                continue
        elif found_task:
            if stripped.startswith("last_done:"):
                indent = len(line) - len(line.lstrip())
                result.append(" " * indent + f"last_done: {new_date}\n")
                found_task = False
                continue
            elif stripped.startswith("- "):
                # Hit the next list item without finding last_done — give up
                found_task = False
        result.append(line)
    return "".join(result)


async def get_scheduled_tasks_section(note_date: date) -> str:
    """Build scheduled task lines for a daily note from vault config.

    Reads last_done directly from the config file (no separate tracker).
    Writes updated last_done back to the config after determining which tasks are due.
    """
    config_path = _safe_path("config/task-config.yaml")
    if not config_path.exists():
        return ""

    try:
        config_text = config_path.read_text(errors="replace")
        config = _parse_task_config(config_text)
        if not config or not isinstance(config, dict):
            return ""
    except Exception:
        return ""

    day_name = note_date.strftime("%A").lower()
    task_lines: list[str] = []
    tasks_to_update: list[str] = []  # task strings whose last_done should be written back

    # Daily tasks (plain strings, always injected)
    for t in config.get("daily", []):
        task_lines.append(f"- [ ] {t}")

    # Weekdays tasks (plain strings, weekdays only)
    weekdays = config.get("weekdays", [])
    if weekdays and day_name in ("monday", "tuesday", "wednesday", "thursday", "friday"):
        for t in weekdays:
            task_lines.append(f"- [ ] {t}")

    # Biweekly tasks — objects {task, last_done} on configured days
    biweekly = config.get("biweekly", {})
    if isinstance(biweekly, dict):
        bw_days = [d.lower() for d in biweekly.get("days", [])]
        if day_name in bw_days:
            for item in biweekly.get("tasks", []):
                if isinstance(item, dict):
                    task_str = item.get("task", "")
                    if task_str:
                        task_lines.append(f"- [ ] {task_str}")
                        tasks_to_update.append(task_str)
                else:
                    task_lines.append(f"- [ ] {item}")

    # Weekly tasks — flat list of {day, task, last_done}; also handles old {day, tasks:[]} format
    weekly = config.get("weekly", [])
    if isinstance(weekly, list):
        for entry in weekly:
            if not isinstance(entry, dict) or entry.get("day", "").lower() != day_name:
                continue
            task_str = entry.get("task", "")
            if task_str:
                task_lines.append(f"- [ ] {task_str}")
                if "last_done" in entry:
                    tasks_to_update.append(task_str)
            # Backwards compat: old format with tasks sub-list
            for t in entry.get("tasks", []):
                t_str = t.get("task", "") if isinstance(t, dict) else t
                if t_str:
                    task_lines.append(f"- [ ] {t_str}")
    elif isinstance(weekly, dict):
        if weekly.get("day", "").lower() == day_name:
            for t in weekly.get("tasks", []):
                t_str = t.get("task", "") if isinstance(t, dict) else t
                if t_str:
                    task_lines.append(f"- [ ] {t_str}")

    # Fortnightly tasks — objects {task, last_done}; injected when >14 days since last_done
    fortnightly_cfg = config.get("fortnightly", {})
    if isinstance(fortnightly_cfg, dict):
        for item in fortnightly_cfg.get("tasks", []):
            if isinstance(item, dict):
                task_str = item.get("task", "")
                last_done_str = item.get("last_done", "never")
                last_done_date = None if last_done_str == "never" else _try_parse_date(last_done_str)
                if last_done_date is None or (note_date - last_done_date).days > 14:
                    if task_str:
                        task_lines.append(f"- [ ] {task_str}")
                        tasks_to_update.append(task_str)
            else:
                # Legacy plain string — always inject (no due-date tracking)
                task_lines.append(f"- [ ] {item}")

    # Interval tasks — {task, every, last_done}; injected when elapsed days >= interval
    for item in config.get("interval", []) or []:
        if not isinstance(item, dict):
            continue
        task_str = item.get("task", "")
        every_str = item.get("every", "")
        last_done_str = item.get("last_done", "never")
        if not task_str or not every_str:
            continue
        interval_days = _parse_interval_days(every_str)
        if interval_days is None:
            continue
        last_done_date = None if last_done_str == "never" else _try_parse_date(last_done_str)
        if last_done_date is None or (note_date - last_done_date).days >= interval_days:
            task_lines.append(f"- [ ] {task_str}")
            tasks_to_update.append(task_str)

    # Write back updated last_done for all injected tracked tasks
    if tasks_to_update:
        try:
            new_config_text = config_text
            today_str = note_date.isoformat()
            for task_str in tasks_to_update:
                new_config_text = _update_last_done_in_config(new_config_text, task_str, today_str)
            if new_config_text != config_text:
                config_path.write_text(new_config_text, errors="replace")
        except Exception as e:
            print(f"[task_schedule] failed to update last_done in config: {e}")

    if not task_lines:
        return ""

    return "\n".join(task_lines) + "\n"


async def append_to_note(filename: str, content: str, commit_message: str | None = None) -> str:
    """
    Append content to a note. If filename is 'daily' or 'today', uses today's
    daily note (YYYY-MM-DD.md in vault root).
    For daily notes, inserts content into the '## ✏️ Daily notes' section.
    Pulls, writes, commits, pushes.
    """
    if filename.lower() in ("daily", "today"):
        filename = f"daily/{date.today().isoformat()}.md"

    is_daily = bool(re.match(r"daily/\d{4}-\d{2}-\d{2}\.md$", filename))

    path = _safe_path(filename)
    await _git("pull", "--quiet", "--autostash")
    path.parent.mkdir(parents=True, exist_ok=True)

    if is_daily and path.exists():
        existing = path.read_text(errors="replace")
        path.write_text(_insert_before_hr(existing, content))
    else:
        with path.open("a") as f:
            f.write(f"\n{content}\n")

    await _git("add", str(path.relative_to(VAULT)))
    msg = commit_message or f"ki: append to {filename}"
    await _git("commit", "-m", msg)
    await _git("push")
    return f"Appended to {filename}"


async def list_vault_directory(directory: str = "") -> str:
    """
    List all files in a vault directory (non-recursive by default).
    directory is relative to vault root, e.g. 'INSURANCE DOCS' or 'daily'.
    Returns filenames and relative paths.
    """
    if directory:
        dir_path = _safe_path(directory)
    else:
        dir_path = VAULT

    if not dir_path.exists():
        return f"Directory not found: {directory or '(vault root)'}"
    if not dir_path.is_dir():
        return f"Not a directory: {directory}"

    entries = sorted(dir_path.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    if not entries:
        return f"Directory is empty: {directory or '(vault root)'}"

    lines = []
    for entry in entries:
        rel = str(entry.relative_to(VAULT))
        if entry.is_dir():
            lines.append(f"[dir]  {rel}/")
        else:
            lines.append(f"[file] {rel}")

    return "\n".join(lines)


async def read_vault_pdf(path: str) -> str:
    """
    Extract text from a PDF file in the vault.
    path is relative to the vault root, e.g. 'INSURANCE DOCS/appeal-letter.pdf'.
    """
    import asyncio
    from pdfminer.high_level import extract_text

    full_path = _safe_path(path)
    if not full_path.exists():
        return f"File not found: {path}"
    if not full_path.suffix.lower() == ".pdf":
        return f"Not a PDF file: {path}"

    loop = asyncio.get_event_loop()
    text = await loop.run_in_executor(None, extract_text, str(full_path))
    text = text.strip()
    if not text:
        return f"No text extracted from {path} (may be a scanned/image-only PDF)."
    # Cap at 50k chars to avoid blowing the context window on huge docs
    if len(text) > 50000:
        text = text[:50000] + f"\n\n[truncated — {len(text)} total chars]"
    return text


async def write_note(filename: str, content: str, overwrite: bool = False) -> str:
    """
    Write (create) a note. Refuses to overwrite an existing note unless
    overwrite=True is explicitly set.
    Pulls, writes, commits, pushes.
    """
    path = _safe_path(filename)
    await _git("pull", "--quiet", "--autostash")

    if path.exists() and not overwrite:
        return f"Note already exists: {filename}. Set overwrite=True to replace it."

    action = "overwrite" if path.exists() else "create"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)

    await _git("add", str(path.relative_to(VAULT)))
    await _git("commit", "-m", f"ki: {action} {filename}")
    await _git("push")
    return f"Written: {filename}"
