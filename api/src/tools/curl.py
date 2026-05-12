"""
curl tool — execute a curl command in a subprocess and return its output.

Safety constraint: only commands that start with the word "curl" are accepted.
Timeout: 15 seconds.
"""
import asyncio
import json
import shlex


TIMEOUT_SECONDS = 15


async def run_curl(command: str) -> str:
    """
    Execute a curl command and return JSON with stdout, stderr, and exit_code.

    - command must start with "curl" (case-insensitive, leading whitespace stripped).
    - Runs in a subprocess via asyncio.create_subprocess_shell.
    - Times out after TIMEOUT_SECONDS seconds.
    - Returns a JSON-encoded dict: {"stdout": "...", "stderr": "...", "exit_code": N}
    """
    stripped = command.strip()

    # Safety: only allow commands that start with "curl"
    first_token = shlex.split(stripped)[0] if stripped else ""
    if first_token.lower() != "curl":
        return json.dumps({
            "stdout": "",
            "stderr": f"Rejected: command must start with 'curl', got '{first_token}'.",
            "exit_code": 1,
        })

    print(f"[curl] running: {stripped[:200]}")

    try:
        proc = await asyncio.create_subprocess_shell(
            stripped,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            print(f"[curl] timed out after {TIMEOUT_SECONDS}s")
            return json.dumps({
                "stdout": "",
                "stderr": f"Command timed out after {TIMEOUT_SECONDS} seconds.",
                "exit_code": 124,
            })

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        exit_code = proc.returncode if proc.returncode is not None else -1

        print(f"[curl] exit_code={exit_code} stdout={len(stdout)}B stderr={len(stderr)}B")

        return json.dumps({
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": exit_code,
        })

    except Exception as e:
        print(f"[curl] error: {e}")
        return json.dumps({
            "stdout": "",
            "stderr": f"Failed to execute command: {e}",
            "exit_code": 1,
        })
