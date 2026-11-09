"""
Print a fresh VAPID keypair for web push, ready to paste into .env:

    docker compose run --rm --no-deps api python3 -m src.gen_vapid
"""
import base64

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


key = ec.generate_private_key(ec.SECP256R1())
private_raw = key.private_numbers().private_value.to_bytes(32, "big")
public_raw = key.public_key().public_bytes(
    serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
)
print(f"VAPID_PRIVATE_KEY={_b64url(private_raw)}")
print(f"VAPID_PUBLIC_KEY={_b64url(public_raw)}")
