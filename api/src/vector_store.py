"""
Qdrant client and collection bootstrap.
Collections: 'conversations' and 'obsidian', both using 1536-dim vectors
(text-embedding-3-small via OpenAI).
"""
import os

from qdrant_client import AsyncQdrantClient
from qdrant_client.models import Distance, VectorParams

QDRANT_URL = os.environ.get("QDRANT_URL", "http://qdrant:6333")
VECTOR_SIZE = 1536

client = AsyncQdrantClient(url=QDRANT_URL)

COLLECTIONS = ["conversations", "obsidian", "emails"]


async def ensure_collections() -> None:
    existing = {c.name for c in (await client.get_collections()).collections}
    for name in COLLECTIONS:
        if name not in existing:
            await client.create_collection(
                collection_name=name,
                vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE),
            )
