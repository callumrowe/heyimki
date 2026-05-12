"""
Vault indexer — on startup (and on demand), indexes changed Obsidian .md files
into the Qdrant 'obsidian' collection.
"""
import asyncio
import hashlib
import os
import re
import subprocess
from pathlib import Path

import sqlalchemy as sa
from qdrant_client.models import Filter, FieldCondition, MatchValue, PointStruct

from .db import SessionLocal
from .embeddings import embed
from .vector_store import client as qdrant

VAULT_PATH = Path(os.environ.get("OBSIDIAN_VAULT_PATH", "/vault"))
COLLECTION = "obsidian"

BULLET_RE = re.compile(r'^([-*+]|\d+\.)\s+(?:\[.\]\s+)?(.*)$')
# Only matches genuinely unchecked items — [ ] with a literal space inside.
# [x] / [X] / any other character means done — never match those.
OPEN_TODO_RE = re.compile(r'^\t*[-*+]\s+\[ \]\s+(.+)$', re.MULTILINE)
# Matches any completed (ticked) checkbox variant.
DONE_TODO_RE = re.compile(r'^\t*[-*+]\s+\[[xX]\]\s+', re.MULTILINE)
# Matches a ticked checkbox at the start of a stripped (un-indented) line.
# Used in _flatten_bullets to skip done items before BULLET_RE can strip the [x] marker.
TICKED_LINE_RE = re.compile(r'^[-*+]\s+\[[xX]\]\s+')
HR_RE = re.compile(r'^---+\s*$')
HEADING_RE = re.compile(r'^#{1,3} ')


# ---------------------------------------------------------------------------
# Preprocessing helpers
# ---------------------------------------------------------------------------

def _is_indexable(path: Path) -> bool:
    """Skip vault root .md files and anything under /templates."""
    rel = path.relative_to(VAULT_PATH)
    parts = rel.parts
    if len(parts) == 1:
        return False  # vault root index files
    if parts[0].lower() == "templates":
        return False
    return True


def _strip_obsidian_syntax(text: str) -> str:
    """Remove/simplify Obsidian-specific markdown so embeddings see clean prose."""
    # Strip dataview/tasks code blocks
    text = re.sub(r'```(?:dataview|tasks)\b.*?```', '', text, flags=re.DOTALL)
    # Remove file embeds ![[file]]
    text = re.sub(r'!\[\[.*?\]\]', '', text)
    # Convert [[Note|alias]] → alias, [[Note]] → Note
    text = re.sub(r'\[\[([^\]|]+)\|([^\]]+)\]\]', r'\2', text)
    text = re.sub(r'\[\[([^\]]+)\]\]', r'\1', text)
    # Convert [link text](url) → link text
    text = re.sub(r'\[([^\]]+)\]\(https?://[^\)]+\)', r'\1', text)
    # Strip bare URLs
    text = re.sub(r'https?://\S+', '', text)
    return text


def _flatten_bullets(text: str) -> str:
    """
    Flatten outliner-style (tab-indented) bullet points into readable prose.
    Each top-level bullet and all its descendants become a single sentence,
    joined with '. ' so the embedding model gets full context.
    Non-bullet lines (headings, plain text) are passed through unchanged.

    IMPORTANT: Ticked checkbox lines (- [x] / - [X]) are skipped entirely so
    completed items never appear in the indexed content, even as plain prose.
    The ticked-line check runs BEFORE BULLET_RE because BULLET_RE also matches
    ticked lines and strips the [x] marker, which would otherwise let the
    done-item text survive as plain prose.
    """
    lines = text.splitlines()
    output = []
    current_parts: list[str] = []

    def flush() -> None:
        if current_parts:
            sentence = '. '.join(p.rstrip('. ') for p in current_parts if p.strip())
            if sentence:
                output.append(sentence + '.')
            current_parts.clear()

    for line in lines:
        if not line.strip():
            flush()
            output.append('')
            continue

        # Count leading tabs for indent level (vault uses tabs)
        stripped = line.lstrip('\t')
        level = len(line) - len(stripped)

        # Hard skip: ticked checkbox lines must NEVER contribute to index content.
        # Must be checked before BULLET_RE, which would strip [x] and let the
        # done-item text leak through as plain prose.
        if TICKED_LINE_RE.match(stripped):
            # If this ticked line is at the top level, flush any pending group
            # so sub-bullets accumulated so far aren't orphaned under a done item.
            if level == 0:
                flush()
            # Skip this line completely — it is done.
            continue

        m = BULLET_RE.match(stripped)
        if m:
            content = m.group(2).strip()
            if not content:
                continue
            if level == 0:
                flush()
                current_parts.append(content)
            else:
                # Sub-bullet: append to current top-level group
                current_parts.append(content)
        else:
            # Non-bullet line (heading, plain text, etc.)
            flush()
            output.append(line.strip())

    flush()
    # Clean up excessive blank lines
    result = re.sub(r'\n{3,}', '\n\n', '\n'.join(output))
    return result.strip()


