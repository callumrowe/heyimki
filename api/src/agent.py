import asyncio
import base64
import io
import json
import os
import re
from datetime import datetime, date as date_type, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import anthropic
import httpx
import openai as _openai

from . import memory as mem
from .config import CODING_AGENT_ENABLED, USER_LABEL, render as _render_prompt
from .memory import minutes_since_last_message, save_user_message, get_recent_conversation_for_display
from .actions import execute_pending_action, get_pending_action, resolve_pending_action, save_pending_action
from . import logbuffer, self_deploy
from .tools.browser import browser_act, navigate  # both return (result, screenshot_b64|None)
from .tools.curl import run_curl
from .tools.obsidian import append_to_note, list_vault_directory, read_note, read_vault_pdf, search_vault, write_note
from .integrations.loader import load_integrations
from .integrations.tts import generate_speech

client = anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"], max_retries=3)
_openai_client = _openai.AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY", ""))
_USER_TZ = ZoneInfo((os.environ.get("USER_TZ") or "UTC"))

MODEL = "claude-sonnet-4-6"
SESSION_GAP_HOURS = float(os.environ.get("SESSION_GAP_HOURS", "0.5"))
MIN_VAULT_SCORE = 0.35  # minimum cosine similarity to auto-inject a vault excerpt
CODING_AGENT_URL = os.environ.get("CODING_AGENT_URL", "http://coding-agent:3002")
# The coding agent has its own secret, separate from INTERNAL_SECRET, so code
# running there can't call the api as the user.
_CODING_AGENT_HEADERS = {"Authorization": f"Bearer {os.environ.get('CODING_AGENT_SECRET', '')}"}

_REPLY_QUOTE_RE = re.compile(r'^> ([\s\S]*?)\n\n([\s\S]*)$')


def _strip_emojis(text: str) -> str:
    try:
        import regex as _regex_mod
        cleaned = _regex_mod.sub(r'\p{Emoji}', '', text)
    except ImportError:
        _emoji_re = re.compile(
            "[\U0001F600-\U0001F64F"
            "\U0001F300-\U0001F5FF"
            "\U0001F680-\U0001F6FF"
            "\U0001F1E0-\U0001F1FF"
            "\U00002600-\U000027BF"
            "\U0001F900-\U0001F9FF"
            "\U0001FA00-\U0001FA6F"
            "\U0001FA70-\U0001FAFF"
            "\U00002702-\U000027B0"
            "\U000024C2-\U0001F251"
            "]+",
            flags=re.UNICODE,
        )
        cleaned = _emoji_re.sub('', text)
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
    return cleaned.strip()


# ── /kill infrastructure ──────────────────────────────────────────────────────
_kill_event: asyncio.Event = asyncio.Event()
_kill_callback = None


def register_kill_callback(fn) -> None:
    global _kill_callback
    _kill_callback = fn


async def _clear_kill_event(delay: float) -> None:
    await asyncio.sleep(delay)
    _kill_event.clear()


async def _kill_all() -> None:
    """Set the kill event, signal external services, and cancel active tasks."""
    _kill_event.set()
    try:
        if not CODING_AGENT_ENABLED:
            raise RuntimeError("coding agent not enabled")
        async with httpx.AsyncClient(timeout=3, headers=_CODING_AGENT_HEADERS) as c:
            await c.post(f"{CODING_AGENT_URL}/kill")
    except Exception as e:
        print(f"[kill] coding-agent signal failed: {e}")
    try:
        from .browser_manager import get_browser_manager
        get_browser_manager().cancel()
    except Exception as e:
        print(f"[kill] browser cancel failed: {e}")
    if _kill_callback:
        try:
            _kill_callback()
        except Exception as e:
            print(f"[kill] task cancel callback failed: {e}")
    asyncio.create_task(_clear_kill_event(2))


_VAULT_GIT_ALLOWED = {"log", "show", "ls-tree", "ls-files", "diff", "status", "cat-file",
                      "rev-list", "branch", "tag", "rev-parse"}


async def _vault_git_readonly(args: list[str]) -> str:
    """Run an allow-listed, read-only git command against the vault."""
    from .tools.obsidian import GIT_ENV, VAULT, vault_is_git
    if not vault_is_git():
        return "The vault is not a git repository, so there is no history to inspect."
    if not args or args[0] not in _VAULT_GIT_ALLOWED:
        return f"Subcommand not allowed. Allowed: {', '.join(sorted(_VAULT_GIT_ALLOWED))}"
    if any(a.startswith("--output") for a in args):
        return "Writing output files is not allowed."
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", str(VAULT), *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=GIT_ENV,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=30)
    except asyncio.TimeoutError:
        proc.kill()
        return "git command timed out after 30s"
    text = (out or err).decode("utf-8", errors="replace").strip()
    return text[:50000] or "(no output)"


async def _run_deploy() -> str:
    """The /deploy command: the user typed it, so deploy the coding agent's work now."""
    if not CODING_AGENT_ENABLED:
        return "The coding agent isn't enabled, so there's nothing to deploy."
    try:
        prep = await self_deploy.prepare()
        if prep is None:
            return "Nothing to deploy: the coding agent has no undeployed changes."
        return self_deploy.describe(prep) + "\n\n" + await self_deploy.apply(prep["base"], prep["head"])
    except Exception as e:
        return f"Deploy failed: {e}"


def _annotate_reply_quote(msg: str) -> str:
    """
    Transform "> {quote}\\n\\n{reply}" into "{reply}\\n\\n[Replying to: "{short}"]".
    The full quoted text is stripped so Ki doesn't re-engage with already-handled items,
    but a short reference is kept so Ki knows which message the user is responding to.
    The original (with full quote) is always stored in the DB for display.
    """
    m = _REPLY_QUOTE_RE.match(msg)
    if not m:
        return msg
    quoted = m.group(1).strip()
    reply = m.group(2).strip()
    short = quoted[:80] + ('…' if len(quoted) > 80 else '')
    return f'{reply}\n\n[Replying to your message: "{short}"]'


async def _create_with_retry(**kwargs):
    """Call client.messages.create with exponential backoff on rate limit and overload errors."""
    for attempt in range(4):
        try:
            return await client.messages.create(**kwargs)
        except (anthropic.RateLimitError, anthropic.InternalServerError) as e:
            if attempt == 3:
                raise
            wait = 2 ** attempt * 15  # 15s, 30s, 60s
            print(f"[agent] retryable error, retrying in {wait}s (attempt {attempt + 1}/4): {e}")
            await asyncio.sleep(wait)




# Load integrations once at startup
_INTEGRATIONS = load_integrations()

# Build a lookup: tool name → integration module
_INTEGRATION_TOOL_MAP: dict[str, object] = {}
for _mod in _INTEGRATIONS:
    for _schema in _mod.TOOL_SCHEMAS:
        _INTEGRATION_TOOL_MAP[_schema["name"]] = _mod

