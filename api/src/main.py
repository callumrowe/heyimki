import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)

import anthropic
import sqlalchemy as sa
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import Depends, FastAPI, HTTPException, Request, Security
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from .agent import Agent, _INTEGRATIONS, _INTEGRATION_TOOL_MAP, register_kill_callback
from . import memory as _mem_module
from .alerts import send_alert
from .db import SessionLocal, run_migrations
from .heartbeat import run_heartbeat
from .config import USER_LABEL
from .indexer import reindex
from . import memory as mem
from .push import send_push_to_all, VAPID_PUBLIC_KEY
from .vector_store import client as qdrant_client, ensure_collections

HEARTBEAT_HOURS = int(os.environ.get("HEARTBEAT_HOURS", "3"))
DESKTOP_SECRET = os.environ.get("DESKTOP_SECRET", "")
VAULT_SYNC_MINUTES = float(os.environ.get("VAULT_SYNC_MINUTES", "5"))
INTERNAL_SECRET = os.environ.get("INTERNAL_SECRET", "")
USER_TZ = ZoneInfo((os.environ.get("USER_TZ") or "UTC"))
PROMPTS_DIR = Path(__file__).parent.parent / "prompts"
WEB_DIR = Path(__file__).parent.parent / "web"
_ALLOWED_PROMPTS = {"identity", "instructions"}

# Background task registry: channel → Queue of SSE event dicts.
# Tasks run to completion regardless of SSE connection state.
_active_tasks: dict[str, asyncio.Queue] = {}

# Tracks the currently-running tool per channel so reconnecting clients can
# show it immediately without waiting for the next SSE event.
_active_tool: dict[str, dict] = {}

# Cancellable task handles: channel → asyncio.Task, for /kill support.
_running_tasks: dict[str, asyncio.Task] = {}

# Exposed so route handlers can read next_run_time from registered jobs.
_scheduler: AsyncIOScheduler | None = None

# ── Desktop auth ──────────────────────────────────────────────────────────────
_bearer = HTTPBearer(auto_error=False)

async def _require_desktop_auth(credentials: HTTPAuthorizationCredentials | None = Security(_bearer)):
    if not DESKTOP_SECRET:
        raise HTTPException(status_code=500, detail="DESKTOP_SECRET not configured on server")
    if credentials is None or credentials.credentials != DESKTOP_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")


async def _require_internal_auth(credentials: HTTPAuthorizationCredentials | None = Security(_bearer)):
    """Service-to-service auth (Baileys, the ki CLI). Accepts INTERNAL_SECRET as a bearer token.

    The web app's DESKTOP_SECRET is also accepted, so admin actions that the CLI
    and the web app share (heartbeat trigger, daily note) work from either.
    """
    token = credentials.credentials if credentials else None
    if not token:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if (INTERNAL_SECRET and token == INTERNAL_SECRET) or (DESKTOP_SECRET and token == DESKTOP_SECRET):
        return
    raise HTTPException(status_code=401, detail="Unauthorized")

_anthropic = anthropic.AsyncAnthropic(max_retries=3)
HAIKU = "claude-haiku-4-5-20251001"


async def _distill_topic(message: str) -> str:
    """Use Haiku to extract a concise topic label from a Ki response."""
    resp = await _anthropic.messages.create(
        model=HAIKU,
        max_tokens=60,
        messages=[{
            "role": "user",
            "content": (
                "In 10 words or fewer, what topic or task is this message about?\n"
                "Reply with only the topic, no punctuation.\n\n"
                f"{message[:800]}"
            ),
        }],
    )
    await mem.log_token_usage(HAIKU, resp.usage.input_tokens, resp.usage.output_tokens, use_case="memory_classify")
    return resp.content[0].text.strip()


async def _classify_memory(reaction: str, message: str) -> tuple[str, str]:
    """Use Haiku to pick the right memory category and write clean content from a reaction.

    Returns (category, content) where category is one of preference/fact/person.
    """
    emoji_context = {
        "👍": "The user marked this topic as done and wants no further follow-up from the assistant.",
        "❤️": "The user appreciated this response — it resonated with them personally.",
        "✕":  "The user dismissed this response — they didn't want or need it.",
    }.get(reaction, f"The user reacted with {reaction}.")

    resp = await _anthropic.messages.create(
        model=HAIKU,
        max_tokens=80,
        messages=[{
            "role": "user",
            "content": (
                "A user reacted to an AI assistant's message. "
                "Choose the right memory category and write a clean one-sentence memory about the user.\n\n"
                "Categories:\n"
                "- preference: The user likes, dislikes, or has a style/approach preference\n"
                "- person: the message is primarily about a named person in the user's life\n"
                "- fact: anything else — what the user did, closed, decided, or their context\n\n"
                f"Reaction meaning: {emoji_context}\n"
                f"Message: {message[:600]}\n\n"
                'Reply as JSON only, no markdown: {"category": "...", "content": "..."}'
            ),
        }],
    )
    await mem.log_token_usage(HAIKU, resp.usage.input_tokens, resp.usage.output_tokens, use_case="memory_classify")
    raw = resp.content[0].text.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
    try:
        data = json.loads(raw)
        cat = data.get("category", "fact")
        if cat not in ("preference", "fact", "person"):
            cat = "fact"
        return cat, data["content"]
    except Exception:
        # Fallback: save as fact with a simple topic distillation
        topic = await _distill_topic(message)
        return "fact", f"User reacted {reaction} to Ki's message about: {topic}"


async def _classify_tool_error(bad_message: str, tool_calls: list[dict] | None, correction: str | None) -> str | None:
    """Use Haiku to diagnose a 👎 signal and generate a tool-selection rule if applicable.

    Returns a rule string if this was a factual/source error, or None if it was something else
    (tone, formatting, etc.) that doesn't warrant a tool-selection rule.
    """
    tool_desc = (
        ", ".join(f"{t['name']}({t['input']})" for t in tool_calls)
        if tool_calls else "none — Ki answered entirely from model knowledge"
    )
    correction_line = f"\nUser correction: {correction}" if correction else ""

    prompt = (
        "An AI assistant gave a bad response. Determine if this was a factual or source error.\n\n"
        f"Bad response: {bad_message[:500]}\n"
        f"Tools Ki used during this response: {tool_desc}{correction_line}\n\n"
        "Only generate a rule if the problem was:\n"
        "1. Ki stated wrong or stale information she should have verified with a tool "
        "(web_search, search_vault, etc.)\n"
        "2. Ki used the wrong source — e.g. searched vault when she should have searched the web\n\n"
        "If this was a tone, length, formatting, or other non-factual issue: respond with exactly: null\n\n"
        "Otherwise respond as JSON only, no markdown:\n"
        '{"rule": "concise one-sentence rule — e.g. When asked about X, use web_search rather than answering from model knowledge"}'
    )

    resp = await _anthropic.messages.create(
        model=HAIKU,
        max_tokens=120,
        messages=[{"role": "user", "content": prompt}],
    )
    await mem.log_token_usage(HAIKU, resp.usage.input_tokens, resp.usage.output_tokens, use_case="self_improvement")
    raw = resp.content[0].text.strip()
    if raw.lower() == "null":
        return None
    raw = raw.lstrip("```json").lstrip("```").rstrip("```").strip()
    try:
        data = json.loads(raw)
        rule = data.get("rule", "").strip()
        return rule or None
    except Exception:
        return None


async def _heartbeat_next_run_time() -> datetime:
    """Fire heartbeat immediately on startup if it's overdue or has never run."""
    async with SessionLocal() as session:
        result = await session.execute(sa.text("""
            SELECT started_at FROM schedule_runs
            WHERE job_id = 'heartbeat' AND status = 'success'
            ORDER BY started_at DESC LIMIT 1
        """))
        row = result.first()
    if row is None:
        return datetime.now(USER_TZ)
    last = row[0].astimezone(USER_TZ)
    next_run = last + timedelta(hours=HEARTBEAT_HOURS)
    now = datetime.now(USER_TZ)
    return next_run if next_run > now else now


# ── Per-job failure tracking ──────────────────────────────────────────────────
# Tracks consecutive failure counts, last-success timestamps, and back-off
# multipliers for each scheduled job id.  Mutated only inside wrapper().

_job_state: dict[str, dict] = {}
# Maximum back-off multiplier (caps the interval doubling at 8×).
_MAX_BACKOFF = 8
# Number of consecutive failures before an alert is sent.
_ALERT_AFTER = 3


