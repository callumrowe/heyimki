"""
GitHub integration — read-only access to Ki's own repository.

Exposes two conversation tools:
  - read_github_file: fetch and decode the contents of any file in the repo
  - list_github_files: list the contents of a directory in the repo

Requires:
  GITHUB_TOKEN — personal access token with repo read access
  GITHUB_REPO  — owner/repo format, e.g. "your-username/ki"
"""
import base64
import os

import httpx

REQUIRED_ENV_VARS = ["GITHUB_TOKEN", "GITHUB_REPO"]

TOOL_SCHEMAS = [
    {
        "name": "read_github_file",
        "description": (
            "Read the contents of a file from Ki's own GitHub repository. "
            "Use this to inspect source code, configuration files, prompts, migrations, "
            "or any other file in the repo. "
            "The path is relative to the repo root — e.g. 'api/src/agent.py', "
            "'api/prompts/identity.md', 'docker-compose.yml'. "
            "Returns the decoded file contents as a string."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Relative file path from the repo root. "
                        "Examples: 'api/src/agent.py', 'CLAUDE.md', "
                        "'api/src/integrations/time_tools.py', 'docker-compose.yml'"
                    ),
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "list_github_files",
        "description": (
            "List the files and directories at a path in Ki's GitHub repository. "
            "Use this to explore the repo structure before reading specific files. "
            "Defaults to the root of the repository if no path is given. "
            "Returns names and types (file or dir) for each entry."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Directory path relative to the repo root. "
                        "Leave empty or omit to list the root directory. "
                        "Examples: 'api/src', 'api/src/integrations', 'api/migrations'"
                    ),
                },
            },
            "required": [],
        },
    },
]

SCHEDULES: list[dict] = []

_API_BASE = "https://api.github.com"


def _headers() -> dict:
    token = os.environ.get("GITHUB_TOKEN", "")
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _repo() -> str:
    return os.environ.get("GITHUB_REPO", "")


# ── Health check ──────────────────────────────────────────────────────────────

async def health_check() -> tuple[bool, str]:
    """
    Lightweight self-test: hit GET /repos/{owner}/{repo} and expect 200.
    Returns (ok: bool, detail: str).
    """
    repo = _repo()
    if not repo:
        return False, "GITHUB_REPO not set"
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        return False, "GITHUB_TOKEN not set"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"{_API_BASE}/repos/{repo}",
                headers=_headers(),
            )
            r.raise_for_status()
        return True, "ok"
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            return False, "GITHUB_TOKEN is invalid or expired"
        if e.response.status_code == 404:
            return False, f"Repository '{repo}' not found or token lacks access"
        return False, f"HTTP {e.response.status_code}"
    except Exception as e:
        return False, str(e)


# ── Tool implementations ──────────────────────────────────────────────────────

async def _read_github_file(path: str) -> str:
    """Fetch a single file from the GitHub repo and return its decoded contents."""
    repo = _repo()
    # Strip leading slash if present
    path = path.lstrip("/")
    url = f"{_API_BASE}/repos/{repo}/contents/{path}"

    print(f"[github] read_github_file: {repo}/{path}")

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.get(url, headers=_headers())

            if r.status_code == 401:
                return "Error: GitHub authentication failed — GITHUB_TOKEN may be invalid or expired."
            if r.status_code == 403:
                return "Error: Access forbidden — token may lack read access to this repository."
            if r.status_code == 404:
                return f"Error: File not found — '{path}' does not exist in {repo}."

            r.raise_for_status()
            data = r.json()

    except httpx.HTTPStatusError as e:
        return f"Error: GitHub API returned HTTP {e.response.status_code} for '{path}'."
    except Exception as e:
        return f"Error: Failed to fetch '{path}' from GitHub — {e}"

    # The API returns a list when the path is a directory
    if isinstance(data, list):
        return (
            f"Error: '{path}' is a directory, not a file. "
            "Use list_github_files to explore directories."
        )

    content_type = data.get("type")
    if content_type != "file":
        return f"Error: '{path}' is a {content_type!r}, not a file."

    encoding = data.get("encoding", "")
    raw_content = data.get("content", "")

    if encoding == "base64":
        try:
            # GitHub includes newlines in the base64 blob — strip them before decoding
            decoded = base64.b64decode(raw_content.replace("\n", "")).decode("utf-8")
        except Exception as e:
            return f"Error: Could not decode file contents — {e}"
    else:
        # Fallback: return raw content as-is
        decoded = raw_content

    size = data.get("size", 0)
    sha = data.get("sha", "")[:7]
    print(f"[github] read_github_file: got {size} bytes (sha {sha})")
    return decoded


async def _list_github_files(path: str = "") -> str:
    """List contents of a directory in the GitHub repo."""
    repo = _repo()
    path = path.lstrip("/").rstrip("/")
    url = f"{_API_BASE}/repos/{repo}/contents/{path}" if path else f"{_API_BASE}/repos/{repo}/contents"

    print(f"[github] list_github_files: {repo}/{path or '(root)'}")

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.get(url, headers=_headers())

            if r.status_code == 401:
                return "Error: GitHub authentication failed — GITHUB_TOKEN may be invalid or expired."
            if r.status_code == 403:
                return "Error: Access forbidden — token may lack read access to this repository."
            if r.status_code == 404:
                location = f"'{path}'" if path else "the repository root"
                return f"Error: Path not found — {location} does not exist in {repo}."

            r.raise_for_status()
            data = r.json()

    except httpx.HTTPStatusError as e:
        return f"Error: GitHub API returned HTTP {e.response.status_code}."
    except Exception as e:
        return f"Error: Failed to list '{path or '/'}' from GitHub — {e}"

    # If the API returned a single object instead of a list, the path is a file
    if isinstance(data, dict):
        return (
            f"Error: '{path}' is a file, not a directory. "
            "Use read_github_file to read its contents."
        )

    if not data:
        location = f"'{path}'" if path else "the repository root"
        return f"No files found at {location}."

    # Sort: directories first, then files — both alphabetically
    dirs  = sorted([e for e in data if e.get("type") == "dir"],  key=lambda e: e["name"].lower())
    files = sorted([e for e in data if e.get("type") == "file"], key=lambda e: e["name"].lower())
    other = [e for e in data if e.get("type") not in ("dir", "file")]

    lines = []
    location = f"{repo}/{path}" if path else repo
    lines.append(f"Contents of {location}:")
    lines.append("")

    for entry in dirs:
        lines.append(f"  [dir]  {entry['name']}/")

    for entry in files:
        size = entry.get("size", 0)
        size_str = _fmt_size(size)
        lines.append(f"  [file] {entry['name']}  ({size_str})")

    for entry in other:
        lines.append(f"  [{entry.get('type', '?')}] {entry['name']}")

    lines.append("")
    lines.append(f"{len(dirs)} director{'y' if len(dirs) == 1 else 'ies'}, {len(files)} file{'s' if len(files) != 1 else ''}")

    return "\n".join(lines)


def _fmt_size(n: int) -> str:
    """Format byte count as a human-readable string."""
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


# ── Tool dispatcher ───────────────────────────────────────────────────────────

async def run_tool(name: str, input: dict) -> str:
    if name == "read_github_file":
        path = input.get("path", "").strip()
        if not path:
            return "Error: 'path' parameter is required — provide the file path relative to the repo root."
        return await _read_github_file(path)

    elif name == "list_github_files":
        path = input.get("path", "").strip()
        return await _list_github_files(path)

    return f"Unknown GitHub tool: {name}"
