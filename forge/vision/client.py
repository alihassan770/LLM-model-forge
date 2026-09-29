"""One call, three providers, one JSON contract.

Claude goes through the official Anthropic SDK and its structured-output
support, which guarantees schema-valid JSON or a loud failure. DeepSeek and
OpenRouter both expose an OpenAI-compatible surface and share a code path,
but they do not get identical requests: DeepSeek's JSON mode guarantees
*valid JSON* and not *your schema*, so the schema is restated in the prompt
and the result is validated on the way back, whereas an OpenRouter model that
advertises `structured_outputs` is held to the schema by the provider and
needs neither.

All three return the same shape, so nothing above this module knows which ran.
"""

from __future__ import annotations

import base64
import json
import re
import time
from dataclasses import dataclass, field

import httpx

from ..ai.providers import Model, Provider, estimate_cost


@dataclass
class Completion:
    ok: bool
    data: dict | None = None
    error: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    latency_ms: int = 0
    stop_reason: str = ""
    cost_usd: float = 0.0
    raw_text: str = field(default="", repr=False)


class ExtractionError(Exception):
    pass


def complete_json(
    provider: Provider,
    model: Model,
    api_key: str,
    system: str,
    user_text: str,
    images: list[tuple[bytes, str]],
    schema: dict,
    max_tokens: int = 16000,
    effort: str = "",
) -> Completion:
    """Ask the configured model for a JSON document matching `schema`.

    `effort` is how hard the model should think before answering. Empty means
    the model's own default from the registry.
    """
    if not api_key:
        return Completion(False, error="No API key is configured for this provider.")
    if not model.vision and images:
        return Completion(
            False,
            error=f"{model.label} cannot read images. Choose a vision model in "
                  f"Settings.",
        )

    started = time.monotonic()
    try:
        if provider.id == "anthropic":
            result = _anthropic(model, api_key, system, user_text, images,
                                schema, max_tokens)
        else:
            result = _openai_compatible(provider, model, api_key, system,
                                        user_text, images, schema, max_tokens,
                                        effort)
    except httpx.TimeoutException:
        return Completion(False, error="The provider did not respond in time.")
    except Exception as exc:  # noqa: BLE001
        return Completion(False, error=f"{type(exc).__name__}: {exc}"[:400])

    result.latency_ms = int((time.monotonic() - started) * 1000)
    result.cost_usd = estimate_cost(model, result.input_tokens, result.output_tokens)
    return result


# --------------------------------------------------------------------------- #
# Claude
# --------------------------------------------------------------------------- #

