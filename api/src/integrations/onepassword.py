"""
1Password CLI integration.

Retrieves credentials from the "Ki" 1Password vault via the `op` CLI.
Authentication uses OP_SERVICE_ACCOUNT_TOKEN — passed explicitly to the
subprocess environment so it's always present, even if Docker env propagation
is incomplete. stdin is always /dev/null so the process can never block
waiting for interactive input.
"""
import asyncio
import json
import logging
import os
import time

logger = logging.getLogger(__name__)

REQUIRED_ENV_VARS = ["OP_SERVICE_ACCOUNT_TOKEN"]

TOOL_SCHEMAS = [
    {
        "name": "get_credential",
        "description": (
            "Retrieve a credential from the Ki 1Password vault. "
            "Use to get usernames, passwords, or other fields before logging into something in the browser. "
            "item_name is the name of the item in 1Password (e.g. \"HEY\"), "
            "field is the field to retrieve (default \"password\", also accepts \"username\", \"email\", "
            "or any custom field name)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "item_name": {
                    "type": "string",
                    "description": "The name of the item in 1Password (e.g. \"HEY\", \"Gmail\", \"GitHub\")",
                },
                "field": {
                    "type": "string",
                    "description": "The field to retrieve: \"password\" (default), \"username\", \"email\", \"totp\" / \"otp\" / \"one-time password\" (returns the current TOTP code), or any custom field name",
                },
            },
            "required": ["item_name"],
        },
    }
]

SCHEDULES = []


def _sanitise_args(args: list[str]) -> list[str]:
    """
    Return a copy of args safe for logging — redact values that follow
    inherently sensitive flags so passwords never appear in log output.
    """
    REDACT_NEXT = {"--fields", "--field", "--value"}
    sanitised: list[str] = []
    skip_next = False
    for arg in args:
        if skip_next:
            sanitised.append("<redacted>")
            skip_next = False
        elif arg in REDACT_NEXT:
            sanitised.append(arg)
            skip_next = True
        else:
            sanitised.append(arg)
    return sanitised


_AUTH_PHRASES = ("sign in", "authenticate", "unauthorized", "invalid token", "service account", "session token", "not signed in")


async def _run_op(args: list[str], timeout: int = 15) -> bytes:
    """Run the op CLI with the given args; return stdout bytes or raise RuntimeError."""
    safe_args = _sanitise_args(args)

    token = os.environ.get("OP_SERVICE_ACCOUNT_TOKEN", "")
    if not token:
        raise RuntimeError("OP_SERVICE_ACCOUNT_TOKEN is not set — cannot authenticate with 1Password")

    logger.debug("op CLI starting: op %s (timeout=%ds)", " ".join(safe_args), timeout)
    t0 = time.monotonic()

    env = os.environ.copy()
    env["OP_SERVICE_ACCOUNT_TOKEN"] = token
    # Disable update checks — without this, op tries to reach update servers on every call
    # and hangs for 60s inside Docker if outbound connections to 1password.com are slow.
    env["OP_DISABLE_UPDATE_CHECK"] = "true"
    # Ensure op has a writable config dir with strict permissions.
    # /tmp/op-config fails because op checks parent-dir permissions too (/tmp is 1777).
    # /app/.op-config lives inside the container's app dir which has sane permissions.
    env.setdefault("OP_CONFIG_DIR", "/app/.op-config")
    config_dir = env["OP_CONFIG_DIR"]
    os.makedirs(config_dir, mode=0o700, exist_ok=True)
    os.chmod(config_dir, 0o700)

    proc = await asyncio.create_subprocess_exec(
        "op", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.DEVNULL,  # never block waiting for interactive input
        env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        elapsed = time.monotonic() - t0
        logger.error(
            "op CLI timed out after %.2fs — OP_SERVICE_ACCOUNT_TOKEN may be invalid or expired. "
            "Command: op %s", elapsed, " ".join(safe_args)
        )
        try:
            proc.kill()
        except Exception:
            pass
        raise RuntimeError(
            f"op CLI timed out after {timeout}s — check that OP_SERVICE_ACCOUNT_TOKEN is valid "
            f"and the 1Password service account has access to the Ki vault"
        )

    elapsed = time.monotonic() - t0
    if proc.returncode != 0:
        err = stderr.decode().strip()
        err_lower = err.lower()
        if any(phrase in err_lower for phrase in _AUTH_PHRASES):
            logger.error(
                "op CLI auth failure (rc=%d) after %.2fs: op %s — %s",
                proc.returncode, elapsed, " ".join(safe_args), err,
            )
            raise RuntimeError(f"op CLI auth failure — OP_SERVICE_ACCOUNT_TOKEN may be invalid or lack vault access: {err}")
        logger.debug(
            "op CLI failed (rc=%d) after %.2fs: op %s — %s",
            proc.returncode, elapsed, " ".join(safe_args), err,
        )
        raise RuntimeError(f"op CLI exited {proc.returncode}: {err}")

    logger.debug("op CLI completed in %.2fs: op %s", elapsed, " ".join(safe_args))
    return stdout


async def get_credential(item_name: str, field: str = "password") -> str:
    """
    Retrieve a credential field from the Ki 1Password vault via the op CLI.

    When field is "password", fetches the full item as JSON and returns the
    value of the first field with purpose "PASSWORD" or type "CONCEALED".
    This handles items like HEY where the password field label is non-standard
    (e.g. "sign_up[password]" instead of "password").

    For all other fields, uses --fields label=FIELDNAME directly.

    Uses OP_SERVICE_ACCOUNT_TOKEN from the environment automatically.
    Raises RuntimeError with stderr included if op exits non-zero.
    """
    if field == "password":
        # Fetch the full item JSON so we can find the password field by purpose/type,
        # regardless of how it's labelled in 1Password.
        raw = await _run_op(
            ["item", "get", item_name, "--vault", "Ki", "--format", "json", "--reveal"]
        )
        try:
            item = json.loads(raw.decode())
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"op CLI returned non-JSON for '{item_name}': {exc}") from exc

        fields = item.get("fields", [])

        # Priority 1: field with purpose == "PASSWORD"
        for f in fields:
            if f.get("purpose") == "PASSWORD":
                return f.get("value", "")

        # Priority 2: field with type == "CONCEALED" (covers non-standard password fields)
        for f in fields:
            if f.get("type") == "CONCEALED":
                return f.get("value", "")

        raise RuntimeError(
            f"No PASSWORD-purpose or CONCEALED-type field found in '{item_name}'"
        )

    # OTP/TOTP field: use --otp flag which returns the current TOTP code directly.
    if field.lower() in ("one-time password", "otp", "totp"):
        raw = await _run_op(
            ["item", "get", item_name, "--otp", "--vault", "Ki"],
        )
        return raw.decode().strip()

    # Non-password field: use label lookup directly.
    raw = await _run_op(
        ["item", "get", item_name, "--fields", f"label={field}", "--reveal", "--vault", "Ki"]
    )
    return raw.decode().strip()


async def run_tool(name: str, input: dict) -> str:
    if name == "get_credential":
        item_name = input["item_name"]
        field = input.get("field", "password")
        try:
            value = await get_credential(item_name, field)
            return value
        except RuntimeError as e:
            return f"FAILED: {e}"
    return f"Unknown 1Password tool: {name}"