def _make_tracked_job(job_id: str, fn, **kwargs):
    """Wrap a scheduled async function so:
      - each run is recorded in schedule_runs
      - consecutive failures trigger an alert after _ALERT_AFTER in a row
      - any unhandled exception triggers an immediate alert
      - back-off multiplier doubles on each alert, caps at _MAX_BACKOFF
      - failure count + back-off reset on next success
    """
    # Initialise state entry for this job if it doesn't already exist.
    if job_id not in _job_state:
        _job_state[job_id] = {
            "consecutive_failures": 0,
            "last_success": None,       # ISO string or None
            "backoff_multiplier": 1,
        }

    async def wrapper():
        state = _job_state[job_id]

        async with SessionLocal() as session:
            async with session.begin():
                result = await session.execute(
                    sa.text("INSERT INTO schedule_runs (job_id) VALUES (:job_id) RETURNING id"),
                    {"job_id": job_id},
                )
                run_id = result.scalar()
        try:
            await fn(**kwargs)
            async with SessionLocal() as session:
                async with session.begin():
                    await session.execute(
                        sa.text("UPDATE schedule_runs SET status='success', finished_at=now() WHERE id=:id"),
                        {"id": run_id},
                    )
            # ── Success path: reset failure tracking ──────────────────────────
            if state["consecutive_failures"] > 0:
                print(
                    f"[scheduler] {job_id}: recovered after "
                    f"{state['consecutive_failures']} failure(s) — resetting back-off"
                )
            state["consecutive_failures"] = 0
            state["last_success"] = datetime.now(USER_TZ).isoformat()
            state["backoff_multiplier"] = 1

        except Exception as exc:
            async with SessionLocal() as session:
                async with session.begin():
                    await session.execute(
                        sa.text(
                            "UPDATE schedule_runs SET status='error', finished_at=now(), error=:err WHERE id=:id"
                        ),
                        {"id": run_id, "err": str(exc)[:500]},
                    )

            state["consecutive_failures"] += 1
            n = state["consecutive_failures"]
            last_ok = state["last_success"] or "unknown"
            print(
                f"[scheduler] {job_id}: failure #{n} — {exc!r}  "
                f"(last success: {last_ok})"
            )

            should_alert = (n == _ALERT_AFTER) or (n > _ALERT_AFTER and (n - _ALERT_AFTER) % _ALERT_AFTER == 0)

            if should_alert:
                # Double the back-off multiplier, capped at _MAX_BACKOFF
                state["backoff_multiplier"] = min(
                    state["backoff_multiplier"] * 2, _MAX_BACKOFF
                )
                alert_msg = (
                    f"🚨 {job_id} has failed {n} time(s) in a row\n"
                    f"Latest error: {exc}\n"
                    f"Last success: {last_ok}"
                )
                try:
                    await send_alert(alert_msg)
                except Exception as alert_exc:
                    print(f"[scheduler] alert delivery failed: {alert_exc}")

            # Apply back-off: push the job's next run time forward so APScheduler
            # waits longer before retrying.  The multiplier doubles each alert
            # cycle (3, 6, 9 … failures), capping at _MAX_BACKOFF × normal interval.
            # We do this by rescheudling via the global _scheduler reference.
            multiplier = state["backoff_multiplier"]
            if multiplier > 1 and _scheduler is not None:
                try:
                    job = _scheduler.get_job(job_id)
                    if job is not None and job.next_run_time is not None:
                        # Calculate the normal interval between the previous and
                        # next run, then add (multiplier-1) extra intervals on top.
                        normal_delta = job.trigger.get_next_fire_time(None, job.next_run_time) - job.next_run_time
                        extra_wait = normal_delta * (multiplier - 1)
                        new_next = job.next_run_time + extra_wait
                        job.modify(next_run_time=new_next)
                        print(
                            f"[scheduler] {job_id}: back-off ×{multiplier} — "
                            f"next run deferred to {new_next.isoformat()}"
                        )
                except Exception as bo_exc:
                    print(f"[scheduler] {job_id}: back-off reschedule failed (non-fatal): {bo_exc}")

            # Re-raise so APScheduler records the exception and continues scheduling.
            raise

    return wrapper


# ── Startup health checks ─────────────────────────────────────────────────────

async def _run_startup_health_checks() -> None:
    """
    Run a lightweight health check for every loaded integration that exposes
    one.  All checks run concurrently (asyncio.gather) so they don't add up
    to slow startup.  Failures send an alert but never abort the startup.
    """
    checks = []
    names = []
    for mod in _INTEGRATIONS:
        if hasattr(mod, "health_check"):
            names.append(mod.__name__.split(".")[-1])
            checks.append(mod.health_check())

    if not checks:
        print("[health] no integration health checks registered")
        return

    print(f"[health] running {len(checks)} startup health check(s): {', '.join(names)}")
    results = await asyncio.gather(*checks, return_exceptions=True)

    all_ok = True
    for name, result in zip(names, results):
        if isinstance(result, Exception):
            # health_check() itself raised — treat as failure
            ok, detail = False, str(result)
        else:
            ok, detail = result

        if ok:
            print(f"[health] ✓ {name}")
        else:
            all_ok = False
            print(f"[health] ✗ {name}: {detail}")
            try:
                await send_alert(
                    f"⚠️ Ki started but {name} is unhealthy: {detail}"
                )
            except Exception as e:
                print(f"[health] alert failed for {name}: {e}")

    if all_ok:
        print("[health] all checks passed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Phoenix tracing ────────────────────────────────────────────────────────
    # Registered here (not at module level) so all third-party imports —
    # including browser-use's observability.py — have already run by this
    # point and cannot overwrite the global tracer provider afterwards.
    try:
        from phoenix.otel import register as _phoenix_register
        from openinference.instrumentation.anthropic import AnthropicInstrumentor
        _PHOENIX_ENDPOINT = os.environ.get("PHOENIX_COLLECTOR_ENDPOINT", "http://phoenix:6006/v1/traces")
        _phoenix_register(project_name="ki", endpoint=_PHOENIX_ENDPOINT, batch=True, verbose=False)
        AnthropicInstrumentor().instrument()
        print(f"[tracing] Phoenix instrumentation active → {_PHOENIX_ENDPOINT}")
    except Exception as e:
        print(f"[tracing] Phoenix instrumentation skipped: {e}")

    # ── Global asyncio exception handler ──────────────────────────────────────
    # Catches unhandled exceptions in any asyncio Task that wasn't explicitly
    # awaited — e.g. fire-and-forget create_task() calls.  These would
    # otherwise be silently swallowed (just a log line from Python internals).
    def _handle_task_exception(loop, context):
        exc = context.get("exception")
        msg_raw = context.get("message", "unknown")
        task = context.get("task")
        task_name = getattr(task, "get_name", lambda: "unknown")()

        if exc is None:
            # Not an actual exception — just an informational context (e.g.
            # "Future exception was never retrieved" without an exc object).
            print(f"[asyncio] unhandled loop message: {msg_raw}")
            return

        # Ignore CancelledError — these are normal during shutdown.
        if isinstance(exc, asyncio.CancelledError):
            return

        alert_msg = (
            f"🚨 Unhandled asyncio exception in task '{task_name}'\n"
            f"Error: {type(exc).__name__}: {exc}"
        )
        print(f"[asyncio] {alert_msg}")
        # Schedule the coroutine without awaiting — we're inside a sync callback.
        asyncio.ensure_future(send_alert(alert_msg))

    loop = asyncio.get_event_loop()
    loop.set_exception_handler(_handle_task_exception)
    print("[asyncio] global exception handler installed")

    await run_migrations()
    await ensure_collections()
    # A bad OpenAI key or unreachable vault shouldn't stop the web app from
    # starting — log it and let the vault_sync job retry.
    try:
        await reindex()
    except Exception as e:
        print(f"[startup] vault index failed (will retry on the next vault sync): {e}")

    # Seed prompts from files into DB on first boot (migrate file → DB)
    for _prompt_name in _ALLOWED_PROMPTS:
        if await mem.get_prompt(_prompt_name) is None:
            _path = PROMPTS_DIR / f"{_prompt_name}.md"
            if _path.exists():
                await mem.set_prompt(_prompt_name, _path.read_text(encoding="utf-8").strip())
                print(f"[prompts] seeded {_prompt_name} from file into DB")

    global _scheduler
    # misfire_grace_time=None: if a cron job was missed while the container was
    # down, fire it immediately on restart rather than silently skipping it.
    _scheduler = AsyncIOScheduler(timezone=USER_TZ, job_defaults={"misfire_grace_time": None})

    heartbeat_nrt = await _heartbeat_next_run_time()
    _scheduler.add_job(
        _make_tracked_job("heartbeat", run_heartbeat, hours=HEARTBEAT_HOURS),
        "interval",
        hours=HEARTBEAT_HOURS,
        id="heartbeat",
        next_run_time=heartbeat_nrt,
    )

    # Vault sync: pull remote changes (git vaults with a remote only) and
    # re-index anything that changed. 
    async def _vault_sync_job():
        from .tools.obsidian import pull_vault
        await pull_vault()
        await reindex()

    _scheduler.add_job(
        _make_tracked_job("vault_sync", _vault_sync_job),
        "interval",
        minutes=VAULT_SYNC_MINUTES,
        id="vault_sync",
    )

    # Register schedules from all loaded integrations
    for mod in _INTEGRATIONS:
        for sched in getattr(mod, "SCHEDULES", []):
            fn = mod.get_schedule_fn(sched["fn"]) if hasattr(mod, "get_schedule_fn") else getattr(mod, sched["fn"])
            trigger_type = sched.get("trigger", "interval")
            trigger_kwargs = {k: v for k, v in sched.items() if k not in {"id", "fn", "trigger", "next_run_time_fn"}}
            if trigger_type == "cron":
                _scheduler.add_job(_make_tracked_job(sched["id"], fn), CronTrigger(**trigger_kwargs, timezone=USER_TZ), id=sched["id"])
            else:
                extra = {}
                if "next_run_time_fn" in sched:
                    nrt_fn = mod.get_schedule_fn(sched["next_run_time_fn"]) if hasattr(mod, "get_schedule_fn") else getattr(mod, sched["next_run_time_fn"])
                    extra["next_run_time"] = await nrt_fn()
                _scheduler.add_job(_make_tracked_job(sched["id"], fn), trigger_type, id=sched["id"], **trigger_kwargs, **extra)
            print(f"[scheduler] {sched['id']}: {trigger_type} {trigger_kwargs}")

    _scheduler.start()
    print(f"[scheduler] heartbeat every {HEARTBEAT_HOURS}h, next run: {heartbeat_nrt.strftime('%H:%M:%S')}")

    # ── Startup health checks (non-blocking) ───────────────────────────────────
    # Run after the scheduler starts so alerts have a live event loop to
    # deliver on.  asyncio.create_task ensures startup is never held up.
    asyncio.create_task(_run_startup_health_checks())

    # Register /kill callback so agent._kill_all() can cancel running asyncio tasks
    def _kill_tasks_callback():
        for task in list(_running_tasks.values()):
            if not task.done():
                task.cancel()
        _running_tasks.clear()
        _active_tasks.clear()

    register_kill_callback(_kill_tasks_callback)

    yield

    _scheduler.shutdown()


app = FastAPI(lifespan=lifespan)


agent = Agent()



class InboundMessage(BaseModel):
    text: str
    jid: str
    image: dict | None = None  # {"data": "<base64>", "media_type": "image/jpeg"}
    audio: dict | None = None  # {"data": "<base64>", "mime_type": "audio/ogg; codecs=opus"}
    document: dict | None = None  # {"data": "<base64>", "media_type": "application/pdf", "filename": "file.pdf"}
    channel: str = "whatsapp"


class DesktopMessage(BaseModel):
    text: str = ""
    channel: str = "desktop"  # 'desktop' or 'whatsapp'
    image: dict | None = None     # {"data": "<base64>", "media_type": "image/jpeg"}
    document: dict | None = None  # {"data": "<base64>", "media_type": "application/pdf", "filename": "file.pdf"}
    audio: dict | None = None     # {"data": "<base64>", "mime_type": "audio/webm"}
    voice_mode: bool = False      # if True, transcribe audio and generate TTS response
    reattach: bool = False        # if True, subscribe to existing task without starting a new one


class Reaction(BaseModel):
    message: str | None = None
    fallback: bool = False  # if True and message is None, mark most recent Ki WhatsApp message


class DesktopReaction(BaseModel):
    message_text: str
    reaction: str  # '👍', '❤️', '✕'


class KillRequest(BaseModel):
    channel: str = "desktop"
    secret: str | None = None  # INTERNAL_SECRET for Baileys callers


@app.post("/kill")
async def kill_session(request: Request, body: KillRequest):
    """Cancel any running agent loop for the given channel.

    Accepts either:
    - Authorization: Bearer <DESKTOP_SECRET>  (web app)
    - body.secret == INTERNAL_SECRET           (Baileys / internal callers)
    """
    auth_header = request.headers.get("Authorization", "")
    bearer_valid = bool(DESKTOP_SECRET and auth_header == f"Bearer {DESKTOP_SECRET}")
    secret_valid = bool(INTERNAL_SECRET and body.secret == INTERNAL_SECRET)
    if not bearer_valid and not secret_valid:
        raise HTTPException(status_code=401, detail="Unauthorized")

    channel = body.channel
    task = _running_tasks.get(channel)
    killed = False
    if task and not task.done():
        task.cancel()
        killed = True
        print(f"[kill] cancelled task for channel={channel!r}")
    else:
        print(f"[kill] no running task for channel={channel!r}")
    return {"ok": True, "killed": killed, "channel": channel}


@app.post("/message")
async def handle_message(msg: InboundMessage, _=Depends(_require_internal_auth)):
    # Wrap in a task so it can be cancelled mid-loop via POST /kill.
    task = asyncio.create_task(
        agent.respond(msg.text, image=msg.image, audio=msg.audio, document=msg.document, channel=msg.channel)
    )
    _running_tasks[msg.channel] = task
    try:
        reply = await task
    except asyncio.CancelledError:
        reply = "Stopped."
    finally:
        _running_tasks.pop(msg.channel, None)
    if reply is None:
        return {"reply": None}
    return {"reply": reply}


# ── Desktop endpoints (require Authorization: Bearer <DESKTOP_SECRET>) ────────

_LONG_RUNNING_TOOLS = {"browser_act", "delegate_coding_task"}


async def _run_agent_task(channel: str, msg: DesktopMessage) -> None:
    """Run the agent loop to completion, placing SSE events into _active_tasks[channel].

    Continues running even if the SSE client disconnects — the turn is saved to
    the DB on completion so the client can load it on reconnect.
    """
    queue = _active_tasks[channel]
    _running_tasks[channel] = asyncio.current_task()
    accumulated_text: list[str] = []
    had_long_tool = False
    completed_ok = False
    try:
        async for event in agent.respond_stream(
            msg.text, channel=channel, image=msg.image, document=msg.document,
            audio=msg.audio, voice_mode=msg.voice_mode
        ):
            if event.get("type") == "tool_start":
                _active_tool[channel] = {"name": event["name"], "label": event.get("label", "")}
                if event.get("name") in _LONG_RUNNING_TOOLS:
                    had_long_tool = True
            elif event.get("type") == "tool_done":
                _active_tool.pop(channel, None)
            elif event.get("type") == "text":
                accumulated_text.append(event.get("text", ""))
            elif event.get("type") == "done":
                completed_ok = True
            await queue.put(event)
    except asyncio.CancelledError:
        # Signal the SSE client to close cleanly, then propagate for proper task cleanup.
        try:
            queue.put_nowait({"type": "stopped"})
        except Exception:
            pass
        raise
    except Exception as e:
        print(f"[bg-task:{channel}] error: {e}")
        await queue.put({"type": "error", "message": str(e)})
    finally:
        _active_tasks.pop(channel, None)
        _active_tool.pop(channel, None)
        _running_tasks.pop(channel, None)
        print(f"[bg-task:{channel}] done")
        if completed_ok and accumulated_text:
            reply_preview = "".join(accumulated_text).strip()[:120]
            if had_long_tool:
                title = "Ki finished a task"
                body = reply_preview or "Tap to see the response"
            else:
                title = reply_preview or "New message"
                body = ""
            asyncio.create_task(send_push_to_all(title, body))


@app.post("/desktop/stream")
async def desktop_stream(request: Request, msg: DesktopMessage, _=Depends(_require_desktop_auth)):
    """SSE streaming endpoint for the desktop client.

    Starts a background asyncio task so the agent loop survives client disconnects.
    Pass reattach=true to subscribe to a running task without sending a new message.
    """
    channel = msg.channel

    # Start a new background task unless we're reattaching or one is already running.
    if not msg.reattach and (msg.text or msg.image or msg.document or msg.audio):
        if channel not in _active_tasks:
            queue: asyncio.Queue = asyncio.Queue()
            _active_tasks[channel] = queue
            asyncio.create_task(_run_agent_task(channel, msg))
            print(f"[bg-task:{channel}] started")
        else:
            print(f"[bg-task:{channel}] task already running, ignoring duplicate submit")

    queue = _active_tasks.get(channel)

    async def generate():
        if queue is None:
            # No running task (reattach with nothing running, or duplicate submit with nothing queued)
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
            return

        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=5.0)
                except asyncio.TimeoutError:
                    if await request.is_disconnected():
                        print(f"[bg-task:{channel}] SSE client disconnected, task continues in background")
                        return
                    yield ": keepalive\n\n"
                    continue
                yield f"data: {json.dumps(event)}\n\n"
                if event.get("type") in ("done", "error", "stopped"):
                    break
        except GeneratorExit:
            print(f"[bg-task:{channel}] SSE generator closed, task continues in background")

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/desktop/task-status")
async def desktop_task_status(channel: str = "desktop", _=Depends(_require_desktop_auth)):
    """Return whether a background agent task is currently running for the given channel."""
    return {"running": channel in _active_tasks, "active_tool": _active_tool.get(channel)}


