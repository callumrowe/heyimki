"""
Image generation integration — xAI Aurora (via Grok API).

Exposes a single conversation tool: generate_image.
Generates an image from a text prompt, then pushes it directly to the user
over WhatsApp via the Baileys /send-image internal endpoint so the image
arrives as a proper WhatsApp image message rather than a plain URL.
"""
import base64
import os

import httpx
import openai

from ..memory import log_token_usage, get_setting

REQUIRED_ENV_VARS = ["XAI_API_KEY"]

TOOL_SCHEMAS = [
    {
        "name": "generate_image",
        "description": (
            "Generate an image from a text prompt using xAI's image generation model. "
            "Use when the user asks you to generate, create, draw, or make an image or picture. "
            "Extract a clear, descriptive prompt from what the user asked for."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": (
                        "A clear, descriptive text prompt for the image to generate. "
                        "Be specific about style, subject, composition, and mood."
                    ),
                },
            },
            "required": ["prompt"],
        },
    }
]

SCHEDULES: list[dict] = []

_BAILEYS_URL = os.environ.get("BAILEYS_INTERNAL_URL", "http://baileys:3000")
_INTERNAL_SECRET = os.environ.get("INTERNAL_SECRET", "")
_ALLOWED_JID = os.environ.get("ALLOWED_JID", "")

_MODEL_DEFAULT = "grok-imagine-image"

_xai_client = openai.AsyncOpenAI(
    api_key=os.environ.get("XAI_API_KEY", ""),
    base_url="https://api.x.ai/v1",
)


async def _generate_image_bytes(prompt: str) -> tuple[bytes, str]:
    """Call xAI images.generate and return (raw image bytes, model used)."""
    model = await get_setting("image_gen_model", _MODEL_DEFAULT)
    response = await _xai_client.images.generate(
        model=model,
        prompt=prompt,
        n=1,
    )
    image_data = response.data[0]

    if image_data.b64_json:
        return base64.b64decode(image_data.b64_json), model

    if image_data.url:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.get(image_data.url)
            r.raise_for_status()
            return r.content, model

    raise ValueError("xAI returned neither b64_json nor url in image response")


async def _send_image_via_baileys(image_bytes: bytes, caption: str = "") -> None:
    """POST the image buffer to Baileys' /send-image endpoint."""
    payload = {
        "secret": _INTERNAL_SECRET,
        "jid": _ALLOWED_JID,
        "image_b64": base64.b64encode(image_bytes).decode(),
        "caption": caption,
    }
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(f"{_BAILEYS_URL}/send-image", json=payload)
        r.raise_for_status()


async def run_tool(name: str, input: dict) -> str:
    if name == "generate_image":
        prompt = input["prompt"]
        print(f"[image_gen] generating image via xAI: {prompt[:80]!r}")

        try:
            image_bytes, model_used = await _generate_image_bytes(prompt)
            print(f"[image_gen] got {len(image_bytes):,} bytes from {model_used!r}, sending to Baileys")
            await log_token_usage(model_used, 0, 0, use_case="image_gen")
        except Exception as e:
            print(f"[image_gen] xAI error: {e}")
            return f"Image generation failed: {e}"

        try:
            await _send_image_via_baileys(image_bytes)
            print("[image_gen] image sent via Baileys")
        except Exception as e:
            print(f"[image_gen] Baileys send error: {e}")
            return f"Image was generated but could not be sent: {e}"

        return "Image generated and sent successfully."

    return f"Unknown image_gen tool: {name}"
