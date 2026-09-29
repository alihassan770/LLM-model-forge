"""Per-organisation settings and secrets.

Resolution order is environment → org override → platform default, and the
resolved value carries where it came from. When a client reports that something
behaves oddly, 'which layer decided this' is the first question, and the
settings page answers it without anyone reading code.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from . import db
from .crypto import SecretKeyMissing, decrypt, encrypt
from .ai import providers

# Platform defaults. A key absent from here is not a setting.
DEFAULTS: dict[str, Any] = {
    "ai.provider": providers.DEFAULT_PROVIDER,
    # Where this workspace's files live. Empty bucket means fall back to
    # whatever the deployment is configured for, which keeps a single-tenant
    # install working with nothing filled in.
    "storage.bucket": "",
    "storage.region": "",
    "storage.prefix": "forge",
    "storage.endpoint": "",
    "ai.model": "claude-opus-5",
    "ai.effort": "high",
    "units.canonical": "mm",
    "units.display": "mm",
    "limits.max_image_mb": 25,
    "limits.max_pdf_mb": 32,
    "limits.max_pdf_pages": 600,
    "limits.pages_per_extraction": 20,
}

# Settings that may be overridden by an environment variable, and by which one.
ENV_OVERRIDES: dict[str, str] = {
    "ai.provider": "FORGE_AI_PROVIDER",
    "ai.model": "FORGE_AI_MODEL",
}


@dataclass(frozen=True)
class Resolved:
    key: str
    value: Any
    source: str  # "environment" | "organisation" | "default"


def get(org_id: str, key: str) -> Any:
    return resolve(org_id, key).value


def resolve(org_id: str, key: str) -> Resolved:
    env_name = ENV_OVERRIDES.get(key)
    if env_name:
        raw = os.environ.get(env_name)
        if raw:
            return Resolved(key, raw, "environment")

    row = db.fetch_one(
        "SELECT value FROM org_settings WHERE org_id = %s AND key = %s", (org_id, key)
    )
    if row is not None:
        return Resolved(key, row["value"], "organisation")

    return Resolved(key, DEFAULTS.get(key), "default")


def resolve_many(org_id: str, keys: list[str]) -> dict[str, Resolved]:
    return {k: resolve(org_id, k) for k in keys}


def set_value(org_id: str, key: str, value: Any, updated_by: str | None = None) -> None:
    import json

    db.execute(
        """
        INSERT INTO org_settings (org_id, key, value, updated_by, updated_at)
        VALUES (%s, %s, %s, %s, now())
        ON CONFLICT (org_id, key)
        DO UPDATE SET value = EXCLUDED.value,
                      updated_by = EXCLUDED.updated_by,
                      updated_at = now()
        """,
        (org_id, key, json.dumps(value), updated_by),
    )


def clear(org_id: str, key: str) -> None:
    db.execute(
        "DELETE FROM org_settings WHERE org_id = %s AND key = %s", (org_id, key)
    )


# --------------------------------------------------------------------------- #
# Secrets
# --------------------------------------------------------------------------- #

def set_secret(
    org_id: str, key: str, plaintext: str, updated_by: str | None = None
) -> None:
    db.execute(
        """
        INSERT INTO org_secrets (org_id, key, ciphertext, hint, updated_by, updated_at)
        VALUES (%s, %s, %s, %s, %s, now())
        ON CONFLICT (org_id, key)
        DO UPDATE SET ciphertext = EXCLUDED.ciphertext,
                      hint = EXCLUDED.hint,
                      updated_by = EXCLUDED.updated_by,
                      updated_at = now()
        """,
        (
            org_id,
            key,
            encrypt(plaintext),
            providers.mask(plaintext),
            updated_by,
        ),
    )


def get_secret(org_id: str, key: str) -> str | None:
    """The org's own value, or the platform-wide fallback from the environment.

    Returns None when a stored secret cannot be decrypted, which in practice
    means FORGE_SECRET_KEY changed. The caller should treat that as 'not
    configured' and ask for the value again rather than crash.
    """
    row = db.fetch_one(
        "SELECT ciphertext FROM org_secrets WHERE org_id = %s AND key = %s",
        (org_id, key),
    )
    if row is not None:
        try:
            value = decrypt(row["ciphertext"])
        except SecretKeyMissing:
            return None
        if value:
            return value

    provider = providers.get_provider(key.removeprefix("ai.key."))
    if provider is not None:
        return os.environ.get(provider.key_env) or None
    return None


def secret_status(org_id: str, key: str) -> dict:
    """What the settings page needs to describe a secret without exposing it."""
    row = db.fetch_one(
        """
        SELECT s.hint, s.updated_at, u.email AS updated_by_email
        FROM org_secrets s
        LEFT JOIN users u ON u.id = s.updated_by
        WHERE s.org_id = %s AND s.key = %s
        """,
        (org_id, key),
    )
    if row is not None:
        return {
            "set": True,
            "source": "organisation",
            "hint": row["hint"],
            "updated_at": row["updated_at"],
            "updated_by_email": row["updated_by_email"],
        }

    provider = providers.get_provider(key.removeprefix("ai.key."))
    if provider is not None and os.environ.get(provider.key_env):
        return {
            "set": True,
            "source": "environment",
            "hint": providers.mask(os.environ[provider.key_env]),
            "updated_at": None,
            "updated_by_email": None,
        }

    return {"set": False, "source": None, "hint": "", "updated_at": None,
            "updated_by_email": None}


def delete_secret(org_id: str, key: str) -> None:
    db.execute("DELETE FROM org_secrets WHERE org_id = %s AND key = %s", (org_id, key))


# --------------------------------------------------------------------------- #
# The AI configuration, assembled
# --------------------------------------------------------------------------- #

def storage_config(org_id: str) -> dict:
    """This workspace's own bucket, if it has been given one.

    Returns `bucket` empty when the workspace has not configured storage, in
    which case the caller uses the deployment-wide backend. Keeping that
    fallback means an install that serves one customer needs nothing filled in,
    and an install that serves several can give each its own bucket - which is
    the point: one client's files are then not merely in a different folder
    from another's, they are in a different bucket under a different key.
    """
    return {
        "bucket": (resolve(org_id, "storage.bucket").value or "").strip(),
        "region": (resolve(org_id, "storage.region").value or "").strip(),
        "prefix": (resolve(org_id, "storage.prefix").value or "forge").strip(),
        "endpoint": (resolve(org_id, "storage.endpoint").value or "").strip(),
        "access_key": get_secret(org_id, "storage.access_key_id") or "",
        "secret_key": get_secret(org_id, "storage.secret_access_key") or "",
        "key_status": secret_status(org_id, "storage.access_key_id"),
    }


def ai_config(org_id: str) -> dict:
    """Everything the pipeline needs to make a model call for this org."""
    provider_id = resolve(org_id, "ai.provider")
    model_id = resolve(org_id, "ai.model")

    provider = providers.get_provider(provider_id.value) or providers.PROVIDERS[
        providers.DEFAULT_PROVIDER
    ]
    model = provider.model(model_id.value) or provider.default_model

    # How hard the model thinks before answering. Only meaningful on a model
    # that reasons; kept per-org because the right answer depends on whether
    # this workspace uploads clean spec sheets or phone photographs.
    effort = resolve(org_id, "ai.effort")
    chosen = effort.value if effort.value in model.reasoning_efforts else (
        model.default_effort or "")

    return {
        "provider": provider,
        "model": model,
        "provider_source": provider_id.source,
        "model_source": model_id.source,
        "effort": chosen,
        "effort_source": effort.source,
        "key": get_secret(org_id, f"ai.key.{provider.id}"),
        "key_status": secret_status(org_id, f"ai.key.{provider.id}"),
    }
