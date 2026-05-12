"""
Integration auto-discovery.

Scans the integrations/ directory for modules that expose the standard interface:
  REQUIRED_ENV_VARS: list[str]        -- env vars needed; integration skipped if any missing
  TOOL_SCHEMAS: list[dict]            -- Anthropic tool schema dicts
  SCHEDULES: list[dict]               -- scheduled jobs (optional)
  async def run_tool(name, input)     -- tool dispatcher

Files in _SKIP are treated as pure libraries and not loaded as integrations.
"""
import importlib
import os
from pathlib import Path

# loader.py itself must be excluded — everything else is caught by the _ prefix convention
_SKIP = {"loader"}


def load_integrations() -> list:
    """Return list of loaded integration modules, skipping any with missing env vars."""
    integrations_dir = Path(__file__).parent
    modules = []

    for path in sorted(integrations_dir.glob("*.py")):
        name = path.stem
        if name.startswith("_") or name in _SKIP:
            continue

        try:
            mod = importlib.import_module(f".{name}", package="src.integrations")
        except Exception as e:
            print(f"[integrations] {name}: import failed — {e}")
            continue

        if not hasattr(mod, "TOOL_SCHEMAS"):
            continue  # not a standard integration module

        missing = [v for v in getattr(mod, "REQUIRED_ENV_VARS", []) if not os.environ.get(v)]
        if missing:
            print(f"[integrations] {name}: skipping — missing env vars: {', '.join(missing)}")
            continue

        n_tools = len(mod.TOOL_SCHEMAS)
        n_schedules = len(getattr(mod, "SCHEDULES", []))
        print(f"[integrations] loaded {name}: {n_tools} tool(s), {n_schedules} schedule(s)")
        modules.append(mod)

    return modules