def _extract_daily_content(text: str) -> tuple[str, list[str]]:
    """
    Extract the main content and open todos from a daily note.

    Daily notes end with:
        ---
        ## To do
        ```tasks ... ```
        ## Projects
        ```tasks ... ```

    Everything above the last '---' is the actual note content.
    Returns (flattened_content, [open todo strings]).

    IMPORTANT: Only genuinely unchecked items (- [ ]) are returned as todos.
    Completed items (- [x] / - [X]) are stripped from the content and never
    returned as todos, no matter what.
    """
    lines = text.splitlines()

    # Find the last horizontal rule (separates content from Obsidian task queries)
    last_hr = -1
    for i, line in enumerate(lines):
        if HR_RE.match(line):
            last_hr = i

    content_lines = lines[:last_hr] if last_hr >= 0 else lines

    # Strip trailing blank lines
    while content_lines and not content_lines[-1].strip():
        content_lines.pop()

    raw_content = '\n'.join(content_lines)

    # Extract ONLY unchecked todos — lines with literal "- [ ]" (space, not x).
    # Belt-and-suspenders: also drop any match that somehow contains [x]/[X].
    todos_raw = [
        t for t in OPEN_TODO_RE.findall(raw_content)
        if not re.search(r'\[[xX]\]', t)
    ]

    # Remove completed (ticked-off) items from content before indexing.
    # Completed items have no value in vault search results — they're done.
    # Open [ ] items remain and are also indexed as separate todo points.
    content_for_index = re.sub(
        r'^\t*[-*+]\s+\[[xX]\]\s+.*$', '', raw_content, flags=re.MULTILINE
    )

    # Clean and flatten content
    content = _strip_obsidian_syntax(content_for_index)
    content = _flatten_bullets(content)

    # Clean todos
    todos = [_strip_obsidian_syntax(t).strip() for t in todos_raw]
    todos = [t for t in todos if t]

    return content.strip(), todos


# ---------------------------------------------------------------------------
# Source-file todo verification
# ---------------------------------------------------------------------------

def _todo_still_open_in_file(vault_path: Path, filename: str, todo_text: str) -> bool:
    """
    Return True only if *todo_text* appears as an unchecked item in the source
    file right now.  This is the ground-truth check used at query time to
    filter out stale Qdrant points that were indexed when the item was open
    but have since been ticked off in the vault.

    Strategy:
    1. Read the source file.
    2. The todo_text stored in Qdrant has the form
       "Open todo (YYYY-MM-DD): <raw todo text>" — strip that prefix first.
    3. Look for the raw todo text on any line that starts with "- [ ]".
       If we find it on a "- [ ]" line, the item is still open.
       If we only find it on a "- [x]" / "- [X]" line (or not at all), it's done.
    """
    path = vault_path / filename
    if not path.exists():
        # File gone — treat as completed/deleted
        return False

    try:
        file_content = path.read_text(errors="replace")
    except Exception:
        return False

    # Strip the "Open todo (date): " prefix that the indexer adds
    raw_text = re.sub(r'^Open todo \(\d{4}-\d{2}-\d{2}\):\s*', '', todo_text).strip()

    if not raw_text:
        return False

    # Search every line of the file (not just the content section before ---)
    # because the user might tick it off anywhere.
    raw_lower = raw_text.lower()
    for line in file_content.splitlines():
        stripped = line.strip()
        if raw_lower not in stripped.lower():
            continue
        # Found the text on this line — check the checkbox state.
        if re.match(r'^[-*+]\s+\[ \]\s+', stripped):
            return True   # still open — confirmed
        if re.match(r'^[-*+]\s+\[[xX]\]\s+', stripped):
            return False  # ticked off — done, stop immediately
        # Text found in non-checkbox prose — keep scanning.

    # The todo text was not found on any open-checkbox line.
    # It's either been ticked off or deleted — either way it's no longer open.
    return False


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

