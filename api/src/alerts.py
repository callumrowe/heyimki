"""
Centralised alert delivery for Ki.

send_alert(message) is the single entry point for all system alerts.
It delivers to:
  1. WhatsApp (via Baileys /send)
  2. Desktop web app (via mem.save_turn — appears in Ki chat UI)
  3. Stdout (always)

All channels are attempted independently — a failure in one never
prevents delivery to the others.

Usage:
    from .alerts import send_alert
    await send_alert("⚠️ Something is wrong")
"""
import asyncio
import logging
import os

import httpx

logger = logging.getLogger(__name__)

BAILEYS_URL = os.environ.get("BAILEYS_URL", "http://baileys:3000")
INTERNAL_SECRET = os.environ.get("INTERNAL_SECRET", "")


async def send_alert(message: str) -> None:
    """
    Deliver an alert message to all configured channels.

    Channels:
      - stdout/logging  (always, never fails)
      - WhatsApp        (via Baileys /send)
      - Desktop app     (via mem.save_turn on both channels so it appears
                         in the chat UI regardless of which the user has open)

    Non-blocking: all channel failures are caught and logged so a broken
    WhatsApp connection never prevents the desktop alert from arriving, and
    vice versa.  The caller does not need to handle exceptions.
    """
    # ── 1. Stdout / logging — always ─────────────────────────────────────────
    print(f"[alert] {message}")
    logger.warning("[alert] %s", message)

    # ── 2. WhatsApp via Baileys ───────────────────────────────────────────────
    if INTERNAL_SECRET:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.post(
                    f"{BAILEYS_URL}/send",
                    json={"text": message, "secret": INTERNAL_SECRET},
                )
                r.raise_for_status()
            print("[alert] delivered via WhatsApp")
        except Exception as e:
            print(f"[alert] WhatsApp delivery failed (non-fatal): {e}")
    else:
        print("[alert] WhatsApp skipped — INTERNAL_SECRET not set")

    # ── 3. Desktop web app ────────────────────────────────────────────────────
    # Import here to avoid circular imports at module load time (alerts is
    # imported by main.py before memory is fully initialised).
    try:
        from . import memory as mem  # noqa: PLC0415
        # Save to both channels so the alert is visible regardless of which
        # interface the user has open.
        await asyncio.gather(
            mem.save_turn(user="", assistant=message, channel="whatsapp"),
            mem.save_turn(user="", assistant=message, channel="desktop"),
            return_exceptions=True,
        )
        print("[alert] delivered to desktop conversation history")
    except Exception as e:
        print(f"[alert] desktop delivery failed (non-fatal): {e}")