def _anthropic(model, api_key, system, user_text, images, schema, max_tokens):
    import anthropic

    client = anthropic.Anthropic(api_key=api_key, timeout=600.0, max_retries=1)

    content: list[dict] = []
    for data, mime in images:
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": mime,
                "data": base64.standard_b64encode(data).decode(),
            },
        })
    content.append({"type": "text", "text": user_text})

    try:
        # Streaming because a long extraction at high effort can exceed the
        # non-streaming HTTP timeout; get_final_message() gives the whole
        # response back without handling individual events.
        with client.messages.stream(
            model=model.id,
            max_tokens=max_tokens,
            thinking={"type": "adaptive"},
            output_config={
                "effort": "high",
                "format": {"type": "json_schema", "schema": schema},
            },
            system=[{
                "type": "text",
                "text": system,
                # The prompt is long and identical across every extraction, so
                # it is worth caching; the minimum on current models is 512
                # tokens and this clears it comfortably.
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": content}],
        ) as stream:
            message = stream.get_final_message()
    except anthropic.AuthenticationError:
        return Completion(False, error="The API key was rejected by Anthropic.")
    except anthropic.RateLimitError:
        return Completion(False, error="Rate limited by Anthropic. Try again shortly.")
    except anthropic.APIStatusError as exc:
        return Completion(False, error=f"Anthropic returned {exc.status_code}.")

    usage = message.usage
    common = dict(
        input_tokens=getattr(usage, "input_tokens", 0) or 0,
        output_tokens=getattr(usage, "output_tokens", 0) or 0,
        cached_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        stop_reason=message.stop_reason or "",
    )

    # A refusal is a successful HTTP response with an empty body - check it
    # before reading content, or this raises StopIteration instead of
    # reporting what actually happened.
    if message.stop_reason == "refusal":
        return Completion(False, error="The model declined to analyse this file.",
                          **common)

    text = next((b.text for b in message.content if b.type == "text"), "")
    if not text:
        return Completion(False, error="The model returned no content.", **common)

    return _parse(text, **common)


# --------------------------------------------------------------------------- #
# DeepSeek and other OpenAI-compatible providers
# --------------------------------------------------------------------------- #

def _chat_body(provider, model, system, parts, max_tokens, effort, schema,
               enforced=False):
    """The request body for an OpenAI-compatible provider.

    The shape is shared; two things are not, and both are worth stating rather
    than assuming.

    **Thinking.** DeepSeek has it on by default at high effort, so every
    extraction this app ever ran was already reasoning without being asked -
    which also means a change to their default would silently change our
    results. OpenRouter spells the same idea `reasoning` with an `effort`
    string. Either way we send it explicitly, so the behaviour is ours.

    **JSON.** DeepSeek's JSON mode guarantees syntactically valid JSON but not
    *our* schema. OpenRouter can do better on the models that advertise
    `structured_outputs`: a strict `json_schema` is enforced by the provider,
    so a malformed or incomplete spec fails loudly instead of arriving as a
    plausible-looking object with a dimension missing. We take that whenever
    it is on offer and fall back to plain JSON mode when it is not.

    Either way the reasoning comes back in its own field, so it never pollutes
    `content` and `_parse` is unaffected.
    """
    body = {
        "model": model.id,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": parts},
        ],
    }

    if enforced:
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": "partspec",
                "strict": True,
                "schema": schema,
            },
        }
    else:
        body["response_format"] = {"type": "json_object"}

    if model.reasoning:
        chosen = effort if effort in model.reasoning_efforts else model.default_effort
        if provider.id == "openrouter":
            # `exclude` keeps the thinking out of the response body. It is
            # still done and still billed; we simply have no use for it and no
            # reason to carry tens of thousands of tokens back over the wire.
            body["reasoning"] = ({"effort": "none"} if chosen == "none"
                                 else {"effort": chosen or "high",
                                       "exclude": True})
        else:
            body["thinking"] = (
                {"type": "disabled"} if chosen == "none"
                else {"type": "enabled", "reasoning_effort": chosen or "high"}
            )
    return body


def _headers(provider, api_key: str) -> dict:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if provider.id == "openrouter":
        # Attribution. Optional, and worth sending: it is how a request is
        # identified on OpenRouter's own dashboards, which is the difference
        # between reading your usage and guessing at it.
        headers["HTTP-Referer"] = "https://github.com/HSxTech/3d_rendering"
        headers["X-Title"] = "Model Forge"
    return headers


# Status codes worth trying again. Everything here is the provider saying "not
# right now" rather than "not ever": 429 is rate limiting, and 500/502/503/504
# are its own upstream trouble. A 400 or a 401 is a fault in the request and
# repeating it only wastes time.
TRANSIENT_STATUSES = frozenset({429, 500, 502, 503, 504})

# Three attempts, not more. An extraction is a two-minute generation, so a
# retry is expensive and a third failure means the provider is properly down
# rather than briefly busy - at which point telling the user beats queueing
# behind an outage.
MAX_ATTEMPTS = 3

# Deliberately short relative to the request it guards. Waiting a minute to
# retry something that takes two is worse than starting again promptly, and a
# transient upstream error clears in seconds.
RETRY_BACKOFF_SECONDS = (3.0, 9.0)


def _retry_after(response, default: float) -> float:
    """Honour Retry-After when the provider sends one, within reason."""
    raw = (response.headers.get("retry-after") or "").strip()
    try:
        return max(0.0, min(30.0, float(raw)))
    except (TypeError, ValueError):
        return default