def _chunks_for_file(path: Path) -> list[dict]:
    """
    Preprocess and chunk a vault file. Returns a list of chunk dicts with keys:
    doc_type, date, heading, content.
    """
    rel = path.relative_to(VAULT_PATH)
    parts = rel.parts
    is_daily = (
        parts[0] == "daily"
        and len(parts) == 2
        and re.match(r'^\d{4}-\d{2}-\d{2}\.md$', parts[1])
    )

    text = path.read_text(errors="replace")
    chunks: list[dict] = []

    if is_daily:
        date_str = parts[1].replace('.md', '')
        content, todos = _extract_daily_content(text)

        # One chunk per top-level bullet group so each is its own vector,
        # not one giant blob where the relevant part gets buried.
        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue
            chunks.append({
                "doc_type": "daily_note",
                "date": date_str,
                "heading": f"Daily note {date_str}",
                "content": f"Daily note {date_str}: {line}",
            })

        for todo in todos:
            chunks.append({
                "doc_type": "todo",
                "date": date_str,
                "heading": "Open todo",
                "content": f"Open todo ({date_str}): {todo}",
            })

    else:
        # Regular note: strip ticked lines, then strip Obsidian syntax, then flatten.
        note_title = parts[-1].replace('.md', '')
        # Belt-and-suspenders: strip completed checkbox lines from the raw text
        # BEFORE any further processing so that ticked items never appear in the
        # index even as plain prose.  _flatten_bullets has the same guard for
        # anything that slips through (e.g. after _strip_obsidian_syntax).
        text_no_done = re.sub(
            r'^\t*[-*+]\s+\[[xX]\]\s+.*$', '', text, flags=re.MULTILINE
        )
        cleaned = _strip_obsidian_syntax(text_no_done)
        cleaned = _flatten_bullets(cleaned)

        sections = re.split(r'(?m)^(#{1,3} .+)$', cleaned)
        heading = ""
        for part in sections:
            part = part.strip()
            if not part:
                continue
            if HEADING_RE.match(part):
                heading = part
            else:
                # One chunk per line (flattened bullet group or plain paragraph)
                prefix = f"{note_title} — {heading}: " if heading else f"{note_title}: "
                for line in part.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    chunks.append({
                        "doc_type": "note",
                        "date": None,
                        "heading": heading,
                        "content": f"{prefix}{line}",
                    })

        if not chunks and cleaned.strip():
            chunks = [{"doc_type": "note", "date": None, "heading": "", "content": cleaned.strip()}]

    return chunks


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

def _current_vault_commit() -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(VAULT_PATH), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        return result.stdout.strip() if result.returncode == 0 else None
    except Exception:
        return None


