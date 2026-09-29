"""OpenRouter's catalogue, read from OpenRouter rather than hand-copied.

Every other provider in this app is a short hand-written list: three Claude
models, two DeepSeek ones, checked against the docs on the date recorded in
`providers.py`. OpenRouter is a different shape of thing. It resells four
hundred-odd models from every lab at once and the set changes weekly, so
copying it by hand would be wrong within a fortnight - and it would defeat the
point of adding OpenRouter at all, which is to be able to pick *any* model it
carries.

So this module fetches https://openrouter.ai/api/v1/models and maps each entry
onto the same `Model` dataclass the static providers use. Nothing downstream -
cost estimation, the vision check, the settings page, the extraction call -
knows the difference.

Two rules govern the cache, and both exist to keep a page render off the
network:

  - `cached()` never makes a request. It is what the extraction path and the
    settings-page render call, so a slow or unreachable OpenRouter cannot hang
    a request that had nothing to do with it.
  - `catalog()` may fetch, and is called deliberately: when the settings page
    is opened with a cold or stale cache, and when someone presses Refresh. It
    never raises. A failed fetch leaves the previous list in place and says so,
    because a stale catalogue is a great deal more useful than an empty one.

A model id that is not in the catalogue is still usable. `synthesize()` builds
a plausible `Model` from the id alone, so a model released this morning - or
one visible only to this user's own account - can be typed in and used, rather
than being refused by a list that happens to be a few hours old.
"""

from __future__ import annotations

import threading
import time

import httpx

from .providers import Model

MODELS_URL = "https://openrouter.ai/api/v1/models"

# Six hours. Long enough that nobody pays for the fetch twice in a working
# session, short enough that a model added this morning is offered this
# afternoon. The Refresh button exists for the impatient case.
TTL_SECONDS = 6 * 3600

# What a model gets when OpenRouter does not say. Only reached for the handful
# of entries with no `max_completion_tokens`, and for synthesised ids.
FALLBACK_MAX_OUTPUT = 32_000
FALLBACK_CONTEXT = 128_000

# The most output we will ever ask for, whatever a model claims it can produce.
#
# Two reasons, and the second one is why this is not merely tidy. Some models
# here advertise ceilings of 900k and even 1.8M output tokens; a part
# specification is tens of thousands at its longest, so the rest is a number
# with no meaning. And OpenRouter reserves credit for the *whole* request -
# prompt plus max_tokens - before it runs anything, so naming a ceiling we
# will never reach gets the request refused outright with a 402 for an
# extraction that would have cost cents. Asking for what we might actually
# use is the difference between working and not.
#
# 64k matches the most generous ceiling in the hand-written registry, which
# was set where a thorough answer fits rather than where a typical one does.
MAX_OUTPUT_CEILING = 64_000

# OpenRouter accepts max/xhigh/high/medium/low/minimal/none. These are the ones
# this app exposes, and they pass straight through untranslated.
EFFORTS = ("none", "low", "medium", "high", "max")
DEFAULT_EFFORT = "high"

# How long to leave OpenRouter alone after a failed fetch. Without this, an
# OpenRouter outage would cost *every* settings page load a full timeout,
# because a cold cache never stops being cold. Two minutes is long enough that
# nobody waits twice and short enough that a brief blip is forgiven quickly.
# The Refresh button ignores it, since that is someone asking on purpose.
RETRY_AFTER_FAILURE_SECONDS = 120

_lock = threading.Lock()
_models: list[Model] = []
_fetched_at: float = 0.0
_failed_at: float = 0.0
_error: str = ""


def _price_per_million(raw: str | None) -> float:
    """OpenRouter quotes USD per token, as a string. We store per million.

    A negative price means the cost is not fixed - the routing pseudo-models
    ("openrouter/auto" and friends) price by whatever they route to. Reporting
    that as a negative cost would put a negative number on an invoice estimate,
    so it becomes zero and the model's note says the estimate cannot be made.
    """
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.0
    return value * 1_000_000 if value > 0 else 0.0


def _to_model(raw: dict) -> Model | None:
    model_id = (raw.get("id") or "").strip()
    if not model_id:
        return None

    architecture = raw.get("architecture") or {}
    modalities = architecture.get("input_modalities") or []
    pricing = raw.get("pricing") or {}
    top = raw.get("top_provider") or {}
    params = set(raw.get("supported_parameters") or [])

    context = int(raw.get("context_length") or top.get("context_length") or
                  FALLBACK_CONTEXT)
    # A few entries report no completion ceiling. Asking for more than the
    # model allows is a 400, so fall back to something every model can honour
    # rather than to the context window. Then cap it: see MAX_OUTPUT_CEILING
    # for why a model's own advertised ceiling is not a number to pass on.
    reported = top.get("max_completion_tokens")
    max_output = min(int(reported) if reported else FALLBACK_MAX_OUTPUT,
                     MAX_OUTPUT_CEILING, context)

    price_in = _price_per_million(pricing.get("prompt"))
    price_out = _price_per_million(pricing.get("completion"))

    notes = []
    if float(pricing.get("prompt") or 0) < 0:
        notes.append("Priced by whatever it routes to, so the cost shown here "
                     "is not an estimate.")
    elif price_in == 0 and price_out == 0:
        notes.append("Free tier - expect rate limits.")
    if "image" not in modalities:
        notes.append("Text only - cannot read photos or PDFs, so it cannot be "
                     "used for extraction.")

    reasoning = "reasoning" in params
    return Model(
        id=model_id,
        label=raw.get("name") or model_id,
        vision="image" in modalities,
        # OpenRouter does not publish a per-image detail budget, and it varies
        # by the provider it routes to. Zero here means "unknown", which the
        # display handles separately from "text only".
        image_tokens=0,
        context=context,
        price_in=price_in,
        price_out=price_out,
        max_output=int(max_output),
        reasoning=reasoning,
        reasoning_efforts=EFFORTS if reasoning else (),
        default_effort=DEFAULT_EFFORT if reasoning else "",
        # Worth knowing per-model: with it we can demand schema-valid JSON, and
        # without it we fall back to asking nicely and repairing the answer.
        structured="structured_outputs" in params,
        note=" ".join(notes),
    )


