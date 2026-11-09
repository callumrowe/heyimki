"""
Apply the coding agent's work to the live checkout, once the user confirms.

The coding agent works in its own clone and can't write this checkout or reach
Docker. Deploying goes:

  1. prepare(): fetch its undeployed commits as a git bundle on top of the
     checkout's HEAD, and describe what would change.
  2. The user confirms: the deploy_code pending action (actions.py), or the
     /deploy command.
  3. apply(): fast-forward the checkout. Then, depending on the files:
       - web app / prompts / baileys src: live straight away
       - api/src: check the new code imports, then restart the api. The api
         exits and Docker's restart policy starts it again on the new code.
         If it then fails to start, deploy_guard.py (the container entrypoint)
         rolls back after a few attempts.
       - Dockerfiles, dependencies, compose, migrations, the coding agent:
         need a rebuild, which only the user can run.

The checkout is bind-mounted at /repo. Git runs as the checkout's owner, so
files on the host keep their ownership.
"""
import asyncio
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import httpx

REPO = Path("/repo")
STATE = Path("/var/lib/ki-deploy")      # inside the container: survives restarts
PENDING = STATE / "pending.json"         # written before a restart, cleared on startup
ROLLED_BACK = STATE / "rolled-back.json"  # written by deploy_guard.py
PREPARED = STATE / "prepared.json"       # the deploy the user is being asked about

CODING_AGENT_URL = os.environ.get("CODING_AGENT_URL", "http://coding-agent:3002")
CODING_AGENT_HEADERS = {"Authorization": f"Bearer {os.environ.get('CODING_AGENT_SECRET', '')}"}

_LIVE = ("api/web/", "api/prompts/", "baileys/src/")
_RESTART = ("api/src/",)
_REBUILD = (
    "api/Dockerfile", "api/requirements.txt", "api/deploy_guard.py", "api/migrations/",
    "baileys/Dockerfile", "baileys/package.json", "baileys/package-lock.json",
    "coding-agent/", "docker-compose.yml", "deploy/",
)
# Files that change how Ki runs on the host. Flagged in the confirmation.
_SENSITIVE = ("docker-compose.yml", "Dockerfile", "entrypoint.sh", "deploy_guard.py",
              "setup.sh", "bin/", "scripts/", "deploy/", ".github/")


class DeployError(Exception):
    pass


# ── git ───────────────────────────────────────────────────────────────────────

def _owner_kwargs() -> dict:
    """Run git as the checkout's owner, so files on the host keep their owner."""
    st = REPO.stat()
    kwargs: dict = {"env": {**os.environ, "HOME": "/tmp"}}
    if os.geteuid() == 0 and st.st_uid != 0:
        kwargs.update(user=st.st_uid, group=st.st_gid, extra_groups=[])
    return kwargs


def _git_sync(*args: str, timeout: int = 60, keep_indent: bool = False) -> str:
    r = subprocess.run(
        ["git", "-C", str(REPO), *args],
        capture_output=True, text=True, timeout=timeout, **_owner_kwargs(),
    )
    if r.returncode != 0:
        raise DeployError((r.stderr or r.stdout).strip() or f"git {args[0]} failed")
    return r.stdout.rstrip() if keep_indent else r.stdout.strip()


async def _git(*args: str, timeout: int = 60, keep_indent: bool = False) -> str:
    return await asyncio.to_thread(_git_sync, *args, timeout=timeout, keep_indent=keep_indent)


# ── what a change needs ───────────────────────────────────────────────────────

def classify(files: list[str]) -> str:
    """'rebuild', 'restart', 'live' or 'none' (docs and other files with no runtime effect)."""
    if any(f.startswith(_REBUILD) or f.endswith("Dockerfile") for f in files):
        return "rebuild"
    if any(f.startswith(_RESTART) for f in files):
        return "restart"
    if any(f.startswith(_LIVE) for f in files):
        return "live"
    return "none"


_AFTER = {
    "live": "Live as soon as it's deployed.",
    "restart": "Deploying restarts Ki's api (back in about 10 seconds).",
    "rebuild": "This needs a rebuild to take effect. After deploying, run `docker compose up -d --build` on the host.",
    "none": "Nothing needs restarting.",
}


def describe(prep: dict) -> str:
    lines = [f"Deploy {len(prep['commits'])} change(s) from the coding agent:"]
    lines += [f"  • {c}" for c in prep["commits"]]
    lines.append("")
    lines.append(prep["stat"])
    sensitive = [f for f in prep["files"] if any(s in f for s in _SENSITIVE)]
    if sensitive:
        lines.append("")
        lines.append("⚠️ These files change how Ki runs on your machine. Review them before you rebuild or run them: "
                     + ", ".join(sensitive))
    lines.append("")
    lines.append(_AFTER[prep["kind"]])
    return "\n".join(lines)


# ── deploy ────────────────────────────────────────────────────────────────────