@app.get("/desktop/history")
async def desktop_history(channel: str = "desktop", limit: int = 60, after: str | None = None, _=Depends(_require_desktop_auth)):
    """Return recent conversation history for display in the desktop UI."""
    messages = await _mem_module.get_recent_conversation_for_display(limit=limit, channel=channel, after=after)
    return {"messages": messages, "channel": channel}


@app.get("/desktop/today-note")
async def desktop_today_note(_=Depends(_require_desktop_auth)):
    """Return today's daily note content for the /today modal. No conversation save."""
    from .tools.obsidian import read_note
    today = datetime.now(USER_TZ).strftime("%Y-%m-%d")
    daily_note = f"daily/{today}.md"
    try:
        content = await read_note(daily_note)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"content": content, "date": today}


@app.get("/desktop/vault/tasks")
async def desktop_vault_tasks(_=Depends(_require_desktop_auth)):
    """Return all tasks from vault files for the /today modal task renderer."""
    import re as _re
    from .tools.obsidian import VAULT

    open_re = _re.compile(r'^[-*+]\s+\[ \]\s+(.+)$')
    done_re = _re.compile(r'^[-*+]\s+\[[xX]\]\s+(.+)$')
    tag_re = _re.compile(r'#[\w/-]+')
    tasks = []

    if not VAULT.exists():
        return {"tasks": []}

    for md_file in VAULT.rglob("*.md"):
        if any(part.startswith(".") for part in md_file.parts):
            continue
        try:
            rel_path = str(md_file.relative_to(VAULT))
            for line in md_file.read_text(errors="replace").splitlines():
                stripped = line.strip()
                open_m = open_re.match(stripped)
                done_m = done_re.match(stripped)
                if open_m:
                    text = open_m.group(1).strip()
                    done = False
                elif done_m:
                    text = done_m.group(1).strip()
                    done = True
                else:
                    continue
                tasks.append({
                    "text": text,
                    "path": rel_path,
                    "tags": tag_re.findall(text),
                    "done": done,
                })
        except Exception:
            continue

    return {"tasks": tasks}


class _TaskUpdateRequest(BaseModel):
    file_path: str
    task_text: str
    done: bool