_STATIC_TOOLS = [
    {
        "name": "memory_save",
        "description": (
            "Persist something worth remembering about the user for future conversations. "
            "Choose the right category:\n"
            "- preference: Subjective opinions, likes, dislikes, how the user prefers to work or communicate. "
            "Examples: 'The user prefers direct feedback without softening', 'The user appreciates dry humor in responses'.\n"
            "- person: Information about specific people in the user's life — name, relationship, location, key facts. "
            "Examples: '<name> is the user\\'s best friend, lives in <city>', '<name> is the user\\'s partner'.\n"
            "- fact: Everything else about the user — locations, dates, status, events, context. "
            "Examples: 'The user is waiting on a visa decision', 'The user\\'s mobile is +64...'.\n"
            "  This includes movement and logistics — where the user currently is, where they're going, and when. "
            "Examples: 'The user is currently in <city>', 'The user is travelling next week', "
            "'The user is back home as of mid-April'. "
            "IMPORTANT: location/movement facts are transient — when saving one, check 'What you know about the user' "
            "for any existing location fact and use memory_update to replace it rather than saving a new one. "
            "Never let two contradictory location facts coexist.\n\n"
            "Do NOT save: todos, tasks, or closed-loop acknowledgements ('marked as done', 'no further follow-up', "
            "'user appreciated Ki\\'s response about') — these are noise, not memories.\n\n"
            "Before saving, check 'What you know about the user' — if the same or equivalent memory exists, "
            "use memory_update instead. Never save duplicates or near-duplicates."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "enum": ["preference", "fact", "person"],
                    "description": "preference = likes/dislikes/working style; person = people in user's life; fact = everything else about the user",
                },
                "content": {
                    "type": "string",
                    "description": "The thing to remember, written as a clear standalone sentence.",
                },
            },
            "required": ["category", "content"],
        },
    },
    {
        "name": "memory_update",
        "description": (
            "Update an existing memory with corrected or more complete information. "
            "Use when a fact has changed, been corrected, or can be enriched. "
            "Prefer this over saving a new memory when the topic already exists. "
            "The id is shown in the 'What you know about the user' section as [id:N category]."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {
                    "type": "integer",
                    "description": "The numeric id of the memory to update.",
                },
                "content": {
                    "type": "string",
                    "description": "The new content to replace the old memory with.",
                },
            },
            "required": ["id", "content"],
        },
    },
    {
        "name": "memory_delete",
        "description": (
            "Delete a memory that is no longer relevant — e.g. a task that's been completed, "
            "a fact that has been superseded, or something the user says to forget. "
            "The id is shown in the 'What you know about the user' section as [id:N category]."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {
                    "type": "integer",
                    "description": "The numeric id of the memory to delete.",
                },
            },
            "required": ["id"],
        },
    },
    {
        "name": "recall_past_conversations",
        "description": (
            "Search past conversation history for relevant context. "
            "Use when the user references something discussed previously, or when you want to check "
            "if a topic has come up before. Returns the most semantically similar past exchanges. "
            "Examples: 'what did we say about the project timeline?', 'have we discussed this before?'"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What to search for in past conversations.",
                }
            },
            "required": ["query"],
        },
    },
    {
        "name": "search_vault",
        "description": (
            "Semantically search the user's Obsidian vault notes. "
            "Use when the user asks about something they may have written about, "
            "or when relevant context might exist in their notes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to search for"},
                "limit": {"type": "integer", "description": "Max results (default 5)", "default": 5},
                "doc_type": {
                    "type": "string",
                    "description": "Optional filter: 'note' (regular notes), 'daily_note' (daily journal entries), 'todo' (open todos only)",
                    "enum": ["note", "daily_note", "todo"],
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "read_note",
        "description": "Read the full contents of a specific vault note by its relative path.",
        "input_schema": {
            "type": "object",
            "properties": {
                "filename": {"type": "string", "description": "Relative path, e.g. 'folder/note.md'"},
            },
            "required": ["filename"],
        },
    },
    {
        "name": "append_to_note",
        "description": (
            "Append content to an existing vault note. "
            "Today's daily note is at daily/YYYY-MM-DD.md — always use the actual date. "
            "For daily notes, content is automatically inserted into the '## ✏️ Daily notes' section. "
            "Format daily note content as a '- ' bullet point with tab-indented sub-bullets if needed. No headers."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "filename": {"type": "string", "description": "Relative path, e.g. 'daily/2026-03-14.md'"},
                "content": {"type": "string", "description": "Text to append"},
                "commit_message": {"type": "string", "description": "Optional git commit message"},
            },
            "required": ["filename", "content"],
        },
    },
    {
        "name": "write_note",
        "description": "Create a new vault note. Refuses to overwrite existing notes by default.",
        "input_schema": {
            "type": "object",
            "properties": {
                "filename": {"type": "string", "description": "Relative path, e.g. 'folder/note.md'"},
                "content": {"type": "string", "description": "Full note content"},
                "overwrite": {"type": "boolean", "description": "Set true to replace existing note", "default": False},
            },
            "required": ["filename", "content"],
        },
    },
    {
        "name": "read_vault_pdf",
        "description": (
            "Extract and return the text content of a PDF file stored in the Obsidian vault. "
            "Use when the user asks you to read or reason over a PDF document saved in the vault. "
            "Path is relative to the vault root, e.g. 'INSURANCE DOCS/appeal-letter.pdf'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Vault-relative path to the PDF, e.g. 'INSURANCE DOCS/claim-form.pdf'"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "list_vault_directory",
        "description": (
            "List the files and subdirectories inside a vault directory. "
            "Use when you need to see what files exist in a folder before reading them, "
            "e.g. 'INSURANCE DOCS' or 'daily'. Returns filenames and relative paths."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "directory": {
                    "type": "string",
                    "description": "Vault-relative directory path, e.g. 'INSURANCE DOCS'. Omit or leave empty for vault root.",
                    "default": "",
                },
            },
            "required": [],
        },
    },
    {
        "name": "browser_navigate",
        "description": (
            "Open a URL in a headless browser and return the page's readable text content. "
            "Use for simple read-only lookups: prices, schedules, articles, live data. "
            "For anything interactive (filling forms, clicking, booking, submitting), use browser_act instead."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The full URL to navigate to"},
            },
            "required": ["url"],
        },
    },
    {
        "name": "browser_act",
        "description": (
            "Execute a multi-step interactive browser task: fill forms, click buttons, book things, submit data. "
            "Describe the goal in plain English. For actions with real-world consequences (purchases, bookings, "
            "sending messages), always call request_confirmation first with action_name='browser_act' and the "
            "task in the payload, then let the user confirm before proceeding."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "Plain-English description of what to do in the browser"},
            },
            "required": ["task"],
        },
    },
    {
        "name": "run_curl",
        "description": (
            "Execute a curl command and return its stdout, stderr, and exit code. "
            "Use for making HTTP requests, testing APIs, downloading content, or any task "
            "where a raw curl command is the most direct approach. "
            "The command must start with 'curl'. Times out after 15 seconds. "
            "Returns a JSON object: {\"stdout\": \"...\", \"stderr\": \"...\", \"exit_code\": 0}."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The full curl command to execute, exactly as you would type it in a shell. Must start with 'curl'.",
                },
            },
            "required": ["command"],
        },
    },
    {
        "name": "vault_git",
        "description": (
            "Run a read-only git command against the Obsidian vault (only works if the vault is a git repo). "
            "Use this to inspect vault history, find deleted files, or recover file content from past commits. "
            "Examples:\n"
            "- Find a deleted file: vault_git(args=['log','--all','--full-history','--oneline','--','*partial-name*'])\n"
            "- List all paths ever matching a name: vault_git(args=['log','--all','--full-history','--name-only','--format=','--','*partial-name*'])\n"
            "- Recover file content: vault_git(args=['show','<commit>:<path/to/file.md>'])\n"
            "- List files at a commit: vault_git(args=['ls-tree','-r','--name-only','<commit>'])\n"
            "Allowed subcommands: log, show, ls-tree, ls-files, diff, status, cat-file, rev-list, branch, tag, rev-parse"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "git arguments, e.g. ['log','--all','--oneline','--','*partial-name*'] or ['show','abc1234:path/to/file.md']",
                },
            },
            "required": ["args"],
        },
    },
    {
        "name": "delegate_coding_task",
        "description": (
            "Ask the coding agent to write or edit any part of Ki's codebase: integrations, tools, prompts, "
            "core Python (agent.py, main.py, heartbeat.py, etc.), the WhatsApp gateway, the web app, migrations, "
            "or anything else in the repo. "
            "It works in its own copy of the repo: it reads the code, makes the changes, checks syntax and commits. "
            "Nothing it does is live until it's deployed. "
            "You do NOT need to figure out which files to touch or pre-analyse the code — just relay what the user asked for. "
            "Recent conversation is automatically included so the coding agent has full context. "
            "After it finishes, tell the user what changed and any env vars to add, then call deploy_changes, "
            "which shows them the changes and asks them to confirm. "
            "If the result starts with FAILED or contains WARNING, tell the user exactly what went wrong — never report success if something failed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": (
                        "What the user asked for, in as close to their original words as possible. "
                        "Include any constraints or preferences they mentioned. "
                        "Do NOT pre-analyse the codebase or list specific files — the coding agent will do that itself."
                    ),
                },
            },
            "required": ["task"],
        },
    },
    {
        "name": "get_service_logs",
        "description": (
            "Fetch recent log output from Ki's api (which includes the browser tools) or the coding agent. "
            "Use to diagnose crashes, errors, or unexpected behaviour. "
            "Other services' logs aren't reachable from here: the user can run `docker compose logs <service>`."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "service": {"type": "string", "enum": ["api", "coding-agent"], "description": "'api' or 'coding-agent'"},
                "lines": {"type": "integer", "description": "Number of log lines to fetch (default 100, max 2000)"},
            },
            "required": ["service"],
        },
    },
    {
        "name": "deploy_changes",
        "description": (
            "Deploy the coding agent's changes to the live Ki. This does NOT deploy straight away: "
            "it collects the changes and asks the user to confirm. Show the user the description it returns "
            "(what changed, and whether Ki restarts or needs a rebuild), then wait for them to reply confirm or cancel. "
            "Call it after delegate_coding_task, or whenever the user asks to deploy."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "discard_coding_changes",
        "description": (
            "Throw away the coding agent's undeployed changes, so its next task starts from the live code. "
            "Use when the user doesn't want changes the coding agent made."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "send_file",
        "description": (
            "Send a text file (.txt) back to the user via WhatsApp. "
            "Use when the user asks you to produce output as a file — e.g. a document, report, or list. "
            "Write the full file content as plain text in the 'content' field."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "filename": {"type": "string", "description": "Filename including .txt extension, e.g. 'summary.txt'"},
                "content": {"type": "string", "description": "Full text content of the file"},
            },
            "required": ["filename", "content"],
        },
    },
    {
        "name": "request_confirmation",
        "description": (
            "Ask the user to confirm before taking a consequential or irreversible action — "
            "e.g. submitting a form, sending a message, making a payment. "
            "Store all parameters needed to execute the action in payload. "
            "The action will be executed automatically if the user replies 'confirm'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action_name": {
                    "type": "string",
                    "description": "Internal action identifier matching a registered executor, e.g. 'browser_navigate'",
                },
                "details": {
                    "type": "string",
                    "description": "Human-readable description of exactly what will happen",
                },
                "payload": {
                    "type": "object",
                    "description": "All parameters required to execute the action on confirmation",
                },
            },
            "required": ["action_name", "details", "payload"],
        },
    },
]

# Self-modification tools are only offered when the coding-agent profile is on.
_CODING_AGENT_TOOLS = {"delegate_coding_task", "deploy_changes", "discard_coding_changes", "get_service_logs"}
if not CODING_AGENT_ENABLED:
    _STATIC_TOOLS = [t for t in _STATIC_TOOLS if t["name"] not in _CODING_AGENT_TOOLS]

# Integration tools appended after static tools
_integration_tool_schemas = [s for mod in _INTEGRATIONS for s in mod.TOOL_SCHEMAS]

_all_tools = _STATIC_TOOLS + _integration_tool_schemas

# Add cache_control to the last tool (caches the full static tools block)
TOOLS = _all_tools[:-1] + [{**_all_tools[-1], "cache_control": {"type": "ephemeral"}}]


async def _forward_to_whatsapp(text: str) -> None:
    """Forward a reply to the user via Baileys — used when desktop is in WhatsApp channel mode."""
    baileys_url = os.environ.get("BAILEYS_URL", "http://baileys:3000")
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            await c.post(
                f"{baileys_url}/send",
                json={"secret": os.environ.get("INTERNAL_SECRET", ""), "text": text},
            )
        print(f"[whatsapp] mirrored reply ({len(text)} chars)")
    except Exception as e:
        print(f"[whatsapp] failed to mirror reply: {e}")


async def _send_voice_note(text: str) -> None:
    """Generate TTS from text and send as a WhatsApp voice note via Baileys."""
    print(f"[tts] _send_voice_note called, text_len={len(text)}")
    audio_bytes = await generate_speech(text)
    if not audio_bytes:
        return
    baileys_url = os.environ.get("BAILEYS_URL", "http://baileys:3000")
    jid = os.environ.get("YOUR_WHATSAPP_JID", "")
    secret = os.environ.get("INTERNAL_SECRET", "")
    url = f"{baileys_url}/send-voice-note?to={jid}&secret={secret}"
    print(f"[tts] posting {len(audio_bytes):,} bytes to {baileys_url}/send-voice-note")
    try:
        async with httpx.AsyncClient(timeout=30.0) as c:
            r = await c.post(url, content=audio_bytes, headers={"Content-Type": "audio/mpeg"})
            r.raise_for_status()
        print(f"[tts] voice note sent ({len(audio_bytes):,} bytes)")
    except Exception as e:
        print(f"[tts] failed to send voice note: {e}")


def _detect_image_media_type(data: bytes) -> str:
    """Detect image format from magic bytes."""
    if data[:4] == b'\x89PNG':
        return "image/png"
    if data[:2] == b'\xff\xd8':
        return "image/jpeg"
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return "image/webp"
    return "image/png"  # safe fallback


