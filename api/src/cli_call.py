"""
Call the api from inside its own container, with the right secret attached.
Used by bin/ki so secrets never have to be passed on a command line.

    python3 -m src.cli_call POST /heartbeat/trigger
    python3 -m src.cli_call GET /usage
"""
import json
import os
import sys
import urllib.request


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: python3 -m src.cli_call <GET|POST> <path>", file=sys.stderr)
        return 2
    method, path = sys.argv[1].upper(), sys.argv[2]
    # /admin/*, /usage and /integrations take the web app secret; everything
    # else the CLI calls takes the internal one. DESKTOP_SECRET works for both.
    token = os.environ.get("DESKTOP_SECRET") or os.environ.get("INTERNAL_SECRET", "")
    req = urllib.request.Request(
        f"http://localhost:8000{path}",
        method=method,
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=600) as r:
        body = r.read()
    try:
        print(json.dumps(json.loads(body), indent=2))
    except ValueError:
        print(body.decode("utf-8", errors="replace"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