@app.post("/desktop/vault/task-update")
async def desktop_vault_task_update(body: _TaskUpdateRequest, _=Depends(_require_desktop_auth)):
    """Toggle a task checkbox in a vault file and commit the change."""
    import re as _re
    from .tools.obsidian import VAULT, _safe_path, _git

    try:
        path = _safe_path(body.file_path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not path.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {body.file_path}")

    await _git("pull", "--quiet", "--autostash")

    lines = path.read_text(errors="replace").splitlines(keepends=True)
    task_text_norm = body.task_text.strip()

    open_re = _re.compile(r'^([-*+]\s+)\[ \](\s+.+)$')
    done_re = _re.compile(r'^([-*+]\s+)\[[xX]\](\s+.+)$')

    updated = False
    for i, line in enumerate(lines):
        stripped = line.rstrip('\n').rstrip('\r')
        m = open_re.match(stripped) or done_re.match(stripped)
        if not m:
            continue
        # Extract text portion and compare
        line_text = m.group(2).strip()
        if line_text != task_text_norm:
            continue
        prefix = m.group(1)
        rest = m.group(2)
        if body.done:
            lines[i] = f"{prefix}[x]{rest}\n"
        else:
            lines[i] = f"{prefix}[ ]{rest}\n"
        updated = True
        break

    if not updated:
        raise HTTPException(status_code=404, detail="Task line not found in file")

    path.write_text("".join(lines))
    rel = str(path.relative_to(VAULT))
    await _git("add", rel)
    state = "done" if body.done else "open"
    await _git("commit", "-m", f"ki: mark task {state}: {task_text_norm[:60]}")
    await _git("push")

    return {"ok": True}


# ── Journal PDF helpers ───────────────────────────────────────────────────────

def _journal_pdf_css() -> str:
    return """
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
        font-family: 'Cormorant Garamond', Georgia, 'Times New Roman', serif;
        color: #3d2c35;
        font-size: 11pt;
        line-height: 1.8;
    }
    .doc-header { display: flex; align-items: center; justify-content: center; padding: 10px 0 8px; margin-bottom: 10px; gap: 20px; }
    .header-center { text-align: center; flex: 1; }
    .header-portrait { display: flex; flex-direction: column; align-items: center; gap: 6px; flex-shrink: 0; }
    .portrait-circle { width: 85px; height: 85px; border-radius: 50%; object-fit: cover; border: 2px solid #e8ccd8; }
    .portrait-label { font-size: 9pt; color: #b07898; font-style: italic; text-align: center; }
    h1.doc-title {
        font-family: 'Playfair Display', Georgia, serif;
        font-size: 28pt; color: #7a3255; font-weight: 700;
        letter-spacing: 0.04em; margin-bottom: 4px;
    }
    .doc-subtitle {
        font-family: 'Playfair Display', Georgia, serif;
        font-size: 11pt; color: #b07898; font-style: italic; letter-spacing: 0.04em;
    }
    .doc-date { font-size: 9pt; color: #b07898; margin-top: 6px; letter-spacing: 0.15em; text-transform: uppercase; }
    .section-divider { margin: 12px 0 8px; padding-bottom: 4px; }
    .section-divider.page-break-before { page-break-before: always; }
    .section-divider h2 {
        font-family: 'Playfair Display', Georgia, serif;
        font-size: 13pt; color: #7a3255; font-weight: normal; letter-spacing: 0.1em; text-transform: uppercase;
    }
    .content-block h1 {
        font-family: 'Playfair Display', Georgia, serif;
        font-size: 18pt; color: #7a3255; font-weight: 700;
        letter-spacing: 0.03em; margin: 16px 0 8px;
    }
    .content-block h2 {
        font-family: 'Playfair Display', Georgia, serif;
        font-size: 14pt; color: #7a3255; font-weight: 600;
        letter-spacing: 0.02em; margin: 12px 0 6px;
    }
    .content-block h3 {
        font-family: 'Playfair Display', Georgia, serif;
        font-size: 12pt; color: #9a6080; font-weight: normal; font-style: italic; margin: 10px 0 4px;
    }
    .content-block h4, .content-block h5, .content-block h6 {
        font-family: 'Playfair Display', Georgia, serif;
        font-size: 11pt; color: #c9a0dc; font-weight: bold; margin: 8px 0 3px;
    }
    .content-block p { margin: 5px 0; }
    .content-block ul, .content-block ol { margin: 5px 0 5px 20px; padding: 0; }
    .content-block ul { list-style-type: disc; }
    .content-block ol { list-style-type: decimal; }
    .content-block li { margin: 2px 0; display: list-item; }
    .content-block li > ul { list-style-type: circle; margin: 2px 0 2px 20px; }
    .content-block li > ul > li > ul { list-style-type: square; }
    .content-block li > ol { margin: 2px 0 2px 20px; }
    .content-block strong { font-weight: bold; color: #3d2c35; }
    .content-block em { font-style: italic; color: #9a6080; }
    .content-block hr { border: none; border-top: 1px solid #e8ccd8; margin: 8px 0; }
    .content-block blockquote { margin: 5px 0; padding: 4px 12px; border-left: 3px solid #e8ccd8; color: #9a6080; font-style: italic; }
    .content-block blockquote p { margin: 3px 0; }
    .content-block code { font-family: 'Courier New', monospace; font-size: 9pt; background: #f8eef4; color: #7a3255; padding: 1px 4px; border-radius: 2px; }
    .content-block pre { font-family: 'Courier New', monospace; font-size: 9pt; background: #f8eef4; color: #7a3255; padding: 8px 12px; margin: 8px 0; white-space: pre-wrap; }
    .content-block pre code { background: none; padding: 0; }
    .content-block mark { background: #fde8f4; color: #3d2c35; padding: 0 2px; }
    .journal-entry { margin: 0 0 10px; padding: 6px 0; border: none; }
    .journal-entry hr, .entry-body hr { border: none; border-top: 1px solid #e8ccd8; margin: 8px 0; }
    .entry-date {
        font-family: 'Playfair Display', Georgia, serif;
        font-size: 12pt; color: #c9a0dc; font-weight: normal; font-style: italic; margin-bottom: 4px;
    }
    .no-entries { color: #c9a0dc; font-style: italic; }
    .task-config { margin: 0; }
    .task-section { margin: 0 0 12px; }
    .task-section-heading {
        font-family: 'Playfair Display', Georgia, serif;
        font-size: 11pt; color: #7a3255; font-weight: normal;
        font-style: italic; letter-spacing: 0.08em;
        text-transform: uppercase; margin: 0 0 4px;
        padding-bottom: 3px;
    }
    .task-table { width: 100%; border-collapse: collapse; font-size: 10pt; }
    .task-table th {
        font-family: 'Playfair Display', Georgia, serif;
        font-size: 9.5pt; color: #7a3255; font-weight: 600;
        text-align: left; padding: 4px 8px;
        border-bottom: 2px solid #e8ccd8; letter-spacing: 0.05em; text-transform: uppercase;
    }
    .task-table td { color: #3d2c35; padding: 3px 8px; border-bottom: 1px solid #f4e0eb; vertical-align: top; }
    .task-table tr:last-child td { border-bottom: none; }
    """


def _obsidian_md_to_html(text: str) -> str:
    """Strip Obsidian-specific syntax and convert markdown to HTML."""
    import re as _re
    import markdown as _md
    # Strip YAML frontmatter
    if text.startswith("---"):
        end = text.find("---", 3)
        if end != -1:
            text = text[end + 3:].lstrip("\n")
    # Strip unresolved ![[embeds]]
    text = _re.sub(r"!\[\[[^\]]+\]\]", "", text)
    # [[wikilink|display]] → display
    text = _re.sub(r"\[\[[^\]|]+\|([^\]]+)\]\]", r"\1", text)
    # [[wikilink]] → plain text
    text = _re.sub(r"\[\[([^\]]+)\]\]", r"\1", text)
    # Obsidian task checkboxes: - [x] → checked, - [ ] → unchecked
    text = _re.sub(r"^(\s*)-\s+\[x\]\s+", r"\1- ☑ ", text, flags=_re.MULTILINE | _re.IGNORECASE)
    text = _re.sub(r"^(\s*)-\s+\[ \]\s+", r"\1- ☐ ", text, flags=_re.MULTILINE)
    # Strip Obsidian tags (#tag) — bare hashtags not followed by a space (not headings)
    text = _re.sub(r"(?<!\n)#([A-Za-z][A-Za-z0-9_/-]*)", r"\1", text)
    # Obsidian highlights ==text== → <mark>
    text = _re.sub(r"==([^=\n]+)==", r"<mark>\1</mark>", text)
    # Ensure blank lines before headings and around list blocks so the markdown
    # parser handles them correctly (vault content often omits these blank lines)
    lines = text.split("\n")
    out = []
    in_list = False
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        is_list = bool(_re.match(r"[-*+] |^\d+\. ", stripped))
        is_heading = stripped.startswith("#")
        if is_heading and out and out[-1] != "":
            out.append("")
        if is_list and not in_list and out and out[-1] != "":
            out.append("")
        if not is_list and in_list and out and out[-1] != "":
            out.append("")
        out.append(line)
        in_list = is_list
    text = "\n".join(out)
    return _md.markdown(text, extensions=["extra", "sane_lists"])


def _task_config_to_html(raw: str) -> str:
    import re as _re
    import yaml as _yaml

    raw = raw.strip()
    # Strip YAML frontmatter
    if raw.startswith("---"):
        end = raw.find("---", 3)
        if end != -1:
            raw = raw[end + 3:].lstrip("\n")

    try:
        data = _yaml.safe_load(raw)
    except Exception:
        return f'<div class="task-config"><pre>{raw}</pre></div>'

    if not isinstance(data, dict):
        return f'<div class="task-config"><pre>{raw}</pre></div>'

    def strip_tags(text) -> str:
        return _re.sub(r"\s*#[A-Za-z][A-Za-z0-9_/-]*", "", str(text)).strip()

    def simple_table(tasks, heading):
        rows = "".join(f"<tr><td>{strip_tags(t)}</td></tr>" for t in tasks)
        return (
            f'<div class="task-section">'
            f'<h3 class="task-section-heading">{heading}</h3>'
            f'<table class="task-table"><tbody>{rows}</tbody></table>'
            f'</div>'
        )

    html_parts = []

    if "daily" in data and isinstance(data["daily"], list):
        html_parts.append(simple_table(data["daily"], "Daily"))

    if "weekdays" in data and isinstance(data["weekdays"], list):
        html_parts.append(simple_table(data["weekdays"], "Weekdays"))

    if "biweekly" in data:
        bw = data["biweekly"]
        tasks = bw.get("tasks", []) if isinstance(bw, dict) else (bw if isinstance(bw, list) else [])
        days_label = ""
        if isinstance(bw, dict) and "days" in bw:
            days_label = f" ({', '.join(str(d).capitalize() for d in bw['days'])})"
        if tasks:
            rows = []
            for t in tasks:
                name = strip_tags(t.get("task", t) if isinstance(t, dict) else t)
                rows.append(f"<tr><td>{name}</td></tr>")
            html_parts.append(
                f'<div class="task-section">'
                f'<h3 class="task-section-heading">Biweekly{days_label}</h3>'
                f'<table class="task-table"><tbody>{"".join(rows)}</tbody></table>'
                f'</div>'
            )

    if "weekly" in data and isinstance(data["weekly"], list):
        by_day: dict = {}
        for item in data["weekly"]:
            if isinstance(item, dict):
                day = str(item.get("day", "unknown")).capitalize()
                by_day.setdefault(day, []).append(strip_tags(item.get("task", "")))
            else:
                by_day.setdefault("Weekly", []).append(strip_tags(item))
        for day, tasks in by_day.items():
            rows = "".join(f"<tr><td>{t}</td></tr>" for t in tasks)
            html_parts.append(
                f'<div class="task-section">'
                f'<h3 class="task-section-heading">Weekly — {day}</h3>'
                f'<table class="task-table"><tbody>{rows}</tbody></table>'
                f'</div>'
            )

    if "fortnightly" in data:
        fn = data["fortnightly"]
        tasks = fn.get("tasks", []) if isinstance(fn, dict) else (fn if isinstance(fn, list) else [])
        if tasks:
            rows = []
            for t in tasks:
                name = strip_tags(t.get("task", t) if isinstance(t, dict) else t)
                rows.append(f"<tr><td>{name}</td></tr>")
            html_parts.append(
                f'<div class="task-section">'
                f'<h3 class="task-section-heading">Fortnightly</h3>'
                f'<table class="task-table"><tbody>{"".join(rows)}</tbody></table>'
                f'</div>'
            )

    if "interval" in data and isinstance(data["interval"], list):
        rows = []
        for t in data["interval"]:
            if isinstance(t, dict):
                task = strip_tags(t.get("task", ""))
                every = t.get("every", "—")
                last_done = t.get("last_done", "—")
                rows.append(f"<tr><td>{task}</td><td>{every}</td><td>{last_done}</td></tr>")
        if rows:
            html_parts.append(
                f'<div class="task-section">'
                f'<h3 class="task-section-heading">Interval</h3>'
                f'<table class="task-table">'
                f'<thead><tr><th>Task</th><th>Every</th><th>Last Done</th></tr></thead>'
                f'<tbody>{"".join(rows)}</tbody>'
                f'</table></div>'
            )

    return '<div class="task-config">' + "\n".join(html_parts) + "</div>"


async def _collect_tagged_tasks() -> list[str]:
    """Scan the vault for all open tasks tagged #tracked, return cleaned task strings."""
    import re as _re
    from .tools.obsidian import VAULT

    task_pat = _re.compile(r"^- \[ \] (.+)", _re.MULTILINE)
    tasks: list[tuple[bool, str]] = []  # (is_priority, text)

    for md_file in VAULT.rglob("*.md"):
        # Skip config directory
        try:
            rel = md_file.relative_to(VAULT)
        except ValueError:
            continue
        if rel.parts[0] == "config":
            continue

        try:
            content = md_file.read_text(errors="replace")
        except Exception:
            continue

        for m in task_pat.finditer(content):
            raw_text = m.group(1)
            if "#tracked" not in raw_text:
                continue
            # Strip all #tags and clean up
            cleaned = _re.sub(r"#\S+", "", raw_text).strip()
            cleaned = _re.sub(r"\s{2,}", " ", cleaned)
            if not cleaned:
                continue
            is_priority = "#priority" in raw_text
            tasks.append((is_priority, cleaned))

    # Priority tasks first, then alphabetical within each group
    tasks.sort(key=lambda t: (not t[0], t[1].lower()))
    return [text for _, text in tasks]


def _load_portrait_data_uri(vault_path, filename: str) -> str | None:
    import base64 as _b64
    img_path = vault_path / filename
    try:
        data = img_path.read_bytes()
        b64 = _b64.b64encode(data).decode()
        return f"data:image/png;base64,{b64}"
    except Exception:
        return None


async def _build_journal_html(gen_date: str) -> str:
    import re as _re
    from .tools.obsidian import read_note, VAULT

    journal_raw = await read_note("Journal.md")
    task_config_raw = await read_note("config/task-config.yaml")

    # Strip YAML frontmatter
    if journal_raw.startswith("---"):
        end = journal_raw.find("---", 3)
        if end != -1:
            journal_raw = journal_raw[end + 3:].lstrip("\n")

    # Strip table rows containing image embeds (the photos table at the top), then orphaned separators
    journal_raw = _re.sub(r"(?m)^\|[^\n]*!\[\[[^\]]+\]\][^\n]*\n", "", journal_raw)
    journal_raw = _re.sub(r"(?m)^\|[-| :]+\|\s*\n", "", journal_raw)

    # Pre-render the three dynamic blocks

    # 1. Task config — replaces ![[task-config]]
    task_config_html = _task_config_to_html(task_config_raw)

    # 2. Standalone tracked tasks — replaces ```tasks ... ```
    tracked_tasks = await _collect_tagged_tasks()
    if tracked_tasks:
        task_items = "".join(f"<li>{t}</li>" for t in tracked_tasks)
        standalone_tasks_html = f'<div class="content-block"><ul>{task_items}</ul></div>'
    else:
        standalone_tasks_html = '<div class="content-block"><p class="no-entries">No open tracked tasks found.</p></div>'

    # 3. Journal entries from daily notes — replaces ```dataviewjs ... ```
    journal_entries = []
    daily_dir = VAULT / "daily"
    if daily_dir.exists():
        date_pat = _re.compile(r"^(\d{4}-\d{2}-\d{2})\.md$")
        for f in sorted(daily_dir.glob("*.md"), reverse=True):
            m = date_pat.match(f.name)
            if not m:
                continue
            date_str = m.group(1)
            try:
                raw = f.read_text(errors="replace")
                sec = _re.search(
                    r"### (?:\[\[)?Journal(?:\]\])?(.*?)(?=\n### |\n## |\Z)",
                    raw,
                    _re.DOTALL,
                )
                if sec:
                    entry_text = sec.group(1).strip()
                    # Skip days that only have the unfilled placeholder
                    _bare = entry_text.strip("`").strip()
                    if entry_text and _bare != "<injected journal entry goes here>":
                        journal_entries.append((date_str, entry_text))
            except Exception:
                continue

    entries_parts = []
    for date_str, entry_text in journal_entries:
        try:
            d = datetime.strptime(date_str, "%Y-%m-%d")
            nice = f"{d.day} {d.strftime('%B %Y')}"
        except Exception:
            nice = date_str
        entry_html = _obsidian_md_to_html(entry_text)
        entry_html = _re.sub(r'<hr\s*/?>', '', entry_html)
        entries_parts.append(
            f'<div class="journal-entry">'
            f'<div class="entry-date">{nice}</div>'
            f'<div class="content-block entry-body">{entry_html}</div>'
            f"</div>"
        )
    journal_entries_html = (
        '<div class="entries">' + "\n".join(entries_parts) + "</div>"
        if entries_parts
        else '<p class="no-entries">No entries found.</p>'
    )

    # Split the raw note at the three special block types and process each segment inline.
    # Structure and headings come directly from the note — nothing is hard-coded here.
    special_block_re = _re.compile(
        r"(!\[\[task-config\]\]|```tasks\b[\s\S]*?```|```dataviewjs\b[\s\S]*?```)",
        _re.IGNORECASE,
    )
    segments = special_block_re.split(journal_raw)

    html_parts = []
    for i, seg in enumerate(segments):
        if i % 2 == 0:
            converted = _obsidian_md_to_html(seg)
            if converted.strip():
                html_parts.append(f'<div class="content-block">{converted}</div>')
        else:
            seg_lower = seg.lower()
            if "task-config" in seg_lower:
                html_parts.append(task_config_html)
            elif seg_lower.startswith("```tasks"):
                html_parts.append(standalone_tasks_html)
            elif seg_lower.startswith("```dataviewjs"):
                html_parts.append(journal_entries_html)

    body_html = "\n".join(html_parts)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,400;0,600;1,400;1,600&family=Playfair+Display:ital,wght@0,400;0,600;0,700;1,400;1,600&display=swap" rel="stylesheet">
<style>{_journal_pdf_css()}</style>
</head>
<body>

<div class="doc-header">
  <div class="header-center">
    <h1 class="doc-title">Journal</h1>
    <div class="doc-date">Generated {gen_date}</div>
  </div>
</div>

{body_html}

</body></html>"""


@app.get("/desktop/journal-pdf")
async def desktop_journal_pdf(_=Depends(_require_desktop_auth)):
    """Generate the Journal as a PDF and return for download."""
    import io
    from playwright.async_api import async_playwright

    now = datetime.now(USER_TZ)
    gen_date = f"{now.day} {now.strftime('%B %Y')}"
    filename = f"journal-{now.strftime('%Y-%m-%d')}.pdf"

    try:
        html = await _build_journal_html(gen_date)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to build journal: {e}")

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            try:
                page = await browser.new_page()
                await page.set_content(html, wait_until="domcontentloaded")
                pdf_bytes = await page.pdf(
                    format="A4",
                    print_background=True,
                    margin={"top": "8mm", "right": "10mm", "bottom": "8mm", "left": "10mm"},
                )
            finally:
                await browser.close()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"PDF generation failed: {e}")

    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/desktop/media")
async def desktop_media(limit: int = 500, _=Depends(_require_desktop_auth)):
    """Return all media (images) across all channels for the media grid."""
    media = await _mem_module.get_all_media(limit=limit)
    return {"media": media}


@app.delete("/desktop/media/{message_id}")
async def desktop_media_delete(message_id: int, _=Depends(_require_desktop_auth)):
    """Remove the image from a conversation row (nulls image_data/image_media_type)."""
    deleted = await _mem_module.delete_media(message_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Media not found")
    return {"ok": True}


@app.post("/desktop/bridge")
async def bridge_to_desktop(_=Depends(_require_desktop_auth)):
    """Summarise the recent WhatsApp conversation and inject a handoff note into the desktop channel."""
    messages = await _mem_module.get_recent_conversation_for_display(limit=20, channel="whatsapp")
    if not messages:
        return {"ok": True, "created": False}

    convo_text = "\n".join(
        f"{USER_LABEL if m['role'] == 'user' else 'Ki'}: {m['content'][:400]}"
        for m in messages
    )
    resp = await _anthropic.messages.create(
        model=HAIKU,
        max_tokens=200,
        messages=[{
            "role": "user",
            "content": (
                "The user is switching from a WhatsApp conversation with Ki to their desktop app to continue.\n"
                "Write a brief handoff message from Ki (2-3 sentences) that:\n"
                "- Acknowledges the switch from mobile\n"
                "- Summarises what was being discussed or worked on\n"
                "- Warm and direct, dry Kiwi wit, plain text, no markdown\n\n"
                f"Recent conversation:\n{convo_text}"
            ),
        }],
    )
    await mem.log_token_usage(HAIKU, resp.usage.input_tokens, resp.usage.output_tokens, use_case="bridge")
    handoff = resp.content[0].text.strip()
    await _mem_module.save_turn(user="", assistant=handoff, channel="desktop")
    print(f"[bridge] handoff saved to desktop channel")
    return {"ok": True, "created": True}


@app.post("/desktop/reaction")
async def desktop_reaction(body: DesktopReaction, _=Depends(_require_desktop_auth)):
    """Save a memory when the user reacts to a Ki message in the desktop app."""
    category, content = await _classify_memory(body.reaction, body.message_text)
    await mem.save_memory(category=category, content=content, source="desktop_reaction")
    print(f"[desktop_reaction] {body.reaction} → [{category}] {content}")
    return {"ok": True}


@app.post("/desktop/reaction-negative")
async def desktop_reaction_negative(body: DesktopReaction, _=Depends(_require_desktop_auth)):
    """
    Called when the user clicks 👎 on a Ki message in the desktop app.
    Marks the row thumbs_down, then classifies the error and saves a tool_rule if applicable.
    """
    msg_preview = body.message_text[:80]
    conv_id = None
    tool_calls = None

    async with SessionLocal() as session:
        async with session.begin():
            result = await session.execute(
                sa.text("""
                    UPDATE conversations SET signal = 'thumbs_down'
                    WHERE id = (
                        SELECT id FROM conversations
                        WHERE role = 'assistant' AND content = :content
                        ORDER BY created_at DESC LIMIT 1
                    )
                    RETURNING id, tool_calls
                """),
                {"content": body.message_text},
            )
            row = result.first()
            if row:
                conv_id = row.id
                tool_calls = row.tool_calls  # already a list/dict from JSONB

    correction = None
    if conv_id:
        async with SessionLocal() as session:
            res = await session.execute(
                sa.text("SELECT correction_text FROM corrections WHERE conversation_id = :id ORDER BY created_at DESC LIMIT 1"),
                {"id": conv_id},
            )
            corr = res.first()
            if corr:
                correction = corr.correction_text

    rule = await _classify_tool_error(body.message_text, tool_calls, correction)
    if rule:
        await mem.save_memory(category="tool_rule", content=rule, source="thumbs_down")
        print(f"[tool_rule] {rule}")

    print(f"[desktop_reaction_negative] 👎 on: {msg_preview}")
    return {"ok": True}


@app.post("/reaction")
async def handle_reaction(reaction: Reaction, _=Depends(_require_internal_auth)):
    """
    Called when the user gives a 👍 to a Ki message on WhatsApp.
    Classifies and saves a memory so Ki stops surfacing the topic.
    """
    category, content = await _classify_memory("👍", reaction.message)
    await mem.save_memory(category=category, content=content, source="reaction")
    print(f"[reaction] closed: [{category}] {content}")
    return {"ok": True}


@app.post("/reaction-negative")
async def handle_reaction_negative(reaction: Reaction, _=Depends(_require_internal_auth)):
    """
    Called when the user gives a 👎 to a Ki message on WhatsApp.
    Marks the row thumbs_down, then classifies the error and saves a tool_rule if applicable.
    If message is None and fallback=True, marks the most recent Ki WhatsApp assistant message.
    """
    msg_preview = (reaction.message or "")[:80]
    conv_id = None
    tool_calls = None
    message_text = reaction.message

    async with SessionLocal() as session:
        async with session.begin():
            if message_text:
                result = await session.execute(
                    sa.text("""
                        UPDATE conversations SET signal = 'thumbs_down'
                        WHERE id = (
                            SELECT id FROM conversations
                            WHERE role = 'assistant' AND content = :content
                            ORDER BY created_at DESC LIMIT 1
                        )
                        RETURNING id, content, tool_calls
                    """),
                    {"content": message_text},
                )
            elif reaction.fallback:
                # Cache miss — mark the most recent Ki WhatsApp assistant message
                result = await session.execute(
                    sa.text("""
                        UPDATE conversations SET signal = 'thumbs_down'
                        WHERE id = (
                            SELECT id FROM conversations
                            WHERE role = 'assistant' AND channel = 'whatsapp'
                              AND (signal IS NULL OR signal != 'thumbs_down')
                            ORDER BY created_at DESC LIMIT 1
                        )
                        RETURNING id, content, tool_calls
                    """),
                )
            else:
                print("[signal] 👎 received with no message and no fallback flag — ignoring")
                return {"ok": True}

            row = result.first()
            if row:
                conv_id = row.id
                tool_calls = row.tool_calls
                if not message_text:
                    message_text = row.content
                    print(f"[signal] 👎 fallback matched: {(message_text or '')[:80]!r}")
            else:
                print(f"[signal] 👎 no DB row matched for: {msg_preview!r}")

    correction = None
    if conv_id:
        async with SessionLocal() as session:
            res = await session.execute(
                sa.text("SELECT correction_text FROM corrections WHERE conversation_id = :id ORDER BY created_at DESC LIMIT 1"),
                {"id": conv_id},
            )
            corr = res.first()
            if corr:
                correction = corr.correction_text

    rule = await _classify_tool_error(message_text, tool_calls, correction)
    if rule:
        await mem.save_memory(category="tool_rule", content=rule, source="thumbs_down")
        print(f"[tool_rule] {rule}")

    print(f"[signal] 👎 on: {(message_text or '')[:80]!r}")
    return {"ok": True}


@app.get("/admin/signals")
async def admin_signals(_=Depends(_require_desktop_auth)):
    """Return thumbs-down signals, corrections, and learned tool rules for the admin Signals tab."""
    async with SessionLocal() as session:
        # Last 50 thumbs-down rows, with tool_calls so the UI can show what Ki used
        td_result = await session.execute(sa.text("""
            SELECT id, content, created_at, tool_calls
            FROM conversations
            WHERE signal = 'thumbs_down'
            ORDER BY created_at DESC
            LIMIT 50
        """))
        thumbs_down = [
            {
                "id": row.id,
                "content": row.content,
                "created_at": row.created_at.isoformat(),
                "tool_calls": row.tool_calls,
            }
            for row in td_result
        ]

        # Last 50 corrections, most recent first
        corr_result = await session.execute(sa.text("""
            SELECT c.id, c.prior_assistant_msg, c.correction_text, c.created_at, c.conversation_id
            FROM corrections c
            ORDER BY c.created_at DESC
            LIMIT 50
        """))
        corrections = [
            {
                "id": row.id,
                "prior_assistant_msg": row.prior_assistant_msg,
                "correction_text": row.correction_text,
                "created_at": row.created_at.isoformat(),
                "conversation_id": row.conversation_id,
            }
            for row in corr_result
        ]

        # Learned tool-selection rules
        rules_result = await session.execute(sa.text("""
            SELECT id, content, updated_at FROM memories
            WHERE category = 'tool_rule'
            ORDER BY updated_at DESC
            LIMIT 20
        """))
        tool_rules = [
            {"id": row.id, "content": row.content, "updated_at": row.updated_at.isoformat()}
            for row in rules_result
        ]

    return {"thumbs_down": thumbs_down, "corrections": corrections, "tool_rules": tool_rules}


@app.get("/push/vapid-public-key")
async def push_vapid_key():
    if not VAPID_PUBLIC_KEY:
        raise HTTPException(status_code=404, detail="VAPID not configured — add VAPID_PUBLIC_KEY env var")
    return {"publicKey": VAPID_PUBLIC_KEY}


class PushSubscription(BaseModel):
    endpoint: str
    keys: dict  # {"p256dh": str, "auth": str}


@app.post("/push/subscribe")
async def push_subscribe(sub: PushSubscription, _=Depends(_require_desktop_auth)):
    await _mem_module.save_push_subscription(
        endpoint=sub.endpoint,
        p256dh=sub.keys["p256dh"],
        auth=sub.keys["auth"],
    )
    return {"ok": True}


class PushUnsubscribe(BaseModel):
    endpoint: str


@app.post("/push/unsubscribe")
async def push_unsubscribe(body: PushUnsubscribe, _=Depends(_require_desktop_auth)):
    await _mem_module.delete_push_subscriptions([body.endpoint])
    return {"ok": True}


@app.post("/heartbeat/trigger")
async def trigger_heartbeat(_=Depends(_require_internal_auth)):
    """Manually trigger the heartbeat — useful for testing without waiting hours."""
    state = await run_heartbeat(hours=HEARTBEAT_HOURS)
    return {"ok": True, "state": state}


@app.post("/daily-note/create")
async def trigger_daily_note_create(_=Depends(_require_internal_auth)):
    """Manually trigger today's daily note creation immediately."""
    from .integrations.daily_note import run_daily_note_creator
    await run_daily_note_creator()
    return {"ok": True}


@app.get("/integrations")
async def list_integrations(_=Depends(_require_desktop_auth)):
    """List all loaded integrations with their tools, schedules, and required env vars."""
    return {
        "integrations": [
            {
                "name": mod.__name__.split(".")[-1],
                "tools": [t["name"] for t in mod.TOOL_SCHEMAS],
                "schedules": [s["id"] for s in getattr(mod, "SCHEDULES", [])],
                "required_env_vars": getattr(mod, "REQUIRED_ENV_VARS", []),
            }
            for mod in _INTEGRATIONS
        ]
    }


@app.get("/admin/schedule-runs")
async def admin_schedule_runs(_=Depends(_require_desktop_auth)):
    """Return the last 10 runs per scheduled job, most recent first."""
    async with SessionLocal() as session:
        result = await session.execute(sa.text("""
            SELECT job_id, started_at, finished_at, status, error
            FROM schedule_runs
            ORDER BY started_at DESC
            LIMIT 200
        """))
        rows = result.mappings().all()

    by_job: dict = {}
    for row in rows:
        job = row["job_id"]
        if job not in by_job:
            by_job[job] = []
        if len(by_job[job]) >= 10:
            continue
        duration_ms = None
        if row["finished_at"] and row["started_at"]:
            duration_ms = int((row["finished_at"] - row["started_at"]).total_seconds() * 1000)
        by_job[job].append({
            "started_at":  row["started_at"].isoformat(),
            "finished_at": row["finished_at"].isoformat() if row["finished_at"] else None,
            "status":      row["status"],
            "error":       row["error"],
            "duration_ms": duration_ms,
        })

    # Include next_run_time and registration status for every known job.
    job_meta: dict = {}
    if _scheduler:
        for job in _scheduler.get_jobs():
            job_meta[job.id] = job.next_run_time.isoformat() if job.next_run_time else None

    return {"runs": by_job, "jobs": job_meta}


@app.get("/admin/state")
async def admin_state(_=Depends(_require_desktop_auth)):
    """Return the latest heartbeat state snapshot."""
    state = await mem.get_latest_heartbeat_state()
    return {"state": state}


@app.get("/admin/states")
async def admin_states(_=Depends(_require_desktop_auth)):
    """Return all heartbeat state snapshots, newest first."""
    states = await mem.get_all_heartbeat_states()
    return {"states": states}


@app.get("/admin/memories")
async def admin_memories(_=Depends(_require_desktop_auth)):
    """Return all memories (excluding todos) for display."""
    memories = await mem.get_memories()
    return {"memories": memories}


@app.get("/admin/reminders")
async def admin_reminders(_=Depends(_require_desktop_auth)):
    """Return all upcoming (unsent, uncancelled) reminders ordered by scheduled time."""
    async with SessionLocal() as session:
        result = await session.execute(sa.text("""
            SELECT id, message, remind_at
            FROM reminders
            WHERE sent = false AND cancelled = false
            ORDER BY remind_at ASC
        """))
        rows = result.fetchall()

    reminders = []
    for row in rows:
        remind_at_local = row[2].astimezone(USER_TZ)
        reminders.append({
            "id": row[0],
            "message": row[1],
            "remind_at_utc": row[2].isoformat(),
            "remind_at_localdt": remind_at_local.isoformat(),
        })
    return {"reminders": reminders}


@app.get("/admin/vault-status")
async def admin_vault_status(_=Depends(_require_desktop_auth)):
    """Return vault indexer commit and Qdrant point count."""
    async with SessionLocal() as session:
        result = await session.execute(
            sa.text("SELECT value FROM indexer_state WHERE key = 'vault_commit'")
        )
        row = result.first()
        vault_commit = row.value if row else None

    try:
        info = await qdrant_client.get_collection("obsidian")
        point_count = info.points_count
    except Exception:
        point_count = None

    return {"vault_commit": vault_commit, "point_count": point_count}


@app.get("/admin/settings")
async def admin_get_settings(_=Depends(_require_desktop_auth)):
    """Return editable runtime settings."""
    image_gen_model = await mem.get_setting("image_gen_model", "grok-imagine-image")
    return {"image_gen_model": image_gen_model}


@app.get("/admin/session")
async def admin_get_session(_=Depends(_require_desktop_auth)):
    """Return info about the current conversation session."""
    session_gap_hours = float(os.environ.get("SESSION_GAP_HOURS", "0.5"))
    async with SessionLocal() as db:
        result = await db.execute(sa.text("""
            SELECT role, created_at FROM (
                SELECT role, created_at FROM conversations
                ORDER BY created_at DESC LIMIT 200
            ) sub
            WHERE (SELECT MAX(created_at) FROM conversations)
                  > now() - (:gap * INTERVAL '1 hour')
            ORDER BY created_at ASC
        """), {"gap": session_gap_hours})
        rows = result.fetchall()

    if not rows:
        return {"active": False, "message_count": 0, "session_gap_hours": session_gap_hours}

    from datetime import timezone

    reset_before_str = await mem.get_setting("session_reset_before")
    if reset_before_str:
        last_ts = rows[-1].created_at
        reset_before = datetime.fromisoformat(reset_before_str)
        if reset_before.tzinfo is None:
            reset_before = reset_before.replace(tzinfo=timezone.utc)
        last_ts_aware = last_ts.replace(tzinfo=timezone.utc) if last_ts.tzinfo is None else last_ts
        if last_ts_aware <= reset_before:
            return {"active": False, "message_count": 0, "session_gap_hours": session_gap_hours}

    # Find where the current session actually started by walking backwards
    # to the last gap > session_gap_hours.
    session_start_idx = 0
    for i in range(len(rows) - 1, 0, -1):
        gap_h = (rows[i].created_at - rows[i - 1].created_at).total_seconds() / 3600
        if gap_h > session_gap_hours:
            session_start_idx = i
            break
    session_rows = rows[session_start_idx:]

    first = session_rows[0].created_at
    last = session_rows[-1].created_at
    first_aware = first.replace(tzinfo=timezone.utc) if first.tzinfo is None else first
    age_minutes = (datetime.now(timezone.utc) - first_aware).total_seconds() / 60
    return {
        "active": True,
        "message_count": len(session_rows),
        "first_message_at": first.isoformat(),
        "last_message_at": last.isoformat(),
        "age_minutes": round(age_minutes, 1),
        "session_gap_hours": session_gap_hours,
    }


@app.post("/admin/session/reset")
async def admin_reset_session(_=Depends(_require_desktop_auth)):
    """Force a new session by marking all current messages as pre-reset."""
    async with SessionLocal() as db:
        result = await db.execute(sa.text("SELECT MAX(created_at) FROM conversations"))
        max_ts = result.scalar()
    if max_ts:
        from datetime import timezone
        max_ts_aware = max_ts.replace(tzinfo=timezone.utc) if max_ts.tzinfo is None else max_ts
        await mem.set_setting("session_reset_before", max_ts_aware.isoformat())
    return {"ok": True}


@app.put("/admin/settings")
async def admin_put_settings(body: dict, _=Depends(_require_desktop_auth)):
    """Update editable runtime settings."""
    if "image_gen_model" in body:
        val = str(body["image_gen_model"]).strip()
        if not val:
            raise HTTPException(status_code=400, detail="image_gen_model cannot be empty")
        await mem.set_setting("image_gen_model", val)
    return {"ok": True}


@app.get("/admin/prompts")
async def admin_get_prompts(_=Depends(_require_desktop_auth)):
    """Return the current identity and instructions prompt content."""
    result = {}
    for name in _ALLOWED_PROMPTS:
        result[name] = await mem.get_prompt(name) or ""
    return result


@app.get("/admin/prompts/heartbeat")
async def admin_get_heartbeat_prompt(_=Depends(_require_desktop_auth)):
    """Return the heartbeat synthesis prompt (read-only, sourced from file)."""
    path = PROMPTS_DIR / "heartbeat.md"
    if not path.exists():
        raise HTTPException(status_code=404, detail="heartbeat.md not found")
    return {"content": path.read_text(encoding="utf-8").strip()}


@app.put("/admin/prompts/{name}")
async def admin_put_prompt(name: str, body: dict, _=Depends(_require_desktop_auth)):
    """Overwrite a prompt in the DB."""
    if name not in _ALLOWED_PROMPTS:
        raise HTTPException(status_code=404, detail=f"Unknown prompt '{name}'")
    content = body.get("content", "")
    if not isinstance(content, str) or not content.strip():
        raise HTTPException(status_code=400, detail="content cannot be empty")
    await mem.set_prompt(name, content)
    print(f"[prompts] {name} updated in DB ({len(content)} chars)")
    return {"ok": True}


@app.get("/admin/vault-git")
async def admin_vault_git(args: str, _=Depends(_require_desktop_auth)):
    """Run a read-only git command against the vault. Pass args as space-separated string.
    e.g. ?args=log --all --full-history --oneline -- *filename*
    Returns stdout as plain text.
    """
    import asyncio, subprocess
    ALLOWED = ["log", "show", "ls-tree", "ls-files", "diff", "status", "cat-file", "rev-list", "branch", "tag"]
    arg_list = args.split(" ", 1)
    sub = arg_list[0] if arg_list else ""
    if sub not in ALLOWED:
        raise HTTPException(status_code=403, detail=f"Subcommand '{sub}' not allowed. Allowed: {ALLOWED}")
    from .tools.obsidian import VAULT, GIT_ENV
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            ["git", "-C", str(VAULT)] + args.split(" "),
            capture_output=True, text=True, timeout=30, env=GIT_ENV,
        )
        return {"stdout": result.stdout, "stderr": result.stderr, "returncode": result.returncode}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/admin/vault-recover")