async def _send_screenshot(screenshot_b64: str, caption: str = "") -> None:
    """Forward a browser screenshot to the user via Baileys."""
    baileys_url = os.environ.get("BAILEYS_URL", "http://baileys:3000")
    try:
        async with httpx.AsyncClient(timeout=15.0) as c:
            await c.post(
                f"{baileys_url}/send-image",
                json={
                    "secret": os.environ.get("INTERNAL_SECRET", ""),
                    "image_b64": screenshot_b64,
                    "caption": caption,
                },
            )
        print(f"[screenshot] sent to user: {caption!r}")
    except Exception as e:
        print(f"[screenshot] failed to send: {e}")


async def _run_tool(name: str, inputs: dict, channel: str = "whatsapp") -> str:
    try:
        if name == "memory_save":
            await mem.save_memory(
                category=inputs["category"],
                content=inputs["content"],
                source="conversation",
            )
            print(f"[tool] memory_save: {inputs['content'][:80]}")
            return "Saved."

        elif name == "memory_update":
            ok = await mem.update_memory(inputs["id"], inputs["content"])
            print(f"[tool] memory_update id={inputs['id']}: {inputs['content'][:80]}")
            return "Updated." if ok else "Memory not found."

        elif name == "memory_delete":
            ok = await mem.delete_memory(inputs["id"])
            print(f"[tool] memory_delete id={inputs['id']}")
            return "Deleted." if ok else "Memory not found."

        elif name == "search_vault":
            results = await search_vault(inputs["query"], inputs.get("limit", 5), inputs.get("doc_type"))
            hits = [f"{r['score']:.2f}:{r['filename']}" for r in results]
            print(f"[tool] search_vault({inputs['query']!r}) → {hits}")
            if not results:
                return "No results found."
            return "\n\n".join(
                f"[{r['filename']}] {('(' + r['date'] + ') ') if r.get('date') else ''}{r['heading']}\n{r['excerpt']}"
                for r in results
            )

        elif name == "read_note":
            print(f"[tool] read_note({inputs['filename']!r})")
            return await read_note(inputs["filename"])

        elif name == "append_to_note":
            print(f"[tool] append_to_note({inputs['filename']!r})")
            return await append_to_note(
                inputs["filename"], inputs["content"], inputs.get("commit_message")
            )

        elif name == "write_note":
            print(f"[tool] write_note({inputs['filename']!r})")
            return await write_note(
                inputs["filename"], inputs["content"], inputs.get("overwrite", False)
            )

        elif name == "read_vault_pdf":
            print(f"[tool] read_vault_pdf({inputs['path']!r})")
            return await read_vault_pdf(inputs["path"])

        elif name == "list_vault_directory":
            directory = inputs.get("directory", "")
            print(f"[tool] list_vault_directory({directory!r})")
            return await list_vault_directory(directory)

        elif name == "browser_navigate":
            print(f"[tool] browser_navigate({inputs['url']!r})")
            result, screenshot = await navigate(inputs["url"])
            if screenshot:
                await _send_screenshot(screenshot, caption=f"Error screenshot — {inputs['url']}")
            return result

        elif name == "browser_act":
            print(f"[tool] browser_act: {inputs['task']!r}")
            result, screenshot = await browser_act(inputs["task"])
            if screenshot:
                is_error = result.startswith("Browser agent error")
                caption = "Error — last browser state" if is_error else "Last browser state"
                await _send_screenshot(screenshot, caption=caption)
            return result

        elif name == "vault_git":
            args = [str(a) for a in inputs.get("args", [])]
            print(f"[tool] vault_git: {args}")
            return await _vault_git_readonly(args)

        elif name == "run_curl":
            print(f"[tool] run_curl: {inputs['command'][:120]!r}")
            return await run_curl(inputs["command"])

        elif name == "delegate_coding_task":
            print(f"[tool] delegate_coding_task: {inputs['task'][:80]!r}")
            # Fetch recent conversation so the coding agent has full context to diagnose from
            recent_msgs = await mem.get_recent_conversation(limit=20, session_gap_hours=4.0, channel=channel)
            conversation_context = []
            for msg in recent_msgs:
                role = msg.get("role", "")
                content = msg.get("content", "")
                if isinstance(content, list):
                    content = " ".join(
                        b.get("text", "") for b in content
                        if isinstance(b, dict) and b.get("type") == "text"
                    ).strip()
                if role and content:
                    label = USER_LABEL if role == "user" else "Ki"
                    conversation_context.append(f"{label}: {content}")
            async with httpx.AsyncClient(timeout=1200.0, headers=_CODING_AGENT_HEADERS) as c:
                r = await c.post(f"{CODING_AGENT_URL}/code", json={
                    "task": inputs["task"],
                    "conversation_context": conversation_context,
                })
                result = r.json()
            # Log coding agent token usage
            try:
                ca_input = result.get("input_tokens", 0)
                ca_output = result.get("output_tokens", 0)
                if ca_input or ca_output:
                    await mem.log_token_usage("claude-sonnet-4-6", ca_input, ca_output, use_case="coding_agent")
            except Exception as e:
                print(f"[tool] failed to log coding agent token usage: {e}")
            if result.get("error"):
                return f"FAILED: {result['error']}"
            parts = []
            if result.get("files_written"):
                parts.append(f"Written: {', '.join(result['files_written'])}")
            else:
                parts.append("WARNING: No files were written — the task may not have completed.")
            if result.get("required_env_vars"):
                parts.append(f"Required env vars: {', '.join(result['required_env_vars'])}")
            if result.get("summary"):
                parts.append(result["summary"])
            if result.get("pending_commits", 0) > 1:
                parts.append(f"Undeployed changes now cover: {', '.join(result.get('pending_files', []))}")
            return "\n".join(parts)

        elif name == "get_service_logs":
            service = inputs.get("service", "api")
            lines = max(1, min(int(inputs.get("lines", 100) or 100), 2000))
            print(f"[tool] get_service_logs: {service} ({lines} lines)")
            if service == "coding-agent":
                async with httpx.AsyncClient(timeout=20.0, headers=_CODING_AGENT_HEADERS) as c:
                    r = await c.post(f"{CODING_AGENT_URL}/logs", json={"lines": lines})
                    return r.json().get("output", "(no output)")
            if service in ("api", "browser"):
                return logbuffer.tail(lines) or "(no output)"
            return f"Logs for '{service}' aren't reachable from here. The user can run: docker compose logs {service}"

        elif name == "deploy_changes":
            print("[tool] deploy_changes")
            prep = await self_deploy.prepare()
            if prep is None:
                return "Nothing to deploy: the coding agent has no undeployed changes."
            details = self_deploy.describe(prep)
            await save_pending_action(
                "deploy_code", details, {"base": prep["base"], "head": prep["head"]}
            )
            return (
                "Not deployed yet. Show the user this and ask them to reply 'confirm' to deploy or 'cancel':\n\n"
                + details
            )

        elif name == "discard_coding_changes":
            print("[tool] discard_coding_changes")
            return await self_deploy.discard()

        elif name == "send_file":
            filename = inputs.get("filename", "file.txt")
            content = inputs["content"]
            data_b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")
            baileys_url = os.environ.get("BAILEYS_URL", "http://baileys:3000")
            print(f"[tool] send_file: {filename!r} ({len(content)} chars)")
            async with httpx.AsyncClient(timeout=30.0) as c:
                r = await c.post(
                    f"{baileys_url}/send-document",
                    json={
                        "secret": os.environ.get("INTERNAL_SECRET", ""),
                        "data": data_b64,
                        "filename": filename,
                        "mimetype": "text/plain",
                    },
                )
                r.raise_for_status()
            return f"Sent file: {filename}"

        elif name == "request_confirmation":
            print(f"[tool] request_confirmation: {inputs['details']!r}")
            await save_pending_action(inputs["action_name"], inputs["details"], inputs["payload"])
            return (
                f"Confirmation requested. I've asked the user: ready to {inputs['details']}. "
                "They must reply 'confirm' to proceed or 'cancel' to abort."
            )

        elif name == "recall_past_conversations":
            query = inputs["query"]
            print(f"[tool] recall_past_conversations: {query!r}")
            results = await mem.search_conversations(query, limit=4)
            if not results:
                return "No relevant past conversations found."
            lines = [
                f"[score: {r['score']}]\nUser: {r['user'][:300]}\nKi: {r['assistant'][:400]}"
                for r in results
            ]
            return "\n\n---\n\n".join(lines)

        elif name in _INTEGRATION_TOOL_MAP:
            mod = _INTEGRATION_TOOL_MAP[name]
            print(f"[tool] {name} → {mod.__name__.split('.')[-1]}")
            return await mod.run_tool(name, inputs)

        else:
            return f"Unknown tool: {name}"

    except Exception as e:
        print(f"[tool] error in {name}: {e}")
        return f"TOOL FAILED — {name}: {e}"


async def _load_prompt(filename: str) -> str:
    name = filename.removesuffix(".md")
    db_value = await mem.get_prompt(name)
    if db_value is not None:
        return _render_prompt(db_value)
    path = Path(__file__).parent.parent / "prompts" / filename
    return _render_prompt(path.read_text(encoding="utf-8").strip())