def synthesize(model_id: str) -> Model:
    """A usable `Model` for an id the catalogue does not list.

    Assumes vision, deliberately. The alternative - assuming text-only - would
    have this app refuse, from its own stale copy of a list, a vision model
    that OpenRouter added yesterday. Guessing the other way at worst produces a
    clear error from OpenRouter naming the real problem.
    """
    return Model(
        id=model_id,
        label=model_id,
        vision=True,
        image_tokens=0,
        context=FALLBACK_CONTEXT,
        price_in=0.0,
        price_out=0.0,
        max_output=FALLBACK_MAX_OUTPUT,
        reasoning=False,
        note="Not in the fetched catalogue, so its limits and price are "
             "unknown and no cost can be estimated.",
    )


def cached() -> list[Model]:
    """The catalogue as last fetched. Never makes a request."""
    with _lock:
        return list(_models)


def status() -> dict:
    """Enough to tell someone how fresh the list on their screen is."""
    with _lock:
        return {
            "count": len(_models),
            "fetched_at": _fetched_at,
            "age_seconds": (time.time() - _fetched_at) if _fetched_at else None,
            "stale": (not _fetched_at
                      or time.time() - _fetched_at > TTL_SECONDS),
            "error": _error,
        }


def catalog(force: bool = False) -> list[Model]:
    """The catalogue, fetching it first if the copy we have is cold or stale.

    Never raises: on failure the previous list is kept and the reason is
    recorded in `status()`. Callers get models or they get nothing, but they
    never get an exception from a page render.
    """
    global _models, _fetched_at, _failed_at, _error

    now = time.time()
    with _lock:
        if not force:
            if _models and (now - _fetched_at) <= TTL_SECONDS:
                return list(_models)
            if _failed_at and (now - _failed_at) < RETRY_AFTER_FAILURE_SECONDS:
                # Tried recently and it did not work. Don't make this caller
                # wait out the same timeout to learn the same thing.
                return list(_models)

    try:
        response = httpx.get(MODELS_URL, timeout=10.0,
                             headers={"Accept": "application/json"})
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:  # noqa: BLE001
        with _lock:
            _error = f"{type(exc).__name__}: {exc}"[:200]
            _failed_at = time.time()
            return list(_models)

    parsed = [m for m in (_to_model(raw) for raw in payload.get("data") or [])
              if m is not None]
    if not parsed:
        with _lock:
            _error = "OpenRouter returned no models."
            _failed_at = time.time()
            return list(_models)

    # Vision first, then cheapest, then by name: the settings page reads this
    # list top to bottom and every stage of this pipeline starts with a photo.
    parsed.sort(key=lambda m: (not m.vision, m.price_in, m.label.lower()))

    with _lock:
        _models = parsed
        _fetched_at = time.time()
        _failed_at = 0.0
        _error = ""
        return list(_models)


def ensure() -> list[Model]:
    """Guarantee we have a catalogue before anything reads a model from it.

    The distinction from `catalog()` is the one that matters: this fetches
    only when we have *nothing*, not when what we have is merely old. A stale
    entry is a slightly wrong price; no entry at all means `synthesize()`
    invents the model, and an invented model is wrong in ways that break the
    run rather than merely misreport it.

    That is not hypothetical - it is what happened. On a freshly started
    server, before anyone had opened the settings page, every extraction
    resolved its model through `synthesize()` and therefore ran with a 32k
    output ceiling instead of the real 64k, with reasoning flagged off (so
    the truncation retry could never fire) and the price at zero (so the cost
    was logged as $0.00). The extraction then failed on the token limit. None
    of it was visible: the settings page showed the right model, because by
    then someone had loaded the settings page.

    Safe to call from a worker thread, which is where extraction runs. Never
    raises.
    """
    with _lock:
        if _models:
            return list(_models)
    return catalog()


def find(model_id: str) -> Model | None:
    """The catalogue entry for `model_id`, without fetching."""
    return next((m for m in cached() if m.id == model_id), None)


def vendors(models: list[Model]) -> list[tuple[str, list[Model]]]:
    """Group by the vendor prefix in the id, for the settings page's optgroups.

    Four hundred models in one flat list is not a menu, it is a haystack.
    """
    groups: dict[str, list[Model]] = {}
    for model in models:
        vendor = model.id.split("/", 1)[0] if "/" in model.id else "other"
        groups.setdefault(vendor, []).append(model)
    return sorted(groups.items(), key=lambda kv: kv[0].lower())