def _post_with_retry(url: str, headers: dict, body: dict):
    """POST, trying again when the failure is the provider's and temporary.

    This exists because a single transient 502 was destroying a complete
    extraction. The generation runs for one to two minutes and produces tens of
    thousands of tokens; losing all of it to one bad second, with no retry and
    a raw `{"error":{"message":"upstream error"}}` shown to the user, is the
    worst possible trade. The Anthropic path has had `max_retries=1` all along
    and this one had nothing.

    Returns either a response or a Completion describing a failure that no
    amount of retrying will fix.
    """
    last_detail = ""
    for attempt in range(MAX_ATTEMPTS):
        try:
            response = httpx.post(url, headers=headers, json=body, timeout=600.0)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            # A connection reset or a read timeout is the same class of problem
            # as a 502 and gets the same treatment.
            last_detail = f"{type(exc).__name__}: {exc}"
            if attempt == MAX_ATTEMPTS - 1:
                return None, last_detail
            time.sleep(RETRY_BACKOFF_SECONDS[attempt])
            continue

        if response.status_code not in TRANSIENT_STATUSES:
            return response, ""

        last_detail = f"HTTP {response.status_code}: {response.text[:200]}"
        if attempt == MAX_ATTEMPTS - 1:
            return response, last_detail
        time.sleep(_retry_after(response, RETRY_BACKOFF_SECONDS[attempt]))

    return None, last_detail