def _mark_last_cacheable(messages: list[dict]) -> list[dict]:
    """Return messages with cache_control on the last content block of the last message.

    Tells Anthropic to cache all context up to this point so subsequent requests
    sharing the same prefix (tool-loop iterations, next conversation turn) read
    from cache instead of re-billing those tokens at full price.
    """
    if not messages:
        return messages
    last = messages[-1]
    content = last.get("content", "")
    if isinstance(content, str):
        new_content = [{"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}]
    elif isinstance(content, list) and content:
        *rest, tail = content
        if isinstance(tail, dict):
            tail = {**tail, "cache_control": {"type": "ephemeral"}}
        new_content = [*rest, tail]
    else:
        return messages
    return messages[:-1] + [{**last, "content": new_content}]


def _current_timestamp_line() -> str:
    """Return a short timestamp prefix for injection into the user message."""
    tz = _USER_TZ
    now = datetime.now(tz)
    return f"Current time: {now.strftime('%A, %d %B %Y %H:%M %Z')}"


def _prepend_timestamp(messages: list[dict]) -> list[dict]:
    """Return a copy of messages with the current timestamp prepended to the last user message.

    The timestamp is injected into the text content of the last user turn so it
    stays out of the system prompt — keeping the dynamic system block stable
    between turns and allowing it to hit the prompt cache.
    """
    if not messages:
        return messages
    ts_line = _current_timestamp_line()
    last = messages[-1]
    if last.get("role") != "user":
        return messages
    content = last.get("content", "")
    if isinstance(content, str):
        new_content = f"{ts_line}\n\n{content}"
    elif isinstance(content, list):
        # Find the first text block and prepend the timestamp to it
        new_blocks = []
        prepended = False
        for block in content:
            if not prepended and isinstance(block, dict) and block.get("type") == "text":
                new_blocks.append({**block, "text": f"{ts_line}\n\n{block['text']}"})
                prepended = True
            else:
                new_blocks.append(block)
        if not prepended:
            # No text block found — add one at the front
            new_blocks = [{"type": "text", "text": ts_line}] + new_blocks
        new_content = new_blocks
    else:
        return messages
    return messages[:-1] + [{**last, "content": new_content}]


async def _build_system(
    memories: list[dict],
    vault_excerpts: list[dict],
    heartbeat_state: dict | None,
    session_note: str = "",
    vault_search_ok: bool = True,
    session_messages: list[dict] | None = None,
    cross_channel_context: list[dict] | None = None,
) -> list[dict]:
    """Return a two-block system prompt: cached static instructions + cached dynamic context.

    Block 1 — identity + instructions (cache_control: ephemeral, rarely changes)
    Block 2 — memories + heartbeat + tool rules (cache_control: ephemeral, changes only when
               memories/heartbeat update — no timestamp, so stays stable between turns)

    The current time is injected into the user message instead (see _prepend_timestamp).
    """
    tz = _USER_TZ
    now = datetime.now(tz)
    dynamic = ""  # no timestamp here — lives in the user message prefix

    # Build session topic terms for heartbeat suppression.
    # Items whose key words are already present in the current session get skipped —
    # Ki can see them in the conversation itself, repeating them is just noise.
    _STOPWORDS = {"that", "this", "with", "have", "from", "they", "will", "been", "were",
                  "what", "when", "your", "just", "also", "some", "more", "about", "there",
                  "which", "their", "then", "than", "into", "would", "could", "should",
                  "okay", "right", "going", "think", "know", "make", "need"}
    _session_terms: set[str] = set()
    if session_messages:
        for msg in session_messages[-20:]:
            text = msg.get("content", "")
            if isinstance(text, str):
                for w in text.lower().split():
                    w = w.strip(".,!?;:\"'()-")
                    if len(w) > 4 and w not in _STOPWORDS:
                        _session_terms.add(w)

    def _in_session(text: str) -> bool:
        """Return True if the item's key words are already well-covered by session messages."""
        if not _session_terms:
            return False
        sig = [w.strip(".,!?;:\"'()-") for w in text.lower().split()
               if len(w) > 4 and w.strip(".,!?;:\"'()-") not in _STOPWORDS]
        if not sig:
            return False
        return sum(1 for w in sig if w in _session_terms) / len(sig) >= 0.5

    if heartbeat_state:
        lines = []
        today_str = now.strftime("%Y-%m-%d")

        heartbeat_age_note = ""
        last_updated = heartbeat_state.get("last_updated")
        if last_updated:
            try:
                lu = datetime.fromisoformat(last_updated)
                if lu.tzinfo is None:
                    lu = lu.replace(tzinfo=timezone.utc)
                age_mins = int((datetime.now(timezone.utc) - lu).total_seconds() / 60)
                heartbeat_age_note = f" (synced {age_mins}min ago — may be stale)"
            except Exception:
                pass

        if heartbeat_state.get("energy_level"):
            lines.append(f"Energy/state: {heartbeat_state['energy_level']}")

        if heartbeat_state.get("current_projects"):
            projects = [p for p in heartbeat_state["current_projects"] if not _in_session(p)]
            if projects:
                proj_shown = projects[:8]
                overflow = len(projects) - 8
                proj_str = ", ".join(proj_shown) + (f" (+{overflow} more)" if overflow > 0 else "")
                lines.append("Projects: " + proj_str)

        if heartbeat_state.get("open_todos"):
            todos = [t for t in heartbeat_state["open_todos"] if not _in_session(t)]
            if todos:
                lines.append("Open todos: " + "; ".join(todos))

        if heartbeat_state.get("backlog_todos"):
            backlog = [t for t in heartbeat_state["backlog_todos"] if not _in_session(t)]
            if backlog:
                shown = backlog[:6]
                overflow = len(backlog) - 6
                bl_str = "; ".join(shown) + (f" (+{overflow} more)" if overflow > 0 else "")
                lines.append(f"Backlog ({len(backlog)}): " + bl_str)

        if heartbeat_state.get("ki_commitments"):
            commitments = [c for c in heartbeat_state["ki_commitments"] if not _in_session(c)]
            if commitments:
                _commitment_pattern = re.compile(r'^(\d{4}-\d{2}-\d{2}):\s*(.+)$')
                annotated = []
                for c in commitments:
                    m = _commitment_pattern.match(c.strip())
                    if m:
                        try:
                            target = date_type.fromisoformat(m.group(1))
                            today_d = date_type.fromisoformat(today_str)
                            days = (target - today_d).days
                            dow = target.strftime("%a")
                            if days == 0:
                                rel = "today"
                            elif days == 1:
                                rel = "tomorrow"
                            elif days > 0:
                                rel = f"+{days}d"
                            else:
                                rel = f"{abs(days)}d ago"
                            annotated.append(f"{m.group(1)} ({dow}, {rel}): {m.group(2)}")
                        except Exception:
                            annotated.append(c)
                    else:
                        annotated.append(c)
                lines.append("Ki promised: " + "; ".join(annotated))

        if heartbeat_state.get("open_loops"):
            loops = [l for l in heartbeat_state["open_loops"] if not _in_session(l)]
            if loops:
                lines.append("Open loops: " + "; ".join(loops))

        if heartbeat_state.get("recent_themes"):
            themes = heartbeat_state["recent_themes"][:5]
            lines.append("Recent themes: " + ", ".join(themes))

        imminent = []
        for s in heartbeat_state.get("snoozed", []):
            ra = s.get("resurface_after")
            if ra:
                try:
                    days_until = (date_type.fromisoformat(ra) - date_type.fromisoformat(today_str)).days
                    if 0 < days_until <= 3:
                        imminent.append(f"{s['item']} (resurfaces in {days_until}d)")
                except Exception:
                    pass
        if imminent:
            lines.append("Coming off snooze soon: " + "; ".join(imminent))

        if lines:
            dynamic += f"\n\n## Current context{heartbeat_age_note}\n" + "\n".join(f"- {l}" for l in lines)

    if memories:
        lines = "\n".join(f"- [id:{m['id']} {m['category']}] {m['content']}" for m in memories)
        dynamic += f"\n\n## What you know about the user\n{lines}"

    tool_rules = await mem.get_tool_rules()
    if tool_rules:
        lines = "\n".join(f"- {r['content']}" for r in tool_rules)
        dynamic += f"\n\n## Tool selection rules (learned from corrections)\n{lines}"

    if vault_excerpts:
        lines = "\n\n".join(
            f"[{v['filename']}] {v['heading']}\n{v['excerpt']}" for v in vault_excerpts
        )
        dynamic += f"\n\n## Relevant vault notes\n{lines}"

    if not vault_search_ok:
        dynamic += "\n\nNote: vault semantic search is currently unavailable (Qdrant may be down) — answers about notes may be incomplete. You can still use the search_vault tool but it may also fail."

    if cross_channel_context:
        channels = sorted(set(m["channel"] for m in cross_channel_context))
        lines = []
        for m in cross_channel_context:
            speaker = "You" if m["role"] == "assistant" else USER_LABEL
            lines.append(f"[{m['channel']}] {speaker}: {m['content'][:300]}")
        dynamic += (
            f"\n\n## Recent {'/'.join(channels)} context (session gap — not current conversation)\n"
            + "\n".join(lines)
        )

    if session_note:
        dynamic += f"\n\n{session_note}"

    identity, instructions = await asyncio.gather(_load_prompt("identity.md"), _load_prompt("instructions.md"))
    static_system = identity + "\n\n---\n\n" + instructions
    if CODING_AGENT_ENABLED:
        static_system += "\n\n" + _render_prompt(
            (Path(__file__).parent.parent / "prompts" / "coding_agent.md").read_text(encoding="utf-8").strip()
        )
    blocks = [{"type": "text", "text": static_system, "cache_control": {"type": "ephemeral"}}]
    # A fresh install has no memories, heartbeat or vault hits yet, so the dynamic
    # block can be empty — and the API rejects cache_control on an empty text block.
    if dynamic.strip():
        blocks.append({"type": "text", "text": dynamic.strip(), "cache_control": {"type": "ephemeral"}})
    return blocks


async def _transcribe_audio(data: str, mime_type: str) -> str:
    audio_bytes = base64.b64decode(data)
    ext = "ogg"
    if "mp4" in mime_type or "m4a" in mime_type:
        ext = "m4a"
    elif "webm" in mime_type:
        ext = "webm"
    elif "wav" in mime_type:
        ext = "wav"
    elif "mp3" in mime_type or "mpeg" in mime_type:
        ext = "mp3"
    buf = io.BytesIO(audio_bytes)
    buf.name = f"audio.{ext}"
    result = await _openai_client.audio.transcriptions.create(model="whisper-1", file=buf)
    try:
        await mem.log_token_usage("whisper-1", 0, 0, use_case="voice_transcription")
    except Exception:
        pass
    return result.text


_CONFIRM_WORDS = {"confirm", "yes", "yep", "yup", "ok", "okay", "go", "go ahead", "do it", "proceed"}
_CANCEL_WORDS = {"cancel", "no", "nope", "abort", "stop", "nevermind", "never mind", "don't"}

# ── Correction detection ──────────────────────────────────────────────────────
# Phrases that strongly indicate Ki made a specific, identifiable mistake.
# Deliberately narrow — conversational negations ("no,", "nah,", "i said") are
# excluded because they fire on situational redirects, not genuine corrections.
_CORRECTION_PHRASES = [
    # Direct assertions that Ki was wrong
    "you're wrong", "youre wrong", "you are wrong",
    "you were wrong", "you got that wrong", "you got it wrong",
    "you made a mistake", "you're mistaken", "youre mistaken",
    # Statements about correctness
    "that's wrong", "thats wrong", "that is wrong",
    "that's incorrect", "thats incorrect", "that is incorrect",
    "that's not right", "thats not right", "that is not right",
    "that's not true", "thats not true", "that is not true",
    "not true", "not correct", "incorrect",
    "that was wrong", "that wasn't right", "that wasnt right",
    "wrong,", "wrong -", "- wrong",
    # Misread / missed
    "you misread", "you missed",
    "that's the wrong", "thats the wrong",
    # Source / tool errors
    "you should have", "you should've",
    "don't do that", "stop doing",
    # Explicit correction language
    "i corrected you", "i just corrected",
    "that's not what", "thats not what",
    # Qualified "actually" — only when followed by something substantive
    "actually,", "actually ",
    # "not quite" is specific enough to indicate a real miss
    "not quite",
]

# Words that are strong enough correction signals on their own to bypass the
# minimum-word-count gate below (e.g. a single-word "Wrong." or "Incorrect.")
_STRONG_CORRECTION_WORDS = {
    "wrong", "incorrect", "mistaken", "inaccurate",
    "misinformation", "misinformed",
}

async def _maybe_capture_correction(user_message: str) -> None:
    """If the user's message looks like a correction, record it against the most recent assistant message.

    Filtering philosophy — we only proceed if ALL of the following are true:
      1. The message is short (≤300 chars) — long messages are usually requests, not corrections.
      2. The message contains a phrase that signals Ki made a specific, identifiable mistake.
      3. The message has ≥8 words OR contains a strong correction word (handles bare "Wrong." etc.)
      4. The Haiku classifier judges the correction to be about a PERSISTENT rule (not a
         situational redirect like "I don't want to do that right now").
    """
    import sqlalchemy as sa
    from .db import SessionLocal as _SessionLocal

    # Gate 1: length cap — long messages are requests, not corrections
    if len(user_message) > 300:
        return

    lower = user_message.lower()

    # Gate 2: phrase matching — must contain a strong correction signal
    if not any(phrase in lower for phrase in _CORRECTION_PHRASES):
        return

    # Gate 3: minimum substance check — short vague messages are almost always
    # situational redirects.  We waive the word-count floor only if a strong
    # correction word is present (e.g. "Wrong.", "Incorrect.").
    word_count = len(user_message.split())
    has_strong_word = any(w in lower.split() or f"{w}." in lower or f"{w}," in lower
                          for w in _STRONG_CORRECTION_WORDS)
    if word_count < 8 and not has_strong_word:
        print(f"[correction] skipped (too short / weak signal, {word_count} words): {user_message[:80]!r}")
        return

    try:
        # Use two separate sessions: one read-only to find the prior message,
        # one with an explicit transaction to write the correction.
        async with _SessionLocal() as session:
            result = await session.execute(sa.text("""
                SELECT id, content FROM conversations
                WHERE role = 'assistant' AND content IS NOT NULL AND content != ''
                ORDER BY created_at DESC LIMIT 1
            """))
            row = result.first()

        if not row:
            return
        prior_id = row.id
        prior_content = row.content

        async with _SessionLocal() as session:
            async with session.begin():
                # Save the correction
                await session.execute(sa.text("""
                    INSERT INTO corrections (conversation_id, prior_assistant_msg, correction_text)
                    VALUES (:conv_id, :prior_msg, :correction)
                """), {
                    "conv_id": prior_id,
                    "prior_msg": prior_content,
                    "correction": user_message,
                })
                # Mark the assistant message as corrected
                await session.execute(sa.text("""
                    UPDATE conversations SET signal = 'correction'
                    WHERE id = :id
                """), {"id": prior_id})

        print(f"[correction] captured: {user_message[:80]}")

        # Attempt to generate a tool-selection rule from this correction.
        # Fetch tool_calls from the bad turn so Haiku can diagnose the source error.
        try:
            async with _SessionLocal() as session:
                res = await session.execute(sa.text(
                    "SELECT tool_calls FROM conversations WHERE id = :id"
                ), {"id": prior_id})
                tc_row = res.first()
            tool_calls = tc_row.tool_calls if tc_row else None

            tool_desc = (
                ", ".join(f"{t['name']}({t['input']})" for t in tool_calls)
                if tool_calls else "none — Ki answered entirely from model knowledge"
            )
            rule_resp = await client.messages.create(
                model="claude-haiku-4-5-20251001",  # noqa
                max_tokens=200,
                messages=[{"role": "user", "content": (
                    "An AI assistant (Ki) gave a response and the user is correcting it.\n\n"
                    f"Ki's response: {prior_content[:500]}\n"
                    f"Tools Ki used: {tool_desc}\n"
                    f"User's message: {user_message}\n\n"
                    "STEP 1 — Situational vs. persistent?\n"
                    "Ask: Is the user expressing a one-time situational preference "
                    "(e.g. 'I don't want to do that right now', 'not now', 'maybe later', "
                    "'I changed my mind', 'skip that', 'not in the mood') — or did Ki make "
                    "a specific, repeatable, identifiable mistake that would recur unless a "
                    "rule is established?\n"
                    "If situational → respond with exactly: null\n\n"
                    "STEP 2 — Vague negation check?\n"
                    "If the user's message is a vague negation with no specific behavioural "
                    "error identified (e.g. 'no', 'nope', 'that's not it', 'not quite what "
                    "I meant') → respond with exactly: null\n\n"
                    "STEP 3 — Error type?\n"
                    "Only generate a rule if the problem was ONE of:\n"
                    "  a. Ki stated factually wrong/stale info she should have verified with a tool\n"
                    "  b. Ki used the wrong source or tool (e.g. vault when web_search was needed)\n"
                    "  c. Ki made a wrong assumption about what the user wanted (format, scope, approach)\n"
                    "     that is clearly a persistent preference, not a one-off request\n\n"
                    "If the issue is ONLY tone, length, or writing style → respond with exactly: null\n\n"
                    "If a rule IS warranted, respond with JSON only (no markdown):\n"
                    '{"rule": "one-sentence rule — e.g. When asked about X, always use web_search not model knowledge", '
                    '"reason": "brief: why this is persistent not situational"}'
                )}],
            )
            await mem.log_token_usage("claude-haiku-4-5-20251001", rule_resp.usage.input_tokens, rule_resp.usage.output_tokens, use_case="self_improvement")
            raw = rule_resp.content[0].text.strip()
            if raw.lower() != "null":
                raw = raw.lstrip("```json").lstrip("```").rstrip("```").strip()
                import json as _json
                data = _json.loads(raw)
                rule = (data.get("rule") or "").strip()
                reason = (data.get("reason") or "").strip()
                if rule:
                    await mem.save_memory(category="tool_rule", content=rule, source="correction")
                    print(f"[tool_rule] from correction: {rule}" + (f" | reason: {reason}" if reason else ""))
        except Exception as e:
            print(f"[tool_rule] classification failed (non-fatal): {e}")

    except Exception as e:
        print(f"[correction] capture failed (non-fatal): {e}")

_HISTORY_FULL_TURNS = 10   # keep this many most-recent turns at full length
_HISTORY_TRUNCATE_AT = 400  # older turns truncated to this many chars


def _compress_history(history: list[dict]) -> list[dict]:
    """Keep the most recent N turns verbatim; truncate older assistant turns to save tokens."""
    if len(history) <= _HISTORY_FULL_TURNS * 2:
        return history
    cutoff = len(history) - (_HISTORY_FULL_TURNS * 2)
    compressed = []
    for i, msg in enumerate(history):
        if i < cutoff and msg["role"] == "assistant" and isinstance(msg["content"], str):
            text = msg["content"]
            if len(text) > _HISTORY_TRUNCATE_AT:
                msg = {**msg, "content": text[:_HISTORY_TRUNCATE_AT] + " [...]"}
        compressed.append(msg)
    return compressed


class Agent:
    async def respond(self, user_message: str, image: dict | None = None, audio: dict | None = None, document: dict | None = None, channel: str = "whatsapp") -> str:
        if audio:
            try:
                transcript = await _transcribe_audio(audio["data"], audio.get("mime_type", "audio/ogg"))
                print(f"[transcribe] {transcript[:80]!r}")
                user_message = f"[Voice message]: {transcript}"
            except Exception as e:
                print(f"[transcribe] failed: {e}")
                user_message = user_message or "[Voice message — transcription failed, please try again]"

        # Ensure we always have non-empty content to send
        if not user_message:
            user_message = "(no text)"

        # ── /kill slash command — instant abort, no LLM invocation ──────────
        if user_message.strip().lower() == "/kill":
            await _kill_all()
            reply = "Killed."
            await mem.save_turn(user_message, reply, channel=channel)
            return reply

        # ── /deploy slash command — trigger deploy, no LLM invocation ───────
        if user_message.strip().lower() == "/deploy":
            reply = await _run_deploy()
            await mem.save_turn(user_message, reply, channel=channel)
            return reply

        # Check for a pending confirmation gate before normal processing
        pending = await get_pending_action()
        if pending:
            words = set(user_message.lower().split())
            if words & _CONFIRM_WORDS:
                print(f"[action] confirming: {pending['details']!r}")
                result = await execute_pending_action(pending)
                await resolve_pending_action(pending["id"], confirmed=True)
                user_message = (
                    f"Action confirmed and executed: {pending['details']}\n\nResult:\n{result}"
                )
            elif words & _CANCEL_WORDS:
                print(f"[action] cancelling: {pending['details']!r}")
                await resolve_pending_action(pending["id"], confirmed=False)
                user_message = f"Action cancelled: {pending['details']}"

        # ── slash command aliases ─────────────────────────────────────────────
        _msg_lc = user_message.strip().lower()
        for _short, _full in (("/d ", "/do "), ("/r ", "/remind "), ("/m ", "/morning ")):
            if _msg_lc.startswith(_short):
                user_message = _full + user_message.strip()[len(_short):]
                break
        else:
            if _msg_lc == "/t":
                user_message = "/today"

        # ── /do slash command — fast path, no LLM needed ────────────────────
        # Detect "/do <text>" (case-insensitive) and append a todo to today's
        # daily note, then return a short confirmation without invoking Claude.
        if user_message.lower().startswith("/do "):
            todo_text = user_message[4:].strip()
            if todo_text:
                tz = _USER_TZ
                today = datetime.now(tz).strftime("%Y-%m-%d")
                daily_note = f"daily/{today}.md"
                todo_item = f"- [ ] {todo_text}"
                try:
                    await append_to_note(daily_note, todo_item, commit_message=f"ki: add todo — {todo_text[:60]}")
                    reply = "Added to today's list."
                except Exception as e:
                    print(f"[/do] append_to_note failed: {e}")
                    reply = f"Couldn't add that — something went wrong: {e}"
                await mem.save_turn(user_message, reply, channel=channel)
                return reply

        if user_message.lower().startswith("/remind "):
            parts = user_message[8:].strip().split(None, 1)
            if len(parts) < 2:
                reply = "Usage: /remind YYYY-MM-DD[THH:MM] <message>"
                await mem.save_turn(user_message, reply, channel=channel)
                return reply
            dt_raw, remind_msg = parts[0], parts[1].strip()
            try:
                if "T" in dt_raw:
                    if len(dt_raw) == 16:
                        dt_raw = dt_raw + ":00"
                    datetime.fromisoformat(dt_raw)
                    remind_iso = dt_raw
                else:
                    datetime.fromisoformat(dt_raw)
                    remind_iso = f"{dt_raw}T09:00:00"
            except ValueError:
                reply = "Usage: /remind YYYY-MM-DD[THH:MM] <message>"
                await mem.save_turn(user_message, reply, channel=channel)
                return reply
            if "set_reminder" in _INTEGRATION_TOOL_MAP:
                try:
                    await _INTEGRATION_TOOL_MAP["set_reminder"].run_tool(
                        "set_reminder", {"message": remind_msg, "remind_at": remind_iso}
                    )
                except Exception as e:
                    print(f"[/remind] set_reminder failed: {e}")
            tz = _USER_TZ
            today = datetime.now(tz).strftime("%Y-%m-%d")
            daily_note = f"daily/{today}.md"
            note_item = f"- Reminder set: {remind_msg} (due {remind_iso[:10]})"
            try:
                await append_to_note(daily_note, note_item, commit_message=f"ki: add reminder — {remind_msg[:60]}")
            except Exception as e:
                print(f"[/remind] append_to_note failed: {e}")
            display_dt = remind_iso[:16].replace("T", " at ")
            reply = f'Reminder set for {display_dt} — "{remind_msg}" ✅'
            await mem.save_turn(user_message, reply, channel=channel)
            return reply

        if user_message.lower().strip() == "/today":
            tz = _USER_TZ
            today = datetime.now(tz).strftime("%Y-%m-%d")
            daily_note = f"daily/{today}.md"
            try:
                reply = await read_note(daily_note)
            except Exception as e:
                print(f"[/today] read_note failed: {e}")
                reply = f"Couldn't fetch today's note: {e}"
            await mem.save_turn(user_message, reply, channel=channel)
            return reply

        if user_message.lower().startswith("/morning "):
            pages_text = user_message[9:].strip()
            if pages_text:
                tz = _USER_TZ
                today = datetime.now(tz).strftime("%Y-%m-%d")
                daily_note = f"daily/{today}.md"
                try:
                    current = await read_note(daily_note)
                    placeholder = "<injected morning pages go here>"
                    if placeholder in current:
                        new_content = current.replace(placeholder, pages_text, 1)
                    else:
                        morning_heading = "## ☀️"
                        idx = current.find(morning_heading)
                        if idx != -1:
                            insert_at = current.find("\n", idx) + 1
                            new_content = current[:insert_at] + pages_text + "\n" + current[insert_at:]
                        else:
                            new_content = current + "\n" + pages_text + "\n"
                    await write_note(daily_note, new_content, overwrite=True)
                    reply = "Morning pages added."
                except Exception as e:
                    print(f"[/morning] failed: {e}")
                    reply = f"Couldn't add morning pages — something went wrong: {e}"
                await mem.save_turn(user_message, reply, channel=channel)
                return reply

        # Strip reply-quote prefix before passing to Claude — Ki already has conversation
        # history; the quoted text causes it to re-engage with items already handled.
        # Raw message is preserved below for DB storage.
        raw_user_message = user_message
        user_message = _annotate_reply_quote(user_message)

        history = await mem.get_recent_conversation(limit=40, session_gap_hours=SESSION_GAP_HOURS)
        history = _compress_history(history)
        memories = await mem.get_memories()
        heartbeat_state = await mem.get_latest_heartbeat_state()

        # If the session gap fired but conversation was recent (within 4h), let Ki know
        # so it can acknowledge the context drop and offer to recap.
        session_note = ""
        if not history:
            mins = await minutes_since_last_message()
            if mins is not None and mins < 240:
                session_note = (
                    f"Note: your conversation history with this user was cleared after a "
                    f"{int(mins)}-minute gap — you have NO memory of what was said before this message. "
                    "If the user references something you said or something they told you previously, "
                    "you must acknowledge that you don't have that context and ask them to fill you in. "
                    "NEVER invent or guess at what was previously discussed. "
                    "Do NOT resume or restart any prior task unprompted. "
                    "Respond only to what the user actually said in this message."
                )

        # Fetch vault excerpts relevant to this message (auto-injected into system prompt).
        # Skip for short messages — searching "ping" / "ok" / "yes" pulls in unrelated
        # context and causes Claude to continue previous tasks unprompted.
        vault_excerpts: list[dict] = []
        vault_search_ok = True
        if len(user_message.split()) >= 4:
            try:
                raw = await search_vault(user_message, limit=5)
                vault_excerpts = [r for r in raw if r["score"] >= MIN_VAULT_SCORE]
                hits = [f"{r['score']:.2f}:{r['filename']}" for r in raw]
                print(f"[auto-search] {hits} → {len(vault_excerpts)} above threshold")
            except Exception as e:
                print(f"Warning: vault search failed: {e}")
                vault_search_ok = False

        cross_channel_context: list[dict] = []
        if not history:
            cross_channel_context = await mem.get_recent_other_channel(channel, hours=2.0, limit=10)

        system = await _build_system(
            memories, vault_excerpts, heartbeat_state, session_note, vault_search_ok,
            session_messages=history, cross_channel_context=cross_channel_context,
        )

        if audio:
            system.append({"type": "text", "text": (
                "The user sent a voice message. Their transcription is shown as context. "
                "Respond naturally and directly — do NOT repeat, quote, or reference the transcription in any way. "
                "Never start your response with what the user said. Just answer."
            )})

        if image:
            user_content = [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": image["media_type"],
                        "data": image["data"],
                    },
                },
                {"type": "text", "text": user_message or "What's in this image?"},
            ]
        elif document:
            media_type = document["media_type"]
            filename = document.get('filename', 'document')
            if media_type == "application/pdf":
                user_content = [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": document["data"],
                        },
                    },
                    {"type": "text", "text": user_message or f"Please read this document ({filename}) and summarise it."},
                ]
            elif media_type == "text/plain":
                user_content = [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "text/plain",
                            "data": document["data"],
                        },
                    },
                    {"type": "text", "text": user_message or f"Please read this file ({filename}) and summarise it."},
                ]
            elif media_type in ("text/rtf", "application/rtf"):
                try:
                    raw = base64.b64decode(document["data"]).decode("utf-8", errors="replace")
                    # Strip RTF control codes — extract plain text between braces
                    import re as _re
                    file_text = _re.sub(r'\\[a-z]+\d*\s?', ' ', raw)  # remove control words
                    file_text = _re.sub(r'[{}]', '', file_text)         # remove braces
                    file_text = _re.sub(r'\\\*.*?;', '', file_text)     # remove destinations
                    file_text = _re.sub(r'[ \t]+', ' ', file_text).strip()
                except Exception:
                    file_text = "(unreadable)"
                user_content = (
                    f"{user_message}\n\n[File: {filename}]\n{file_text}"
                    if user_message
                    else f"Please read this RTF file ({filename}) and summarise it.\n\n[File: {filename}]\n{file_text}"
                )
            else:
                # Unsupported document type — tell the user
                user_content = user_message or f"The user sent a document ({filename}, type {media_type}) but this format isn't supported for reading. Let the user know you can only read PDFs and text files."
        else:
            user_content = user_message

        messages = _prepend_timestamp(history + [{"role": "user", "content": user_content}])

        # Persist a meaningful record — if a file was sent with no caption, record that.
        # Use raw_user_message (pre-annotation) so the DB retains the original quoted reply for display.
        if document and not raw_user_message:
            saved_text = f"[sent file: {document.get('filename', 'document')}]"
        else:
            saved_text = raw_user_message or ""
        await save_user_message(saved_text, channel=channel)

        # ── Correction detection ──────────────────────────────────────────────
        # Lightweight — one DB read + one optional insert, non-blocking path
        await _maybe_capture_correction(saved_text)

        response = await _create_with_retry(
            model=MODEL,
            max_tokens=8192,
            system=system,
            tools=TOOLS,
            messages=_mark_last_cacheable(messages),
        )
        await mem.log_token_usage(
            MODEL,
            response.usage.input_tokens,
            response.usage.output_tokens,
            getattr(response.usage, "cache_creation_input_tokens", 0) or 0,
            getattr(response.usage, "cache_read_input_tokens", 0) or 0,
            use_case="chat",
        )

        _tool_calls_log: list[dict] = []
        _tool_iterations = 0
        _MAX_TOOL_ITERATIONS = 15
        while response.stop_reason == "tool_use":
            _tool_iterations += 1
            if _tool_iterations > _MAX_TOOL_ITERATIONS:
                print(f"[agent] tool iteration cap reached ({_MAX_TOOL_ITERATIONS}) — aborting loop")
                reply = "I hit my tool iteration limit on that one — something may have gone wrong. Let me know if you want me to try again."
                await mem.save_turn(saved_text, reply, channel=channel, tool_calls=_tool_calls_log or None)
                return reply

            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    label = (block.input.get("query") or block.input.get("url") or block.input.get("task")
                             or block.input.get("path") or block.input.get("note_path") or block.input.get("title") or "")
                    _tool_calls_log.append({"name": block.name, "input": str(label)[:200]})
                    result = await _run_tool(block.name, block.input, channel)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })

            if not tool_results:
                print("[agent] stop_reason=tool_use but no tool_use blocks found — breaking loop")
                break

            if _kill_event.is_set():
                break

            messages = messages + [
                {"role": "assistant", "content": response.content},
                {"role": "user", "content": tool_results},
            ]
            response = await _create_with_retry(
                model=MODEL,
                max_tokens=8192,
                system=system,
                tools=TOOLS,
                messages=_mark_last_cacheable(messages),
            )
            await mem.log_token_usage(
                MODEL,
                response.usage.input_tokens,
                response.usage.output_tokens,
                getattr(response.usage, "cache_creation_input_tokens", 0) or 0,
                getattr(response.usage, "cache_read_input_tokens", 0) or 0,
                use_case="chat",
            )

        reply = next((b.text for b in response.content if hasattr(b, "text")), None)
        if reply is None:
            print(f"[agent] no text block in response (stop_reason={response.stop_reason!r}): {[type(b).__name__ for b in response.content]} — retrying once")
            await asyncio.sleep(2)
            response = await _create_with_retry(
                model=MODEL,
                max_tokens=8192,
                system=system,
                tools=TOOLS,
                messages=_mark_last_cacheable(messages),
            )
            reply = next((b.text for b in response.content if hasattr(b, "text")), None)
            if reply is None:
                print(f"[agent] retry also returned no text block (stop_reason={response.stop_reason!r}) — giving up")
                return "Sorry, something went wrong on my end — I didn't manage to finish that. Can you try again?"
        await mem.save_turn(saved_text, reply, channel=channel, tool_calls=_tool_calls_log or None)
        print(f"[tts] respond() end — audio={bool(audio)}, channel={channel!r}, reply_len={len(reply) if reply else 0}")
        if audio and channel == "whatsapp":
            print(f"[tts] scheduling voice note task")
            async def _tts_task():
                print(f"[tts] _tts_task started")
                try:
                    await _send_voice_note(_strip_emojis(reply))
                except Exception as e:
                    import traceback
                    print(f"[tts] unhandled exception in voice note task: {e}")
                    traceback.print_exc()
            task = asyncio.create_task(_tts_task())
            print(f"[tts] task created: {task!r}")
            return None
        return reply

    async def respond_stream(self, user_message: str, channel: str = "desktop", image: dict | None = None, document: dict | None = None, audio: dict | None = None, voice_mode: bool = False):
        """
        Async generator yielding SSE-compatible event dicts for the desktop client.
        Events: {type: 'text', text: str}
                {type: 'tool_start', name: str}
                {type: 'tool_done', name: str}
                {type: 'done'}
                {type: 'error', message: str}
        """
        try:
            _placeholder_id: int | None = None
            full_reply = ""
            _voice_transcript: str = ""

            # Transcribe audio before any other processing
            if audio:
                try:
                    transcript = await _transcribe_audio(audio["data"], audio.get("mime_type", "audio/webm"))
                    print(f"[stream-transcribe] {transcript[:80]!r}")
                    _voice_transcript = transcript
                    user_message = f"[Voice message]: {transcript}"
                except Exception as e:
                    print(f"[stream] audio transcription failed: {e}")
                    user_message = user_message or "(voice message — transcription failed)"

            # Ensure non-empty content
            if not user_message:
                user_message = "(no text)"

            # ── /kill slash command — instant abort, no LLM invocation ──────
            if user_message.strip().lower() == "/kill":
                await _kill_all()
                reply = "Killed."
                await mem.save_turn(user_message, reply, channel=channel)
                yield {"type": "text", "text": reply}
                yield {"type": "done"}
                return

            # ── /deploy slash command — trigger deploy, no LLM invocation ───
            if user_message.strip().lower() == "/deploy":
                yield {"type": "text", "text": "Starting deploy..."}
                reply = await _run_deploy()
                await mem.save_turn(user_message, reply, channel=channel)
                yield {"type": "text", "text": "\n\n" + reply}
                yield {"type": "done"}
                return

            # Pending confirmation gate
            pending = await get_pending_action()
            if pending:
                words = set(user_message.lower().split())
                if words & _CONFIRM_WORDS:
                    print(f"[stream] confirming: {pending['details']!r}")
                    result = await execute_pending_action(pending)
                    await resolve_pending_action(pending["id"], confirmed=True)
                    user_message = f"Action confirmed and executed: {pending['details']}\n\nResult:\n{result}"
                elif words & _CANCEL_WORDS:
                    print(f"[stream] cancelling: {pending['details']!r}")
                    await resolve_pending_action(pending["id"], confirmed=False)
                    user_message = f"Action cancelled: {pending['details']}"

            _saved_text = (f"[sent file: {document.get('filename', 'document')}]"
                           if (document and user_message == "(no text)")
                           else user_message)

            # ── slash command aliases ─────────────────────────────────────────
            _msg_lc = user_message.strip().lower()
            for _short, _full in (("/d ", "/do "), ("/r ", "/remind "), ("/m ", "/morning ")):
                if _msg_lc.startswith(_short):
                    user_message = _full + user_message.strip()[len(_short):]
                    break
            else:
                if _msg_lc == "/t":
                    user_message = "/today"

            # ── /do slash command — fast path, no LLM needed ────────────────
            if user_message.lower().startswith("/do "):
                todo_text = user_message[4:].strip()
                if todo_text:
                    tz = _USER_TZ
                    today = datetime.now(tz).strftime("%Y-%m-%d")
                    daily_note = f"daily/{today}.md"
                    todo_item = f"- [ ] {todo_text}"
                    try:
                        await append_to_note(daily_note, todo_item, commit_message=f"ki: add todo — {todo_text[:60]}")
                        reply = "Added to today's list."
                    except Exception as e:
                        print(f"[/do] append_to_note failed: {e}")
                        reply = f"Couldn't add that — something went wrong: {e}"
                    await mem.save_turn(_saved_text, reply, channel=channel)
                    yield {"type": "text", "text": reply}
                    yield {"type": "done"}
                    return

            if user_message.lower().startswith("/remind "):
                parts = user_message[8:].strip().split(None, 1)
                if len(parts) < 2:
                    reply = "Usage: /remind YYYY-MM-DD[THH:MM] <message>"
                    await mem.save_turn(_saved_text, reply, channel=channel)
                    yield {"type": "text", "text": reply}
                    yield {"type": "done"}
                    return
                dt_raw, remind_msg = parts[0], parts[1].strip()
                try:
                    if "T" in dt_raw:
                        if len(dt_raw) == 16:
                            dt_raw = dt_raw + ":00"
                        datetime.fromisoformat(dt_raw)
                        remind_iso = dt_raw
                    else:
                        datetime.fromisoformat(dt_raw)
                        remind_iso = f"{dt_raw}T09:00:00"
                except ValueError:
                    reply = "Usage: /remind YYYY-MM-DD[THH:MM] <message>"
                    await mem.save_turn(_saved_text, reply, channel=channel)
                    yield {"type": "text", "text": reply}
                    yield {"type": "done"}
                    return
                if "set_reminder" in _INTEGRATION_TOOL_MAP:
                    try:
                        await _INTEGRATION_TOOL_MAP["set_reminder"].run_tool(
                            "set_reminder", {"message": remind_msg, "remind_at": remind_iso}
                        )
                    except Exception as e:
                        print(f"[/remind] set_reminder failed: {e}")
                tz = _USER_TZ
                today = datetime.now(tz).strftime("%Y-%m-%d")
                daily_note = f"daily/{today}.md"
                note_item = f"- Reminder set: {remind_msg} (due {remind_iso[:10]})"
                try:
                    await append_to_note(daily_note, note_item, commit_message=f"ki: add reminder — {remind_msg[:60]}")
                except Exception as e:
                    print(f"[/remind] append_to_note failed: {e}")
                display_dt = remind_iso[:16].replace("T", " at ")
                reply = f'Reminder set for {display_dt} — "{remind_msg}" ✅'
                await mem.save_turn(_saved_text, reply, channel=channel)
                yield {"type": "text", "text": reply}
                yield {"type": "done"}
                return

            if user_message.lower().strip() == "/today":
                tz = _USER_TZ
                today = datetime.now(tz).strftime("%Y-%m-%d")
                daily_note = f"daily/{today}.md"
                print(f"[/today] fetching {daily_note}")
                try:
                    content = await read_note(daily_note)
                    is_note = not content.startswith("Note not found")
                except Exception as e:
                    print(f"[/today] read_note failed: {e}")
                    content = f"Couldn't fetch today's note: {e}"
                    is_note = False
                print(f"[/today] {'note found' if is_note else 'note missing'}, {len(content)} chars")
                await mem.save_turn(_saved_text, content, channel=channel)
                if is_note:
                    yield {"type": "markdown", "text": content}
                else:
                    yield {"type": "text", "text": content}
                yield {"type": "done"}
                return

            if user_message.lower().startswith("/morning "):
                pages_text = user_message[9:].strip()
                if pages_text:
                    tz = _USER_TZ
                    today = datetime.now(tz).strftime("%Y-%m-%d")
                    daily_note = f"daily/{today}.md"
                    try:
                        current = await read_note(daily_note)
                        placeholder = "<injected morning pages go here>"
                        if placeholder in current:
                            new_content = current.replace(placeholder, pages_text, 1)
                        else:
                            morning_heading = "## ☀️"
                            idx = current.find(morning_heading)
                            if idx != -1:
                                insert_at = current.find("\n", idx) + 1
                                new_content = current[:insert_at] + pages_text + "\n" + current[insert_at:]
                            else:
                                new_content = current + "\n" + pages_text + "\n"
                        await write_note(daily_note, new_content, overwrite=True)
                        reply = "Morning pages added."
                    except Exception as e:
                        print(f"[/morning] failed: {e}")
                        reply = f"Couldn't add morning pages — something went wrong: {e}"
                    await mem.save_turn(_saved_text, reply, channel=channel)
                    yield {"type": "text", "text": reply}
                    yield {"type": "done"}
                    return

            # Strip reply-quote prefix before passing to Claude — Ki already has conversation
            # history; the quoted text causes it to re-engage with items already handled.
            # _saved_text captured above retains the original for DB storage.
            user_message = _annotate_reply_quote(user_message)

            history = await mem.get_recent_conversation(limit=40, session_gap_hours=SESSION_GAP_HOURS)
            await save_user_message(_saved_text, channel=channel)
            try:
                _placeholder_id = await mem.insert_assistant_placeholder(channel)
            except Exception as _e:
                print(f"[stream] placeholder insert failed: {_e}")
            _chunks_since_save = 0

            # ── Correction detection ──────────────────────────────────────────
            await _maybe_capture_correction(_saved_text)

            history = _compress_history(history)
            memories = await mem.get_memories()
            heartbeat_state = await mem.get_latest_heartbeat_state()

            session_note = ""
            if not history:
                mins = await minutes_since_last_message()
                if mins is not None and mins < 240:
                    session_note = (
                        f"Note: your conversation history with this user was cleared after a "
                        f"{int(mins)}-minute gap — you have NO memory of what was said before this message. "
                        "If the user references something previously discussed, acknowledge you don't have that context."
                    )

            vault_excerpts: list[dict] = []
            vault_search_ok = True
            if len(user_message.split()) >= 4:
                try:
                    raw = await search_vault(user_message, limit=5)
                    vault_excerpts = [r for r in raw if r["score"] >= MIN_VAULT_SCORE]
                    if vault_excerpts:
                        yield {"type": "vault_inject", "files": [v["filename"] for v in vault_excerpts]}
                    hits = [f"{r['score']:.2f}:{r['filename']}" for r in raw]
                    print(f"[auto-search] {hits} → {len(vault_excerpts)} above threshold")
                except Exception as e:
                    print(f"Warning: vault search failed: {e}")
                    vault_search_ok = False

            cross_channel_context: list[dict] = []
            if not history:
                cross_channel_context = await mem.get_recent_other_channel(channel, hours=2.0, limit=10)

            system = await _build_system(
                memories, vault_excerpts, heartbeat_state, session_note, vault_search_ok,
                session_messages=history, cross_channel_context=cross_channel_context,
            )

            if audio:
                system.append({"type": "text", "text": (
                    "The user sent a voice message. Respond naturally and directly to what they said. "
                    "Do not repeat or reference the transcription text. "
                    "Never start with 'You said', 'I heard you say', or any echo of the input."
                )})

            if image:
                user_content = [
                    {"type": "image", "source": {"type": "base64", "media_type": image["media_type"], "data": image["data"]}},
                    {"type": "text", "text": user_message or "What's in this image?"},
                ]
            elif document:
                media_type = document["media_type"]
                filename = document.get("filename", "document")
                if media_type == "application/pdf":
                    user_content = [
                        {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": document["data"]}},
                        {"type": "text", "text": user_message or f"Please read this document ({filename}) and summarise it."},
                    ]
                elif media_type == "text/plain":
                    user_content = [
                        {"type": "document", "source": {"type": "base64", "media_type": "text/plain", "data": document["data"]}},
                        {"type": "text", "text": user_message or f"Please read this file ({filename}) and summarise it."},
                    ]
                else:
                    user_content = user_message or f"The user sent a document ({filename}, type {media_type}) but this format isn't supported. Let them know."
            else:
                user_content = user_message

            messages = _prepend_timestamp(history + [{"role": "user", "content": user_content}])
            full_reply = ""
            _ki_image: dict | None = None
            _tool_calls_log: list[dict] = []
            _tool_iterations = 0
            _MAX_TOOL_ITERATIONS = 15

            while True:
                # Iterate raw stream events — avoids reliance on get_final_message()
                # which is sync in some SDK versions and async in others.
                # Retry on overload/rate-limit up to 3 times, but only when no text
                # has been emitted yet (safe to restart without duplicate output).
                content_blocks: list[dict] = []
                stop_reason = None
                in_tokens = out_tokens = 0
                cache_creation = cache_read = 0
                cur_block_idx: int | None = None
                cur_input_str = ""

                # Retry the stream on overload/rate-limit, but only when no text has been
                # emitted yet — safe to restart without duplicating output to the client.
                _stream_attempts = 0
                while True:
                    try:
                        async with client.messages.stream(
                            model=MODEL,
                            max_tokens=8192,
                            system=system,
                            tools=TOOLS,
                            messages=_mark_last_cacheable(messages),
                        ) as stream:
                            async for event in stream:
                                etype = event.type
                                if etype == "message_start":
                                    in_tokens = event.message.usage.input_tokens
                                    cache_creation = getattr(event.message.usage, "cache_creation_input_tokens", 0) or 0
                                    cache_read = getattr(event.message.usage, "cache_read_input_tokens", 0) or 0
                                elif etype == "content_block_start":
                                    cb = event.content_block
                                    if cb.type == "text":
                                        content_blocks.append({"type": "text", "text": ""})
                                    elif cb.type == "tool_use":
                                        content_blocks.append({"type": "tool_use", "id": cb.id, "name": cb.name, "input": {}})
                                        cur_input_str = ""
                                    cur_block_idx = len(content_blocks) - 1
                                elif etype == "content_block_delta":
                                    d = event.delta
                                    if d.type == "text_delta":
                                        full_reply += d.text
                                        if cur_block_idx is not None and content_blocks[cur_block_idx]["type"] == "text":
                                            content_blocks[cur_block_idx]["text"] += d.text
                                        if not voice_mode:
                                            yield {"type": "text", "text": d.text}
                                        if _placeholder_id:
                                            _chunks_since_save += 1
                                            if _chunks_since_save >= 100:
                                                try:
                                                    await mem.update_assistant_message(_placeholder_id, full_reply)
                                                except Exception:
                                                    pass
                                                _chunks_since_save = 0
                                    elif d.type == "input_json_delta":
                                        cur_input_str += d.partial_json
                                elif etype == "content_block_stop":
                                    if cur_block_idx is not None and content_blocks[cur_block_idx]["type"] == "tool_use":
                                        try:
                                            content_blocks[cur_block_idx]["input"] = json.loads(cur_input_str)
                                        except Exception:
                                            content_blocks[cur_block_idx]["input"] = {}
                                        cur_input_str = ""
                                elif etype == "message_delta":
                                    stop_reason = event.delta.stop_reason
                                    out_tokens = event.usage.output_tokens
                        break  # stream completed successfully
                    except (anthropic.RateLimitError, anthropic.InternalServerError) as e:
                        _stream_attempts += 1
                        if _stream_attempts >= 3 or full_reply:
                            # Don't retry if we've already sent text — can't safely restart
                            raise
                        # Reset state for clean retry
                        content_blocks = []
                        stop_reason = None
                        in_tokens = out_tokens = 0
                        cache_creation = cache_read = 0
                        cur_block_idx = None
                        cur_input_str = ""
                        wait = 10 * _stream_attempts
                        print(f"[stream] overload, retrying in {wait}s (attempt {_stream_attempts}/3): {e}")
                        await asyncio.sleep(wait)

                await mem.log_token_usage(MODEL, in_tokens, out_tokens, cache_creation, cache_read, use_case="chat")

                if stop_reason != "tool_use":
                    break

                _tool_iterations += 1
                if _tool_iterations > _MAX_TOOL_ITERATIONS:
                    print(f"[stream] tool iteration cap reached ({_MAX_TOOL_ITERATIONS}) — aborting loop")
                    cap_msg = "I hit my tool iteration limit on that one — something may have gone wrong. Let me know if you want me to try again."
                    if _placeholder_id:
                        await mem.update_assistant_message(_placeholder_id, cap_msg, status="done")
                        await mem.embed_turn(_placeholder_id, _saved_text, cap_msg)
                    else:
                        await mem.save_turn(_saved_text, cap_msg, channel=channel)
                    yield {"type": "text", "text": cap_msg}
                    yield {"type": "done"}
                    return

                tool_results = []
                for block in content_blocks:
                    if block["type"] == "tool_use":
                        inp = block["input"]
                        # Pick the most informative single param to surface in the UI
                        label = (inp.get("query") or inp.get("path") or inp.get("url")
                                 or inp.get("task") or inp.get("note_path") or inp.get("title") or "")
                        _tool_calls_log.append({"name": block["name"], "input": str(label)[:200]})
                        yield {"type": "tool_start", "name": block["name"], "label": str(label)[:120]}
                        if block["name"] == "send_file" and channel == "desktop":
                            fn = block["input"].get("filename", "file.txt")
                            content_str = block["input"].get("content", "")
                            data_b64 = base64.b64encode(content_str.encode("utf-8")).decode("ascii")
                            yield {"type": "file", "filename": fn, "data": data_b64, "media_type": "text/plain"}
                            result = f"File sent to desktop: {fn}"
                        elif block["name"] == "generate_image" and channel == "desktop":
                            mod = _INTEGRATION_TOOL_MAP.get("generate_image")
                            if mod is None:
                                result = "Image generation integration not loaded."
                            else:
                                try:
                                    img_prompt = block["input"]["prompt"]
                                    image_bytes, model_used = await mod._generate_image_bytes(img_prompt)
                                    media_type = _detect_image_media_type(image_bytes)
                                    data_b64 = base64.b64encode(image_bytes).decode("ascii")
                                    _ki_image = {"data": data_b64, "media_type": media_type}
                                    yield {"type": "image", "data": data_b64, "media_type": media_type, "prompt": img_prompt}
                                    await mem.log_token_usage(model_used, 0, 0, use_case="image_gen")
                                    result = "Image generated and displayed."
                                except Exception as e:
                                    result = f"Image generation failed: {e}"
                        else:
                            result = await _run_tool(block["name"], block["input"], channel)
                        yield {"type": "tool_done", "name": block["name"], "label": str(label)[:120]}
                        tool_results.append({"type": "tool_result", "tool_use_id": block["id"], "content": result})

                if _placeholder_id and full_reply:
                    try:
                        await mem.update_assistant_message(_placeholder_id, full_reply)
                        _chunks_since_save = 0
                    except Exception:
                        pass

                if not tool_results:
                    break

                if _kill_event.is_set():
                    break

                messages = messages + [
                    {"role": "assistant", "content": content_blocks},
                    {"role": "user", "content": tool_results},
                ]

            if _placeholder_id:
                await mem.update_assistant_message(_placeholder_id, full_reply or "", status="done", ki_image=_ki_image, tool_calls=_tool_calls_log or None)
                await mem.embed_turn(_placeholder_id, _saved_text, full_reply or "")
            else:
                await mem.save_turn(_saved_text, full_reply or "", channel=channel, ki_image=_ki_image, tool_calls=_tool_calls_log or None)
            # Mirror Ki's reply to WhatsApp when responding in the mobile channel from desktop
            if channel == "whatsapp" and full_reply:
                await _forward_to_whatsapp(full_reply)
            # Generate TTS audio for voice mode responses
            if voice_mode and full_reply:
                try:
                    tts_reply = full_reply
                    if audio and _voice_transcript:
                        import re as _re
                        lines = tts_reply.split('\n')
                        cleaned = []
                        for line in lines:
                            if _voice_transcript[:40].lower() in line.lower():
                                continue
                            if _re.match(r'^\s*(transcription|you said|i heard)[:\s]', line, _re.IGNORECASE):
                                continue
                            cleaned.append(line)
                        tts_reply = '\n'.join(cleaned).strip()
                        tts_reply = _re.sub(r'^\[Voice message\]:\s*.+?\n+', '', tts_reply, flags=_re.IGNORECASE).strip()
                        if not tts_reply:
                            tts_reply = full_reply
                    tts_reply = _strip_emojis(tts_reply)
                    audio_bytes = await generate_speech(tts_reply)
                    if audio_bytes:
                        audio_b64 = base64.b64encode(audio_bytes).decode("ascii")
                        if _placeholder_id:
                            await mem.save_voice_audio(_placeholder_id, audio_b64)
                        yield {"type": "audio", "data": audio_b64, "mime_type": "audio/mpeg"}
                except Exception as _tts_err:
                    print(f"[stream] TTS failed: {_tts_err}")
            yield {"type": "done"}

        except Exception as e:
            print(f"[stream] error: {e}")
            if _placeholder_id and full_reply:
                try:
                    await mem.update_assistant_message(_placeholder_id, full_reply, status="partial")
                    await mem.embed_turn(_placeholder_id, _saved_text, full_reply)
                except Exception:
                    pass
            yield {"type": "error", "message": str(e)}

