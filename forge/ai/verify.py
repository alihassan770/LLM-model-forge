"""Check that a stored API key actually works.

A key that is merely *saved* tells you nothing - the useful question is whether
it authenticates, and whether the selected model is reachable with it. Finding
that out when someone presses "Test connection" is far better than finding out
during a job that has already cost the user an upload and a wait.

Claude goes through the official Anthropic SDK. DeepSeek exposes an
OpenAI-compatible surface, so it goes over plain HTTP. OpenRouter needs its
own path because its model list is public: checking a key against it would
pass any string at all - see `_verify_openrouter`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

from . import openrouter
from .providers import Provider


@dataclass
class VerifyResult:
    ok: bool
    message: str
    latency_ms: int | None = None
    models_seen: int | None = None


def verify(provider: Provider, api_key: str, model_id: str) -> VerifyResult:
    if not api_key or not api_key.strip():
        return VerifyResult(False, "No API key is set for this provider.")

    started = time.monotonic()
    try:
        if provider.id == "anthropic":
            result = _verify_anthropic(api_key, model_id)
        elif provider.id == "openrouter":
            result = _verify_openrouter(provider, api_key, model_id)
        else:
            result = _verify_openai_compatible(provider, api_key, model_id)
    except httpx.TimeoutException:
        return VerifyResult(False, "The provider did not respond within 15 seconds.")
    except httpx.HTTPError as exc:
        return VerifyResult(False, f"Could not reach the provider: {type(exc).__name__}")
    except Exception as exc:  # noqa: BLE001
        return VerifyResult(False, f"Unexpected error: {type(exc).__name__}")

    result.latency_ms = int((time.monotonic() - started) * 1000)
    return result


def _verify_anthropic(api_key: str, model_id: str) -> VerifyResult:
    try:
        import anthropic
    except ImportError:
        return VerifyResult(False, "The anthropic package is not installed.")

    client = anthropic.Anthropic(api_key=api_key, timeout=15.0, max_retries=0)
    try:
        models = list(client.models.list(limit=100))
    except anthropic.AuthenticationError:
        return VerifyResult(False, "That API key was rejected by Anthropic.")
    except anthropic.PermissionDeniedError:
        return VerifyResult(False, "That key is valid but lacks permission.")
    except anthropic.RateLimitError:
        # The key authenticated; it is just busy. That is a pass.
        return VerifyResult(True, "Key is valid (rate limited right now).")
    except anthropic.APIStatusError as exc:
        return VerifyResult(False, f"Anthropic returned {exc.status_code}.")

    ids = {m.id for m in models}
    if model_id not in ids:
        return VerifyResult(
            False,
            f"Key works, but '{model_id}' is not available to this account.",
            models_seen=len(ids),
        )
    return VerifyResult(True, f"Connected. '{model_id}' is available.",
                        models_seen=len(ids))


def _verify_openrouter(
    provider: Provider, api_key: str, model_id: str
) -> VerifyResult:
    """Check the key against `/key`, not `/models`.

    This is the whole reason OpenRouter needs its own function. Its `/models`
    endpoint is public - it answers with the full catalogue whether you send a
    key, a wrong key, or no key at all. Verifying against it would report
    "connected" for any string a user typed, which is worse than not checking:
    the settings page would say the key works, and the first upload would be
    where they found out otherwise. `/key` actually authenticates, and returns
    401 when the key is wrong.
    """
    base = (provider.base_url or "").rstrip("/")
    response = httpx.get(
        f"{base}/key",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=15.0,
    )

    if response.status_code in (401, 403):
        return VerifyResult(False, f"That API key was rejected by {provider.label}.")
    if response.status_code == 429:
        return VerifyResult(True, "Key is valid (rate limited right now).")
    if response.status_code >= 400:
        return VerifyResult(
            False, f"{provider.label} returned {response.status_code}."
        )

    # The key works. Say something useful about the balance, because "your key
    # is fine but you are out of credit" is the next failure along and it is
    # cheap to warn about here.
    try:
        data = (response.json() or {}).get("data") or {}
    except ValueError:
        data = {}

    remaining = data.get("limit_remaining")
    if isinstance(remaining, (int, float)) and remaining <= 0:
        return VerifyResult(
            False,
            "The key is valid but has no credit left. Top up the OpenRouter "
            "account before running an extraction.",
        )

    catalogue = openrouter.catalog()
    known = openrouter.find(model_id)
    suffix = ""
    if isinstance(remaining, (int, float)):
        suffix = f" ${remaining:.2f} of credit remaining."

    if catalogue and known is None:
        # Not a failure: the catalogue is a cached snapshot, and OpenRouter
        # also serves models that are private to an account.
        return VerifyResult(
            True,
            f"Key works. '{model_id}' was not in the fetched catalogue - it "
            f"may still work if it is new or private to your account.{suffix}",
            models_seen=len(catalogue) or None,
        )
    if known is not None and not known.vision:
        return VerifyResult(
            False,
            f"Key works, but {known.label} cannot read images, so it cannot "
            f"be used for extraction. Choose a vision model.",
            models_seen=len(catalogue) or None,
        )
    return VerifyResult(True, f"Connected. '{model_id}' is available.{suffix}",
                        models_seen=len(catalogue) or None)


def _verify_openai_compatible(
    provider: Provider, api_key: str, model_id: str
) -> VerifyResult:
    url = f"{(provider.base_url or '').rstrip('/')}/models"
    response = httpx.get(
        url,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=15.0,
    )

    if response.status_code in (401, 403):
        return VerifyResult(False, f"That API key was rejected by {provider.label}.")
    if response.status_code == 429:
        return VerifyResult(True, "Key is valid (rate limited right now).")
    if response.status_code >= 400:
        return VerifyResult(
            False, f"{provider.label} returned {response.status_code}."
        )

    try:
        payload = response.json()
        ids = {m.get("id") for m in payload.get("data", [])}
    except ValueError:
        return VerifyResult(True, "Key is valid, but the model list was unreadable.")

    if ids and model_id not in ids:
        # Providers do not always list experimental models, so this is a
        # warning rather than a failure.
        return VerifyResult(
            True,
            f"Key works. '{model_id}' was not in the listed models - it may "
            f"still work if it is experimental.",
            models_seen=len(ids),
        )
    return VerifyResult(True, f"Connected. '{model_id}' is available.",
                        models_seen=len(ids) or None)
