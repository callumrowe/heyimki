"""
TTS utility — xAI text-to-speech.

Called automatically after voice message responses; not exposed as a
conversational tool.
"""
import os
import re
import traceback

import httpx

from .. import memory as mem

_TTS_URL = "https://api.x.ai/v1/tts"
_MAX_CHARS = 3000

print(f"[tts] startup check — XAI_API_KEY set: {bool(os.environ.get('XAI_API_KEY'))}")


def _strip_markdown(text: str) -> str:
    text = re.sub(r"#{1,6}\s+", "", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"\*(.+?)\*", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"`{1,3}[^`]*`{1,3}", "", text, flags=re.DOTALL)
    text = re.sub(r"^[-*]\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^---+$", "", text, flags=re.MULTILINE)
    text = re.sub(r"\[(.+?)\]\(.+?\)", r"\1", text)
    return text.strip()


async def generate_speech(text: str) -> bytes | None:
    """Call xAI TTS and return raw MP3 bytes, or None on failure."""
    api_key = os.environ.get("XAI_API_KEY", "")
    print(f"[tts] generate_speech called, key_set={bool(api_key)}, text_len={len(text)}")
    if not api_key:
        print("[tts] XAI_API_KEY not set — skipping")
        return None
    cleaned = _strip_markdown(text)[:_MAX_CHARS]
    if not cleaned:
        return None
    # Strip emojis and punctuation-only content before hitting the API
    no_emoji = re.sub(r"[^\w\s]", "", re.sub(r"[\U00010000-\U0010FFFF\U00002600-\U00002BFF\U0001F000-\U0001FFFF]", "", cleaned, flags=re.UNICODE))
    if not no_emoji.strip():
        print("[tts] skipping — no speakable text after stripping emojis/punctuation")
        return None
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                _TTS_URL,
                headers={"Authorization": f"Bearer {api_key}"},
                json={"text": cleaned, "voice_id": "Ara", "output_format": {"codec": "mp3", "sample_rate": 44100, "bit_rate": 128000}, "language": "en"},
            )
            r.raise_for_status()
            print(f"[tts] generated {len(r.content):,} bytes")
            try:
                await mem.log_token_usage("xai-tts-ara", len(cleaned), 0, use_case="tts")
            except Exception as log_err:
                print(f"[tts] failed to log usage: {log_err}")
            return r.content
    except Exception as e:
        print(f"[tts] failed: {e}")
        traceback.print_exc()
        return None
