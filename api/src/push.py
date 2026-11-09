"""Push notification utility.

Web push (browsers + the installed PWA): uses pywebpush + VAPID. Optional —
everything is a no-op until VAPID_PRIVATE_KEY / VAPID_PUBLIC_KEY are set.
"""
import asyncio
import json
import os

VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY", "")
VAPID_PUBLIC_KEY = os.environ.get("VAPID_PUBLIC_KEY", "")
VAPID_SUBJECT = os.environ.get("VAPID_SUBJECT", "mailto:you@example.com")


async def send_push_to_all(title: str, body: str, tag: str = "ki-message") -> None:
    """Send a web push notification to all subscribers. No-op if VAPID keys not set."""
    if not VAPID_PRIVATE_KEY or not VAPID_PUBLIC_KEY:
        return

    from . import memory as mem
    from pywebpush import webpush, WebPushException

    subscriptions = await mem.get_push_subscriptions()
    if not subscriptions:
        return

    data = json.dumps({"title": title, "body": body, "tag": tag})

    def _send_one(sub: dict) -> str | None:
        """Sends push to one subscriber. Returns endpoint if it should be deleted."""
        try:
            webpush(
                subscription_info={
                    "endpoint": sub["endpoint"],
                    "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]},
                },
                data=data,
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims={"sub": VAPID_SUBJECT},
            )
        except WebPushException as e:
            if e.response is not None and e.response.status_code in (400, 401, 404, 410):
                body_snippet = ""
                try:
                    body_snippet = f", {e.response.text[:80]!r}"
                except Exception:
                    pass
                print(f"[push] subscription stale (HTTP {e.response.status_code}{body_snippet}): {sub['endpoint'][:40]}…")
                return sub["endpoint"]
            print(f"[push] send failed: {e}")
        except Exception as e:
            print(f"[push] error: {e}")
        return None

    loop = asyncio.get_event_loop()
    results = await asyncio.gather(*[
        loop.run_in_executor(None, _send_one, sub) for sub in subscriptions
    ])
    failed = [r for r in results if r]
    if failed:
        await mem.delete_push_subscriptions(failed)


async def send_to_user(text: str, title: str = "Ki") -> bool:
    """Deliver a proactive message with Ki app as primary, WhatsApp as fallback.

    Always saves to the desktop channel (appears in Ki app) and fires push
    notifications first. Baileys/WhatsApp is attempted afterwards but errors
    are caught and logged — Baileys being down never prevents Ki app delivery.

    Returns True if the desktop DB save succeeded (message reached Ki app).
    """
    import httpx
    import sqlalchemy as sa
    from .db import SessionLocal

    BAILEYS_URL = os.environ.get("BAILEYS_URL", "http://baileys:3000")
    INTERNAL_SECRET = os.environ.get("INTERNAL_SECRET", "")

    # 1. Save to desktop channel — primary delivery
    app_ok = False
    try:
        async with SessionLocal() as session:
            await session.execute(
                sa.text(
                    "INSERT INTO conversations (role, content, channel) "
                    "VALUES ('assistant', :content, 'desktop')"
                ),
                {"content": text},
            )
            await session.commit()
        app_ok = True
        print("[notify] ✓ saved to desktop channel")
    except Exception as e:
        print(f"[notify] ✗ failed to save to desktop channel: {e}")

    # 2. Web push notifications
    try:
        await send_push_to_all(text.strip()[:120], "")
    except Exception as e:
        print(f"[notify] push notification failed (non-fatal): {e}")

    # 3. WhatsApp via Baileys — secondary fallback, non-blocking
    if INTERNAL_SECRET:
        try:
            async with httpx.AsyncClient() as client:
                r = await client.post(
                    f"{BAILEYS_URL}/send",
                    json={"text": text, "secret": INTERNAL_SECRET},
                    timeout=10,
                )
            if r.is_success:
                print("[notify] ✓ WhatsApp send succeeded")
            else:
                print(f"[notify] WhatsApp send failed (non-fatal): HTTP {r.status_code}")
        except Exception as e:
            print(f"[notify] WhatsApp send failed (non-fatal): {e}")

    return app_ok
