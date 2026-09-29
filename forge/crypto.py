"""Symmetric encryption for per-org secrets at rest.

Used for values a client owns and we merely hold: their Anthropic API key, their
S3 credentials, webhook signing secrets. The key comes from FORGE_SECRET_KEY and
is never rotated casually - a changed key orphans every stored secret.
"""

from __future__ import annotations

from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from .config import get_settings


class SecretKeyMissing(RuntimeError):
    pass


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    key = get_settings().secret_key
    if not key:
        raise SecretKeyMissing(
            "FORGE_SECRET_KEY is not set. Generate one with:\n"
            '  python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"'
        )
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt(plaintext: str) -> bytes:
    return _fernet().encrypt(plaintext.encode())


def decrypt(ciphertext: bytes) -> str | None:
    """Returns None rather than raising when the value cannot be decrypted.

    An undecryptable secret almost always means FORGE_SECRET_KEY changed. The
    caller should treat that as 'not configured' and prompt for the value again,
    not crash the request.
    """
    try:
        return _fernet().decrypt(bytes(ciphertext)).decode()
    except (InvalidToken, ValueError):
        return None


def generate_key() -> str:
    return Fernet.generate_key().decode()