def _changed_files(since_commit: str) -> list[Path]:
    try:
        result = subprocess.run(
            ["git", "-C", str(VAULT_PATH), "diff", "--name-only", since_commit, "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return list(VAULT_PATH.rglob("*.md"))
        paths = [VAULT_PATH / p for p in result.stdout.splitlines() if p.endswith(".md")]
        return [p for p in paths if p.exists()]
    except Exception:
        return list(VAULT_PATH.rglob("*.md"))


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

async def _get_indexed_commit() -> str | None:
    async with SessionLocal() as session:
        result = await session.execute(
            sa.text("SELECT value FROM indexer_state WHERE key = 'vault_commit'")
        )
        row = result.first()
        return row.value if row else None


async def _save_indexed_commit(commit: str) -> None:
    async with SessionLocal() as session:
        async with session.begin():
            await session.execute(
                sa.text("""
                    INSERT INTO indexer_state (key, value) VALUES ('vault_commit', :v)
                    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
                """),
                {"v": commit},
            )


# ---------------------------------------------------------------------------
# Stale-point pruning
# ---------------------------------------------------------------------------

async def _delete_stale_todo_points(rel: str) -> int:
    """
    Delete all Qdrant points for *rel* whose doc_type is 'todo' but whose
    todo text is no longer an open checkbox in the source file right now.

    Called whenever we re-index a file so that completions are reflected
    immediately — even before the next full reindex cycle.

    Returns the number of points deleted.
    """
    try:
        points, _ = await qdrant.scroll(
            collection_name=COLLECTION,
            scroll_filter=Filter(must=[
                FieldCondition(key="filename", match=MatchValue(value=rel)),
                FieldCondition(key="doc_type", match=MatchValue(value="todo")),
            ]),
            limit=500,
            with_payload=True,
            with_vectors=False,
        )
    except Exception as e:
        print(f"Indexer: scroll for stale todo cleanup failed ({rel}): {e}")
        return 0

    stale_ids = []
    for p in points:
        content = p.payload.get("content", "")
        if not _todo_still_open_in_file(VAULT_PATH, rel, content):
            stale_ids.append(p.id)

    if stale_ids:
        try:
            from qdrant_client.models import PointIdsList
            await qdrant.delete(
                collection_name=COLLECTION,
                points_selector=PointIdsList(points=stale_ids),
            )
            print(f"Indexer: pruned {len(stale_ids)} completed todo point(s) from {rel}")
        except Exception as e:
            print(f"Indexer: delete stale points failed ({rel}): {e}")

    return len(stale_ids)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def reindex() -> None:
    """Index any vault files changed since the last run. Safe to call repeatedly."""
    if not VAULT_PATH.exists():
        print(f"Indexer: vault path {VAULT_PATH} does not exist — skipping")
        return

    current = await asyncio.to_thread(_current_vault_commit)
    if not current:
        print(f"Indexer: could not read git HEAD from {VAULT_PATH} — skipping")
        return

    last = await _get_indexed_commit()
    print(f"Indexer: last indexed={last or 'none'}, current={current[:8]}")

    if last == current:
        print("Indexer: vault up to date, nothing to index")
        return

    all_files = (
        await asyncio.to_thread(_changed_files, last)
        if last
        else list(VAULT_PATH.rglob("*.md"))
    )
    files = [f for f in all_files if _is_indexable(f)]

    if not files:
        await _save_indexed_commit(current)
        print("Indexer: no indexable .md files found")
        return

    # Full reindex (no prior commit): wipe the collection first to clear any
    # duplicates from previous runs that used unstable hash() point IDs.
    if not last:
        print("Indexer: full reindex — recreating obsidian collection")
        from .vector_store import client as qdrant_client
        from qdrant_client.models import Distance, VectorParams
        await qdrant_client.delete_collection(COLLECTION)
        await qdrant_client.create_collection(
            collection_name=COLLECTION,
            vectors_config=VectorParams(size=1536, distance=Distance.COSINE),
        )

    print(f"Indexer: embedding {len(files)} changed vault file(s)")
    for path in files:
        rel = str(path.relative_to(VAULT_PATH))

        # Before upserting new chunks, prune any existing todo points for this
        # file that are now ticked off. This handles the case where a daily note
        # is re-indexed after the user ticked some items — old open-todo points
        # are deleted before fresh ones (only truly open) are added.
        await _delete_stale_todo_points(rel)

        chunks = _chunks_for_file(path)
        for i, chunk in enumerate(chunks):
            text = chunk["content"]
            if not text:
                continue
            vector = await embed(text)
            point_id = int(hashlib.md5(f"{rel}:{i}".encode()).hexdigest()[:16], 16)
            await qdrant.upsert(
                collection_name=COLLECTION,
                points=[PointStruct(
                    id=point_id,
                    vector=vector,
                    payload={
                        "filename": rel,
                        "doc_type": chunk["doc_type"],
                        "date": chunk.get("date"),
                        "heading": chunk["heading"],
                        "chunk_index": i,
                        "content": text,
                    },
                )],
            )

    await _save_indexed_commit(current)
    print(f"Indexer: done (commit {current[:8]})")