def _openai_compatible(provider, model, api_key, system, user_text, images,
                       schema, max_tokens, effort="", _retrying=False,
                       _plain_json=False):
    parts: list[dict] = []
    for data, mime in images:
        b64 = base64.standard_b64encode(data).decode()
        parts.append({
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{b64}"},
        })

    # When the provider enforces the schema itself there is no reason to spend
    # thousands of prompt tokens restating it - the constraint is already
    # binding. Otherwise JSON mode guarantees only syntactically valid JSON, so
    # the schema goes in the prompt as well and the result is validated on
    # return.
    #
    # `_plain_json` is the second attempt after a provider refused the schema
    # outright; see the 400 handling below.
    enforced = (provider.id == "openrouter"
                and getattr(model, "structured", False)
                and not _plain_json)
    parts.append({
        "type": "text",
        "text": user_text if enforced else (
            f"{user_text}\n\n"
            f"Reply with a single JSON object and nothing else. It must match "
            f"this JSON Schema exactly, including every required field:\n\n"
            f"{json.dumps(schema)}"
        ),
    })

    response, transient = _post_with_retry(
        f"{provider.base_url.rstrip('/')}/chat/completions",
        _headers(provider, api_key),
        _chat_body(provider, model, system, parts, max_tokens, effort, schema,
                   enforced),
    )

    if response is None:
        return Completion(
            False,
            error=(f"{provider.label} could not be reached after "
                   f"{MAX_ATTEMPTS} attempts ({transient}). This is the "
                   f"provider, not your files or your key - try again in a "
                   f"few minutes."))

    if response.status_code in (401, 403):
        return Completion(False, error=f"The API key was rejected by {provider.label}.")
    if response.status_code == 402:
        # Worth its own message. OpenRouter reserves credit for the whole
        # request - prompt plus max_tokens - before running anything, so this
        # fires on the balance the request *could* reach, not the one it would
        # actually have cost. The raw body says "requires more credits, or
        # fewer max_tokens", which reads like a bug in this app when it is
        # usually an empty account.
        return Completion(
            False,
            error=(f"{provider.label} declined the request for want of "
                   f"credit. It reserves the full cost up front - the prompt "
                   f"plus the most the reply could run to - so this can fire "
                   f"while a balance still looks sufficient. Top up the "
                   f"account, or choose a cheaper model."))
    if response.status_code == 429:
        return Completion(
            False,
            error=(f"Rate limited by {provider.label}, and still limited after "
                   f"{MAX_ATTEMPTS} attempts. Wait a few minutes."))
    # The schema was refused, not the request. Ask again without it.
    #
    # "Structured outputs" is a capability flag, not a promise about *your*
    # schema. OpenRouter routes to a provider that compiles the schema into a
    # grammar, and ours - 93 properties, 17 enums - is large enough that
    # Anthropic's compiler rejects it: "The compiled grammar is too large".
    # That made every Claude model unusable through OpenRouter, reported as a
    # raw 400 mentioning grammars, which is nobody's idea of an explanation.
    #
    # Falling back costs one fast, unbilled round trip and lands on exactly
    # the path DeepSeek has always used: JSON mode, the schema restated in the
    # prompt, and the result validated and repaired on return. Any 400 on the
    # enforced path is worth this, because the alternative is telling someone
    # their model does not work when it does.
    if response.status_code == 400 and enforced:
        return _openai_compatible(provider, model, api_key, system, user_text,
                                  images, schema, max_tokens, effort,
                                  _retrying=_retrying, _plain_json=True)

    if response.status_code >= 400:
        detail = response.text[:200]
        # Said plainly, because the raw body is the provider's own wording and
        # "upstream error" reads like a fault in this application when it is
        # the exact opposite. A reader who knows it is transient waits; a
        # reader who does not goes looking through their own files.
        if response.status_code in TRANSIENT_STATUSES:
            return Completion(
                False,
                error=(f"{provider.label} is having trouble at its end "
                       f"({response.status_code}) and did not recover after "
                       f"{MAX_ATTEMPTS} attempts. Nothing is wrong with your "
                       f"files or your key - try again shortly. It said: "
                       f"{detail}"))
        return Completion(
            False, error=f"{provider.label} returned {response.status_code}: {detail}")

    payload = response.json()
    usage = payload.get("usage") or {}
    choice = (payload.get("choices") or [{}])[0]
    common = dict(
        input_tokens=usage.get("prompt_tokens", 0) or 0,
        output_tokens=usage.get("completion_tokens", 0) or 0,
        # The same number under two names: DeepSeek's own, and the OpenAI
        # shape OpenRouter follows.
        cached_tokens=(usage.get("prompt_cache_hit_tokens")
                       or (usage.get("prompt_tokens_details") or {})
                       .get("cached_tokens")
                       or 0),
        stop_reason=choice.get("finish_reason", "") or "",
    )

    text = ((choice.get("message") or {}).get("content") or "").strip()

    # Truncated? Try once more without the model thinking out loud.
    #
    # On a reasoning model the private reasoning counts against the same output
    # budget as the answer, and it dominates: measured on a display stand,
    # 88,000 characters of reasoning against 38,000 of actual specification.
    # That sits just under the 64,000 token ceiling and crosses it on the
    # longer objects, so an extraction that succeeds on a chair fails on a
    # stand full of products - which reads as an app that randomly breaks.
    #
    # Disabling thinking hands the whole budget to the answer. The result is a
    # little less considered and a great deal better than no result, and this
    # only ever runs after a failure, so nothing that already worked changes.
    if (common.get("stop_reason") in TRUNCATED and not _retrying
            and getattr(model, "reasoning", False)):
        # Carry `_plain_json` through, or a truncation after a schema refusal
        # would go back to the schema the provider has already refused.
        return _openai_compatible(provider, model, api_key, system, user_text,
                                  images, schema, max_tokens, effort="none",
                                  _retrying=True, _plain_json=_plain_json)

    if not text:
        if common.get("stop_reason") in TRUNCATED:
            return Completion(
                False,
                error=(f"The reply was cut off at the token limit after "
                       f"{common.get('output_tokens', 0)} tokens, both with "
                       f"and without extended reasoning. The object may have "
                       f"too many parts to describe in one reply."),
                **common)
        return Completion(False, error="The model returned no content.", **common)

    return _parse(text, **common)


# --------------------------------------------------------------------------- #

# Providers report a hit token ceiling under different names.
TRUNCATED = {"length", "max_tokens", "model_length"}


def _open_structures(text: str) -> tuple[list[str], bool]:
    """Which brackets are still open at the end of `text`, and if it ends mid-string.

    Brackets inside string literals are not structure, so the scan tracks
    quoting and escapes; counting them naively turns a note containing "{" into
    an unclosed object.
    """
    stack: list[str] = []
    in_string = escaped = False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if stack and stack[-1] == ("{" if ch == "}" else "["):
                stack.pop()
    return stack, in_string