async def admin_vault_recover(body: dict, _=Depends(_require_desktop_auth)):
    """Recover a deleted file from vault git history.
    POST body: {"commit": "<hash>", "vault_path": "path/to/file.md"}
    Gets the file content at that commit and writes/commits/pushes it back.
    """
    import asyncio, subprocess
    from .tools.obsidian import VAULT, GIT_ENV, _git, _safe_path

    commit = body.get("commit", "").strip()
    vault_path = body.get("vault_path", "").strip()
    if not commit or not vault_path:
        raise HTTPException(status_code=400, detail="commit and vault_path are required")

    # Get file content from history
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            ["git", "-C", str(VAULT), "show", f"{commit}:{vault_path}"],
            capture_output=True, text=True, timeout=30, env=GIT_ENV,
        )
        if result.returncode != 0:
            raise HTTPException(status_code=404, detail=f"git show failed: {result.stderr.strip()}")
        file_content = result.stdout
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    # Write and push
    try:
        path = _safe_path(vault_path)
        await _git("pull", "--quiet", "--autostash")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(file_content)
        await _git("add", vault_path)
        await _git("commit", "-m", f"restore: {vault_path} from git history")
        await _git("push")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Restore failed: {e}")

    return {"ok": True, "restored": vault_path, "content": file_content}


