"""
Web search integration — Brave Search API.

Exposes a single conversation tool: web_search.
Use when the user asks about current events, specific websites, prices, news,
or anything that requires up-to-date information from the internet.

Requires: BRAVE_SEARCH_API_KEY
"""
import os

import httpx

REQUIRED_ENV_VARS = ["BRAVE_SEARCH_API_KEY"]

TOOL_SCHEMAS = [
    {
        "name": "web_search",
        "description": (
            "Search the web using Brave Search. Use this when you need current information, "
            "facts you're not certain about, news, prices, or anything that requires looking "
            "something up online. Always prefer searching over guessing URLs or making things up. "
            "Returns a list of results with titles, URLs, and descriptions."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query. Be specific and concise.",
                },
                "count": {
                    "type": "integer",
                    "description": "Number of results to return (1–10, default 5).",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
    }
]

SCHEDULES: list[dict] = []

_API_KEY = os.environ.get("BRAVE_SEARCH_API_KEY", "")
_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"


# ── Health check ──────────────────────────────────────────────────────────────

async def health_check() -> tuple[bool, str]:
    """
    Lightweight self-test: perform a minimal search query and expect a 200.
    Returns (ok: bool, detail: str).
    """
    if not _API_KEY:
        return False, "BRAVE_SEARCH_API_KEY not set"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                _SEARCH_URL,
                headers={
                    "Accept": "application/json",
                    "X-Subscription-Token": _API_KEY,
                },
                params={"q": "test", "count": 1},
            )
            r.raise_for_status()
        return True, "ok"
    except httpx.HTTPStatusError as e:
        return False, f"HTTP {e.response.status_code}"
    except Exception as e:
        return False, str(e)


async def run_tool(name: str, input: dict) -> str:
    if name != "web_search":
        return f"Unknown web_search tool: {name}"

    query = input["query"]
    count = min(max(int(input.get("count", 5)), 1), 10)

    print(f"[web_search] query={query!r} count={count}")

    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "X-Subscription-Token": _API_KEY,
    }
    params = {"q": query, "count": count, "safesearch": "moderate"}

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(_SEARCH_URL, headers=headers, params=params)
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPStatusError as e:
        print(f"[web_search] HTTP error: {e.response.status_code} {e.response.text[:200]}")
        return f"Search failed: HTTP {e.response.status_code}"
    except Exception as e:
        print(f"[web_search] error: {e}")
        return f"Search failed: {e}"

    web_results = data.get("web", {}).get("results", [])
    if not web_results:
        return "No results found."

    lines = []
    for i, result in enumerate(web_results, 1):
        title = result.get("title", "(no title)")
        url = result.get("url", "")
        desc = result.get("description", "").strip()
        lines.append(f"{i}. {title}\n   {url}\n   {desc}")

    return "\n\n".join(lines)