def _closed(text: str) -> str:
    """`text` with any open string and brackets closed, in the right order."""
    stack, in_string = _open_structures(text)
    if in_string:
        text += '"'
    return text + "".join("}" if ch == "{" else "]" for ch in reversed(stack))


# How far back a repair may trim. A reply that needs more than this removed is
# not damaged, it is wrong, and salvaging it would be guesswork.
_MAX_TRIM = 4000


def _repair(text: str) -> str:
    """Fix the JSON faults a non-strict provider actually produces.

    A provider with real structured outputs never needs this. DeepSeek's JSON
    mode guarantees JSON-shaped text, not valid JSON, and on a long reply - a
    full part specification runs past fifteen thousand tokens - it
    intermittently emits a trailing comma or stops mid-structure. Discarding a
    reply that cost a minute and a real number of tokens over a stray comma is
    not a reasonable response to that.

    The method is to remove, never to add: drop trailing commas, then walk back
    from the end one value at a time until what remains can be closed into
    valid JSON. So a truncated reply loses its last, incomplete part and keeps
    the rest.

    It never invents a value. A repair that guessed at content would put a
    fabricated dimension into a specification, which is the one thing this
    pipeline exists to prevent. `_coerce` and the schema check then decide
    whether what survived is usable at all.
    """
    base = re.sub(r",\s*([}\]])", r"\1", text.strip())
    if not base:
        return ""

    # Longest first: keep as much of the reply as can be made valid.
    for cut in _candidate_ends(base):
        candidate = _closed(base[:cut])
        try:
            if isinstance(json.loads(candidate), dict):
                return "" if candidate == text else candidate
        except json.JSONDecodeError:
            continue
    return ""


def _candidate_ends(text: str) -> list[int]:
    """Offsets where a *complete* value ends, longest first.

    Only positions immediately after `}`, `]`, `,` or a string's closing quote
    qualify. That excludes the middle of a number, and it is the point of the
    whole function: `"value": 1520` cut to `"value": 152` closes into perfectly
    valid JSON carrying a dimension that is wrong by a factor of ten. A partial
    string is the same hazard with a quieter failure - `"to"` for `"top"` names
    a part that does not exist.

    So a value that cannot be shown to be complete is dropped rather than
    trusted, even though that throws away slightly more of the reply.
    """
    floor = max(0, len(text) - _MAX_TRIM)
    ends: list[int] = []
    in_string = escaped = False

    # Walk forward, recording safe boundaries as they are passed; a backward
    # walk cannot tell an opening quote from a closing one.
    for i, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
                if i + 1 >= floor:
                    ends.append(i + 1)      # just past a closing quote
            continue
        if ch == '"':
            in_string = True
        elif ch in "}],":
            if i + 1 >= floor:
                ends.append(i + 1)

    ends.reverse()                          # longest first
    return ends


def _parse(text: str, **common) -> Completion:
    # Diagnose truncation before trying to parse. A response cut off at the
    # token limit is invalid JSON, and reporting it as "invalid JSON" or
    # "no content" sends you looking at the prompt instead of at max_tokens - # which is exactly the wrong place, and cost me a debugging round.
    if common.get("stop_reason") in TRUNCATED:
        return Completion(
            False,
            error=(f"The reply was cut off at the token limit after "
                   f"{common.get('output_tokens', 0)} tokens. Raise max_tokens "
                   f"or ask for less."),
            raw_text=text[:4000], **common,
        )

    cleaned = text.strip()
    # Some providers wrap JSON in a fenced block despite being asked not to.
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1]
        if cleaned.rstrip().endswith("```"):
            cleaned = cleaned.rstrip()[:-3]

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        repaired = _repair(cleaned)
        try:
            data = json.loads(repaired) if repaired else None
        except json.JSONDecodeError:
            data = None
        if data is None:
            return Completion(
                False,
                error=f"The model did not return valid JSON ({exc.msg}).",
                raw_text=text[:4000],
                **common,
            )

    if not isinstance(data, dict):
        return Completion(False, error="Expected a JSON object.",
                          raw_text=text[:4000], **common)

    return Completion(True, data=data, raw_text=text[:4000], **common)
