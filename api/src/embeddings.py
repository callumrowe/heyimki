import os

from openai import AsyncOpenAI

_client = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
MODEL = "text-embedding-3-small"


async def embed(text: str) -> list[float]:
    response = await _client.embeddings.create(input=text, model=MODEL)
    try:
        from . import memory as mem
        await mem.log_token_usage(MODEL, response.usage.total_tokens, 0)
    except Exception:
        pass
    return response.data[0].embedding