_LOG_SERVICE_MAP = {
    "browser":       "ki-api-1",        # browser tool runs in api container
    "coding-agent":  "ki-coding-agent-1",
    "api":           "ki-api-1",
    "baileys":       "ki-baileys-1",
}

@app.get("/admin/logs/{service}")
async def admin_logs(service: str, lines: int = 200, _=Depends(_require_desktop_auth)):
    """Return the last N lines of Docker logs for a given service."""
    container = _LOG_SERVICE_MAP.get(service)
    if not container:
        raise HTTPException(status_code=400, detail=f"Unknown service '{service}'. Allowed: {list(_LOG_SERVICE_MAP)}")
    if not os.path.exists("/var/run/docker.sock"):
        return {"service": service, "container": container,
                "lines": f"[Logs aren't available here: the api container has no Docker socket. "
                         f"Run `docker compose logs {service}` on the host instead.]\n"}
    try:
        import docker as _docker
        import asyncio as _asyncio

        def _fetch_logs():
            client = _docker.from_env()
            try:
                c = client.containers.get(container)
                raw = c.logs(tail=max(1, min(lines, 2000)), stdout=True, stderr=True)
                return raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
            except _docker.errors.NotFound:
                return f"[Container '{container}' not found]\n"
            except _docker.errors.APIError as exc:
                msg = str(exc)
                if "does not support reading" in msg:
                    return "[Logs unavailable for this container (logging driver does not support reading). Check the host syslog or configured log destination instead.]\n"
                return f"[Docker API error: {msg}]\n"
            finally:
                client.close()

        combined = await _asyncio.to_thread(_fetch_logs)
        return {"service": service, "container": container, "lines": combined}
    except Exception as e:
        return {"service": service, "container": container, "lines": f"[Log fetch failed: {e}]\n"}