async def prepare() -> dict | None:
    """Fetch the coding agent's undeployed work. None when there's nothing to deploy."""
    if not (REPO / ".git").exists():
        raise DeployError("This copy of Ki isn't a git checkout, so there's nothing to deploy into.")
    base = await _git("rev-parse", "HEAD")
    async with httpx.AsyncClient(timeout=120.0, headers=CODING_AGENT_HEADERS) as c:
        r = await c.post(f"{CODING_AGENT_URL}/bundle", json={"base": base})
    if r.status_code == 204:
        return None
    if r.status_code != 200:
        try:
            detail = r.json().get("error", r.text)
        except Exception:
            detail = r.text
        raise DeployError(f"The coding agent couldn't hand over its changes: {detail}")

    STATE.mkdir(parents=True, exist_ok=True)
    bundle = STATE / "incoming.bundle"
    bundle.write_bytes(r.content)
    os.chmod(bundle, 0o644)  # git runs as the checkout's owner
    await _git("bundle", "verify", "-q", str(bundle))
    await _git("fetch", "--quiet", "--no-tags", str(bundle), "ki/work")
    head = await _git("rev-parse", "FETCH_HEAD")
    try:
        await _git("merge-base", "--is-ancestor", base, head)
    except DeployError:
        raise DeployError("The coding agent's changes aren't based on the current code. Ask it to try again.")

    files = (await _git("diff", "--name-only", base, head)).splitlines()
    prep = {
        "base": base,
        "head": head,
        "files": files,
        "commits": (await _git("log", "--format=%h %s", "--reverse", f"{base}..{head}")).splitlines(),
        "stat": await _git("diff", "--stat", base, head, keep_indent=True),
        "kind": classify(files),
    }
    # apply() only runs the deploy that was last shown to the user.
    PREPARED.write_text(json.dumps(prep))
    return prep


async def _import_check() -> str | None:
    """Import the api's code in a fresh process. Returns the error, or None."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import src.main",
        cwd="/app", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
    except asyncio.TimeoutError:
        proc.kill()
        return "importing the new code timed out after 2 minutes"
    if proc.returncode == 0:
        return None
    return "\n".join(out.decode(errors="replace").strip().splitlines()[-15:])


async def apply(base: str, head: str) -> str:
    """Fast-forward the live checkout to `head` and make it take effect."""
    try:
        prep = json.loads(PREPARED.read_text())
    except (OSError, ValueError):
        prep = None
    if not prep or prep["head"] != head or prep["base"] != base:
        return "That deploy is out of date. Ask me to deploy again and I'll show you the current changes."
    current = await _git("rev-parse", "HEAD")
    if current != base:
        PREPARED.unlink(missing_ok=True)
        return "The code changed since I asked, so I didn't deploy. Ask me to deploy again."
    try:
        await _git("merge", "--ff-only", "--quiet", head)
    except DeployError as e:
        return f"Couldn't deploy: {e}"
    PREPARED.unlink(missing_ok=True)
    short = head[:7]
    kind = prep["kind"]

    if kind == "restart":
        error = await _import_check()
        if error:
            await _git("reset", "--keep", base)
            return (f"Didn't deploy {short}: the new code fails to load, so I put the old code back. "
                    f"The changes are still in the coding agent's workspace.\n\n{error}")
        STATE.mkdir(parents=True, exist_ok=True)
        PENDING.write_text(json.dumps({"base": base, "head": head, "commits": prep["commits"]}))
        asyncio.create_task(_restart_when_idle())
        return f"Deployed {short}. Restarting the api to load it. I'll be back in about 10 seconds."
    if kind == "rebuild":
        return (f"Deployed {short} into the checkout. It takes effect after a rebuild: "
                "run `docker compose up -d --build` on the host.")
    if kind == "live":
        return f"Deployed {short}. It's live now."
    return f"Deployed {short}. Nothing needed restarting."


async def discard() -> str:
    async with httpx.AsyncClient(timeout=60.0, headers=CODING_AGENT_HEADERS) as c:
        r = await c.post(f"{CODING_AGENT_URL}/discard", json={})
    data = r.json()
    if r.status_code != 200:
        return f"Couldn't discard: {data.get('error', r.text)}"
    n = data.get("discarded_commits", 0)
    return f"Discarded {n} undeployed change(s). The coding agent starts fresh from the live code."


# ── restart ───────────────────────────────────────────────────────────────────

_inflight = 0


class InflightCounter:
    """ASGI middleware that counts open requests, so a restart can wait for the
    reply that announced it to finish streaming."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        global _inflight
        if scope["type"] != "http" or scope.get("path") == "/health":
            return await self.app(scope, receive, send)
        _inflight += 1
        try:
            await self.app(scope, receive, send)
        finally:
            _inflight -= 1


async def _restart_when_idle(max_wait: float = 90.0) -> None:
    await asyncio.sleep(2)
    waited = 2.0
    while _inflight > 0 and waited < max_wait:
        await asyncio.sleep(0.5)
        waited += 0.5
    print("[deploy] restarting the api to load the new code")
    # uvicorn shuts down cleanly and exits; the restart policy starts it again.
    os.kill(os.getpid(), signal.SIGTERM)


async def on_startup() -> None:
    """Report how the last deploy went (called once the api has started)."""
    from .alerts import send_alert
    try:
        if ROLLED_BACK.exists():
            info = json.loads(ROLLED_BACK.read_text())
            ROLLED_BACK.unlink()
            how = ("I rolled back to the previous code" if info.get("rollback_ok")
                   else f"rolling back failed too ({info.get('rollback_output', '')}), so check the checkout by hand")
            await send_alert(
                f"⚠️ The deploy of {info['head'][:7]} kept failing to start, so {how}. "
                "The changes are still with the coding agent and would be included in the next deploy: "
                "ask me to fix them, or to discard them."
            )
        elif PENDING.exists():
            info = json.loads(PENDING.read_text())
            PENDING.unlink()
            (STATE / "attempts").unlink(missing_ok=True)
            summary = "; ".join(c.split(" ", 1)[-1] for c in info.get("commits", []))
            await send_alert(f"✅ Deployed {info['head'][:7]}. I'm running the new code: {summary}")
    except Exception as e:
        print(f"[deploy] couldn't report the last deploy: {e}")