@app.get("/auth/google")
async def auth_google():
    """Redirect to Google OAuth consent screen."""
    import urllib.parse
    client_id = os.environ.get("GOOGLE_CLIENT_ID", "")
    redirect_uri = os.environ.get("GOOGLE_REDIRECT_URI", "")
    if not client_id or not redirect_uri:
        raise HTTPException(status_code=500, detail="Google OAuth not configured (GOOGLE_CLIENT_ID / GOOGLE_REDIRECT_URI missing)")
    scopes = " ".join([
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/gmail.compose",
        "https://www.googleapis.com/auth/gmail.modify",
        "https://www.googleapis.com/auth/drive.readonly",
        "https://www.googleapis.com/auth/drive.file",
        "https://www.googleapis.com/auth/documents",
        "https://www.googleapis.com/auth/calendar",
        "openid",
        "email",
    ])
    params = urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": scopes,
        "access_type": "offline",
        "prompt": "consent",
    })
    return RedirectResponse(f"https://accounts.google.com/o/oauth2/v2/auth?{params}")


@app.get("/auth/google/callback")
async def auth_google_callback(code: str | None = None, error: str | None = None):
    """Handle Google OAuth callback — exchange code for tokens and store them."""
    import httpx as _httpx

    if error:
        return HTMLResponse(
            f'<p>Google OAuth error: {error}</p><p><a href="/app/">Back to Ki</a></p>',
            status_code=400,
        )
    if not code:
        raise HTTPException(status_code=400, detail="Missing authorization code")

    client_id = os.environ.get("GOOGLE_CLIENT_ID", "")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "")
    redirect_uri = os.environ.get("GOOGLE_REDIRECT_URI", "")

    # Exchange authorization code for tokens
    try:
        async with _httpx.AsyncClient(timeout=15.0) as c:
            r = await c.post("https://oauth2.googleapis.com/token", data={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            })
            r.raise_for_status()
            token_data = r.json()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Token exchange failed: {e}")

    access_token = token_data.get("access_token", "")
    refresh_token = token_data.get("refresh_token", "")
    if not access_token:
        raise HTTPException(status_code=500, detail="No access_token in Google response")

    expires_in = token_data.get("expires_in", 3600)
    expiry = datetime.now(timezone.utc) + timedelta(seconds=expires_in - 60)

    # Fetch connected email address
    google_email = None
    try:
        async with _httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(
                "https://www.googleapis.com/oauth2/v1/userinfo",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if r.status_code == 200:
                google_email = r.json().get("email")
    except Exception as e:
        print(f"[google] userinfo fetch failed (non-fatal): {e}")

    from .integrations.google import _save_tokens as _google_save_tokens
    await _google_save_tokens(access_token, refresh_token, expiry, google_email)
    print(f"[google] authenticated: {google_email}")

    return RedirectResponse("/app/")


@app.get("/auth/google/status")
async def auth_google_status(_=Depends(_require_desktop_auth)):
    """Return whether Google is connected and which email is linked."""
    from .integrations.google import _get_token_row
    row = await _get_token_row()
    if row is None:
        return {"connected": False, "email": None}
    return {"connected": True, "email": row["email"]}


@app.post("/auth/google/disconnect")
async def auth_google_disconnect(_=Depends(_require_desktop_auth)):
    """Delete stored Google tokens (disconnect)."""
    async with SessionLocal() as session:
        async with session.begin():
            await session.execute(sa.text("DELETE FROM google_tokens"))
    print("[google] disconnected")
    return {"ok": True}


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/usage")
async def usage(_=Depends(_require_desktop_auth)):
    """Token usage and estimated cost, broken down by 3d / 7d / 30d periods."""
    # Costs per 1M tokens (input/cache_write/cache_read/output) + optional per_request flat rate
    COSTS = {
        "claude-sonnet-4-6":         {"input": 3.00,  "cache_write": 3.75, "cache_read": 0.30, "output": 15.00},
        "claude-haiku-4-5-20251001": {"input": 0.80,  "cache_write": 1.00, "cache_read": 0.08, "output":  4.00},
        "text-embedding-3-small":    {"input": 0.02,  "cache_write": 0,    "cache_read": 0,    "output":  0},
        "whisper-1":                 {"input": 0,     "cache_write": 0,    "cache_read": 0,    "output":  0,    "per_request": 0.006},
        "grok-imagine-image":         {"input": 0,     "cache_write": 0,    "cache_read": 0,    "output":  0,    "per_request": 0.07},
        "xai-tts-ara":                {"input": 4.20,  "cache_write": 0,    "cache_read": 0,    "output":  0},
    }

    def calc_cost(row: dict, rates: dict) -> float:
        token_cost = (
            row["input_tokens"]           * rates.get("input", 0)       +
            row["cache_creation_tokens"]  * rates.get("cache_write", 0) +
            row["cache_read_tokens"]      * rates.get("cache_read", 0)  +
            row["output_tokens"]          * rates.get("output", 0)
        ) / 1_000_000
        return token_cost + rates.get("per_request", 0) * row["requests"]

    periods = {"3d": "3 days", "7d": "7 days", "30d": "30 days"}
    result_out: dict = {}

    async with SessionLocal() as session:
        for key, interval in periods.items():
            # By model
            result = await session.execute(sa.text(f"""
                SELECT
                    model,
                    SUM(input_tokens)           AS input_tokens,
                    SUM(output_tokens)          AS output_tokens,
                    SUM(cache_creation_tokens)  AS cache_creation_tokens,
                    SUM(cache_read_tokens)      AS cache_read_tokens,
                    COUNT(*)                    AS requests
                FROM token_usage
                WHERE created_at > now() - INTERVAL '{interval}'
                GROUP BY model
                ORDER BY model
            """))
            model_rows = []
            for row in result.mappings().all():
                rates = COSTS.get(row["model"], {"input": 0, "cache_write": 0, "cache_read": 0, "output": 0})
                cost = calc_cost(dict(row), rates)
                model_rows.append({
                    "model":                  row["model"],
                    "input_tokens":           row["input_tokens"],
                    "output_tokens":          row["output_tokens"],
                    "cache_creation_tokens":  row["cache_creation_tokens"],
                    "cache_read_tokens":      row["cache_read_tokens"],
                    "requests":               row["requests"],
                    "estimated_cost_usd":     round(cost, 4),
                })

            # By use_case
            uc_result = await session.execute(sa.text(f"""
                SELECT
                    use_case,
                    model,
                    SUM(input_tokens)           AS input_tokens,
                    SUM(output_tokens)          AS output_tokens,
                    SUM(cache_creation_tokens)  AS cache_creation_tokens,
                    SUM(cache_read_tokens)      AS cache_read_tokens,
                    COUNT(*)                    AS requests
                FROM token_usage
                WHERE created_at > now() - INTERVAL '{interval}'
                GROUP BY use_case, model
                ORDER BY use_case, model
            """))
            # Aggregate cost per use_case across models
            uc_cost: dict = {}
            for row in uc_result.mappings().all():
                uc = row["use_case"]
                rates = COSTS.get(row["model"], {"input": 0, "cache_write": 0, "cache_read": 0, "output": 0})
                cost = calc_cost(dict(row), rates)
                if uc not in uc_cost:
                    uc_cost[uc] = {"use_case": uc, "estimated_cost_usd": 0.0, "requests": 0}
                uc_cost[uc]["estimated_cost_usd"] += cost
                uc_cost[uc]["requests"] += row["requests"]
            use_case_rows = sorted(
                [{"use_case": k, "estimated_cost_usd": round(v["estimated_cost_usd"], 4), "requests": v["requests"]}
                 for k, v in uc_cost.items()],
                key=lambda r: r["estimated_cost_usd"], reverse=True,
            )

            totals = {
                "cost":                   round(sum(r["estimated_cost_usd"]      for r in model_rows), 4),
                "input_tokens":           sum(r["input_tokens"]           for r in model_rows),
                "output_tokens":          sum(r["output_tokens"]          for r in model_rows),
                "cache_creation_tokens":  sum(r["cache_creation_tokens"]  for r in model_rows),
                "cache_read_tokens":      sum(r["cache_read_tokens"]      for r in model_rows),
                "requests":               sum(r["requests"]               for r in model_rows),
            }
            result_out[key] = {"rows": model_rows, "use_case_rows": use_case_rows, "totals": totals}

    _user_tz_name = (os.environ.get("USER_TZ") or "UTC")
    # Daily breakdown for the last 30 days (user's local timezone)
    daily_rows: list[dict] = []
    async with SessionLocal() as session:
        day_result = await session.execute(sa.text(f"""
            SELECT
                DATE(created_at AT TIME ZONE '{_user_tz_name}') AS day,
                model,
                SUM(input_tokens)           AS input_tokens,
                SUM(output_tokens)          AS output_tokens,
                SUM(cache_creation_tokens)  AS cache_creation_tokens,
                SUM(cache_read_tokens)      AS cache_read_tokens,
                COUNT(*)                    AS requests
            FROM token_usage
            WHERE created_at > now() - INTERVAL '30 days'
            GROUP BY DATE(created_at AT TIME ZONE '{_user_tz_name}'), model
            ORDER BY day DESC, model
        """))
        # Aggregate per day across models (cost needs per-model rates)
        day_agg: dict = {}
        for row in day_result.mappings().all():
            day_str = str(row["day"])
            rates = COSTS.get(row["model"], {"input": 0, "cache_write": 0, "cache_read": 0, "output": 0})
            cost = calc_cost(dict(row), rates)
            if day_str not in day_agg:
                day_agg[day_str] = {"date": day_str, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "cost": 0.0}
            day_agg[day_str]["input_tokens"]  += row["input_tokens"]
            day_agg[day_str]["output_tokens"] += row["output_tokens"]
            day_agg[day_str]["total_tokens"]  += (
                row["input_tokens"] + row["cache_creation_tokens"] +
                row["cache_read_tokens"] + row["output_tokens"]
            )
            day_agg[day_str]["cost"] += cost
        daily_rows = [
            {**v, "cost": round(v["cost"], 4)}
            for v in sorted(day_agg.values(), key=lambda r: r["date"], reverse=True)
        ]

    # Keep last_30_days for backwards compat
    return {"periods": result_out, "last_30_days": result_out["30d"]["rows"], "daily": daily_rows}


@app.get("/")
async def root():
    # A self-hosted instance opens straight into the web app. The project's
    # landing page is still available at /about.
    return RedirectResponse("/app/")


@app.get("/about")
async def marketing_index():
    return FileResponse(WEB_DIR / "marketing" / "index.html")


@app.get("/apple-touch-icon.png")
@app.get("/apple-touch-icon-precomposed.png")
@app.head("/apple-touch-icon.png")
@app.head("/apple-touch-icon-precomposed.png")
async def apple_touch_icon():
    return FileResponse(
        WEB_DIR / "icon.png",
        media_type="image/png",
        headers={"Cache-Control": "no-cache"},
    )


# Mount the Ki app at /app — must come after all explicit API routes so they take precedence.
app.mount("/app", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
