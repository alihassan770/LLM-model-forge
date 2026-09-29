"""OpenRouter: a catalogue we do not own, and a key check that actually checks.

Two things here are easy to get wrong in ways that look fine until a user hits
them.

The first is verification. OpenRouter's model list is public, so checking a
key against it succeeds for any string at all - the settings page would report
"connected" for a typo and the first upload would be where the truth arrived.
The key check must go somewhere that authenticates.

The second is staleness. The catalogue is a cached snapshot of someone else's
list, so a model added this morning is absent through no fault of the user.
Refusing it would mean this app's copy of a list overruling the provider that
actually serves it.
"""

from __future__ import annotations

import json

import httpx
import pytest

from forge.ai import openrouter, providers, verify
from forge.vision import client


def raw_model(model_id, **over):
    """One entry shaped like OpenRouter's /models payload."""
    entry = {
        "id": model_id,
        "name": f"Test: {model_id}",
        "context_length": 200_000,
        "architecture": {"input_modalities": ["text", "image"]},
        "pricing": {"prompt": "0.000003", "completion": "0.000015"},
        "top_provider": {"max_completion_tokens": 64_000},
        "supported_parameters": ["reasoning", "structured_outputs"],
    }
    entry.update(over)
    return entry


@pytest.fixture(autouse=True)
def _cold_cache(monkeypatch):
    """Never let a test see another test's catalogue, or the real network."""
    monkeypatch.setattr(openrouter, "_models", [])
    monkeypatch.setattr(openrouter, "_fetched_at", 0.0)
    monkeypatch.setattr(openrouter, "_failed_at", 0.0)
    monkeypatch.setattr(openrouter, "_error", "")


def serve(monkeypatch, entries, status=200):
    calls = {"n": 0}

    def fake_get(url, timeout=None, headers=None):
        calls["n"] += 1
        return httpx.Response(
            status, json={"data": entries},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(openrouter.httpx, "get", fake_get)
    return calls


# --------------------------------------------------------------------------- #
# Mapping OpenRouter's shape onto ours
# --------------------------------------------------------------------------- #

def test_pricing_is_converted_from_per_token_to_per_million():
    """OpenRouter quotes USD per token as a string; we store per million.

    Getting this wrong by a factor of a million would put $0.000003 on an
    invoice estimate and nobody would notice until they read a bill.
    """
    model = openrouter._to_model(raw_model("lab/m"))
    assert model.price_in == 3.0
    assert model.price_out == 15.0
    assert providers.estimate_cost(model, 1_000_000, 100_000) == pytest.approx(4.5)


def test_router_models_with_negative_pricing_do_not_report_a_negative_cost():
    """A negative price means "depends what it routes to", not "we pay you"."""
    model = openrouter._to_model(
        raw_model("openrouter/auto", pricing={"prompt": "-1", "completion": "-1"})
    )
    assert model.price_in == 0.0
    assert model.price_out == 0.0
    assert "not an estimate" in model.note


def test_a_model_with_no_completion_ceiling_gets_one_every_model_can_honour():
    """Asking for more output than the model allows is a 400.

    Seven of the live entries publish no `max_completion_tokens`. Falling back
    to the context window would ask a 200k-context model for 200k of output.
    """
    model = openrouter._to_model(
        raw_model("lab/m", top_provider={"max_completion_tokens": None})
    )
    assert model.max_output == openrouter.FALLBACK_MAX_OUTPUT
    assert model.max_output < model.context


def test_an_advertised_ceiling_of_a_million_tokens_is_not_passed_on():
    """OpenRouter reserves credit for prompt + max_tokens before it runs.

    Several models here advertise 900k and even 1.8M output tokens. Sending
    that through got the request refused with a 402 - "you requested up to
    934585 tokens, but can only afford 220131" - for an extraction that would
    have produced a few thousand tokens and cost cents. The ceiling we name
    has to be one we might actually reach.
    """
    model = openrouter._to_model(
        raw_model("lab/huge", context_length=2_000_000,
                  top_provider={"max_completion_tokens": 1_800_000})
    )
    assert model.max_output == openrouter.MAX_OUTPUT_CEILING


def test_the_cap_never_raises_a_small_models_ceiling():
    """A cap is a maximum, not a target. Asking a 16k model for 64k is a 400."""
    model = openrouter._to_model(
        raw_model("lab/small", context_length=16_384,
                  top_provider={"max_completion_tokens": 8_192})
    )
    assert model.max_output == 8_192


def test_no_model_in_the_live_shape_can_exceed_the_ceiling(monkeypatch):
    serve(monkeypatch, [
        raw_model("lab/huge", top_provider={"max_completion_tokens": 1_800_000}),
        raw_model("lab/none", top_provider={}),
        raw_model("lab/normal"),
    ])
    for model in openrouter.catalog():
        assert model.max_output <= openrouter.MAX_OUTPUT_CEILING
        assert model.max_output <= model.context


def test_text_only_models_are_marked_and_explained():
    model = openrouter._to_model(
        raw_model("lab/text", architecture={"input_modalities": ["text"]})
    )
    assert model.vision is False
    assert "cannot read photos" in model.note


def test_capabilities_are_read_from_supported_parameters():
    plain = openrouter._to_model(raw_model("lab/plain", supported_parameters=[]))
    assert plain.reasoning is False
    assert plain.structured is False
    assert plain.reasoning_efforts == ()

    full = openrouter._to_model(raw_model("lab/full"))
    assert full.reasoning is True
    assert full.structured is True
    assert full.default_effort in full.reasoning_efforts


# --------------------------------------------------------------------------- #
# The cache
# --------------------------------------------------------------------------- #

def test_cached_never_makes_a_request(monkeypatch):
    """The extraction path and page renders call this. It must not block."""
    def explode(*a, **k):  # pragma: no cover - the point is it is not called
        raise AssertionError("cached() must not touch the network")

    monkeypatch.setattr(openrouter.httpx, "get", explode)
    assert openrouter.cached() == []


def test_catalog_is_fetched_once_and_then_reused(monkeypatch):
    calls = serve(monkeypatch, [raw_model("lab/a"), raw_model("lab/b")])
    assert len(openrouter.catalog()) == 2
    assert len(openrouter.catalog()) == 2
    assert calls["n"] == 1, "a warm cache must not re-fetch"

    openrouter.catalog(force=True)
    assert calls["n"] == 2, "Refresh must ignore the cache"


def test_a_failed_fetch_keeps_the_previous_catalogue(monkeypatch):
    """A stale list is far more useful than an empty one."""
    serve(monkeypatch, [raw_model("lab/a")])
    openrouter.catalog()

    def boom(*a, **k):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(openrouter.httpx, "get", boom)
    still_there = openrouter.catalog(force=True)

    assert [m.id for m in still_there] == ["lab/a"]
    assert openrouter.status()["error"]


def test_an_outage_is_not_retried_on_every_page_load(monkeypatch):
    """Without a backoff, a cold cache costs every settings render a timeout."""
    calls = {"n": 0}

    def boom(*a, **k):
        calls["n"] += 1
        raise httpx.ConnectError("down")

    monkeypatch.setattr(openrouter.httpx, "get", boom)

    openrouter.catalog()
    openrouter.catalog()
    openrouter.catalog()
    assert calls["n"] == 1, "one attempt, then leave it alone for a while"

    openrouter.catalog(force=True)
    assert calls["n"] == 2, "an explicit refresh still tries"


def test_vision_models_are_listed_before_text_only_ones(monkeypatch):
    serve(monkeypatch, [
        raw_model("lab/text", architecture={"input_modalities": ["text"]},
                  pricing={"prompt": "0", "completion": "0"}),
        raw_model("lab/sees"),
    ])
    assert [m.id for m in openrouter.catalog()][0] == "lab/sees"


# --------------------------------------------------------------------------- #
# A model the catalogue has not caught up with
# --------------------------------------------------------------------------- #

def test_an_unlisted_model_id_is_usable(monkeypatch):
    """The catalogue is a snapshot. It must not overrule the live provider.

    A model released this morning, or one private to this account, is absent
    through no fault of the person typing it.
    """
    serve(monkeypatch, [raw_model("lab/a")])
    openrouter.catalog()

    resolved = providers.resolve("openrouter", "lab/released-today")
    assert resolved is not None
    _, model = resolved
    assert model.id == "lab/released-today"
    assert model.vision, "assume it can see, or a new vision model is unusable"
    assert "unknown" in model.note


def test_ensure_fetches_when_empty_but_tolerates_stale(monkeypatch):
    """`ensure` guards the extraction path, and the two cases differ sharply.

    Stale is a slightly wrong price. Empty means `synthesize` invents the
    model, and an invented model is wrong in ways that break the run: it
    carries the 32k fallback ceiling instead of the real one, reasoning off
    so the truncation retry cannot fire, and no price.
    """
    calls = serve(monkeypatch, [raw_model("lab/a")])

    openrouter.ensure()
    assert calls["n"] == 1, "empty cache must fetch"

    openrouter.ensure()
    assert calls["n"] == 1, "a populated cache is enough, even if stale"

    # Stale but present: still no fetch, because a slightly old price cannot
    # break an extraction the way an invented model can.
    monkeypatch.setattr(openrouter, "_fetched_at", 1.0)
    openrouter.ensure()
    assert calls["n"] == 1


def test_a_cold_process_does_not_invent_the_configured_model(monkeypatch):
    """The regression, end to end.

    A freshly started server had never fetched the catalogue, so every
    extraction resolved its model through `synthesize()`: a 32k ceiling
    instead of 64k, reasoning off, price zero. Runs failed on a token limit
    that was never real, and the settings page looked correct throughout.
    """
    serve(monkeypatch, [raw_model("lab/real",
                                  top_provider={"max_completion_tokens": 64_000})])
    provider = providers.PROVIDERS["openrouter"]

    invented = provider.model("lab/real")
    assert invented.max_output == openrouter.FALLBACK_MAX_OUTPUT
    assert invented.reasoning is False, "the state that broke the retry"

    openrouter.ensure()

    real = provider.model("lab/real")
    assert real.max_output == 64_000
    assert real.reasoning is True
    assert real.price_in > 0, "a real price, so the cost is not logged as zero"


def test_a_cold_cache_still_yields_a_usable_default():
    """Choosing OpenRouter before the list arrives must not crash the page."""
    default = providers.PROVIDERS["openrouter"].default_model
    assert default.id == providers.DEFAULT_DYNAMIC_MODEL
    assert default.vision


# --------------------------------------------------------------------------- #
# The request body
# --------------------------------------------------------------------------- #

SCHEMA = {
    "type": "object",
    "properties": {"a": {"type": "string"}},
    "required": ["a"],
    "additionalProperties": False,
}


def body_for(model_id, entries, effort="high", plain_json=False):
    provider = providers.PROVIDERS["openrouter"]
    model = provider.model(model_id)
    # Mirrors the decision `_openai_compatible` makes before building a body.
    enforced = model.structured and not plain_json
    return client._chat_body(provider, model, "sys",
                             [{"type": "text", "text": "hi"}],
                             64_000, effort, SCHEMA, enforced)


def test_a_schema_capable_model_is_held_to_the_schema(monkeypatch):
    """Enforced beats asked-for: a missing dimension should fail, not slip by."""
    serve(monkeypatch, [raw_model("lab/strict")])
    openrouter.catalog()

    body = body_for("lab/strict", None)
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["response_format"]["json_schema"]["schema"] == SCHEMA


def test_a_model_without_structured_outputs_falls_back_to_json_mode(monkeypatch):
    serve(monkeypatch, [raw_model("lab/loose", supported_parameters=[])])
    openrouter.catalog()
    assert body_for("lab/loose", None)["response_format"] == {"type": "json_object"}


def test_the_schema_is_only_restated_in_the_prompt_when_it_is_not_enforced(monkeypatch):
    """Restating a schema the provider already enforces is thousands of wasted
    prompt tokens on every extraction."""
    serve(monkeypatch, [
        raw_model("lab/strict"),
        raw_model("lab/loose", supported_parameters=[]),
    ])
    openrouter.catalog()

    provider = providers.PROVIDERS["openrouter"]
    sent = {}

    def capture(url, headers, body):
        sent.update(body)
        return None, "unreachable"

    monkeypatch.setattr(client, "_post_with_retry", capture)

    for model_id, expect_schema_in_prompt in (("lab/strict", False),
                                              ("lab/loose", True)):
        sent.clear()
        client._openai_compatible(provider, provider.model(model_id), "k",
                                  "sys", "describe it", [], SCHEMA, 1000)
        prompt = sent["messages"][-1]["content"][-1]["text"]
        assert ("JSON Schema" in prompt) is expect_schema_in_prompt


def test_a_refused_schema_falls_back_instead_of_failing(monkeypatch):
    """"Supports structured outputs" is not a promise about *our* schema.

    OpenRouter routes to providers that compile the schema into a grammar, and
    ours - 93 properties, 17 enums - is large enough that Anthropic's compiler
    refuses it: "The compiled grammar is too large". That made every Claude
    model unusable through OpenRouter. The schema is a nicety; the extraction
    is the point, so a refusal drops to the plain-JSON path DeepSeek has
    always used rather than failing.
    """
    serve(monkeypatch, [raw_model("lab/strict")])
    openrouter.catalog()
    provider = providers.PROVIDERS["openrouter"]

    seen = []

    def responder(url, headers, body):
        seen.append(body["response_format"]["type"])
        if body["response_format"]["type"] == "json_schema":
            return httpx.Response(
                400,
                json={"error": {"message": "Provider returned error",
                                "metadata": {"raw": "The compiled grammar is "
                                                    "too large"}}},
                request=httpx.Request("POST", url),
            ), ""
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"ok": true}'},
                               "finish_reason": "stop"}],
                  "usage": {"prompt_tokens": 10, "completion_tokens": 4}},
            request=httpx.Request("POST", url),
        ), ""

    monkeypatch.setattr(client, "_post_with_retry", responder)
    result = client._openai_compatible(provider, provider.model("lab/strict"),
                                       "k", "sys", "go", [], SCHEMA, 1000)

    assert seen == ["json_schema", "json_object"], "must retry without the schema"
    assert result.ok is True
    assert result.data == {"ok": True}
    assert "grammar" not in (result.error or ""), "the user must not see this"


def test_the_fallback_restates_the_schema_in_the_prompt(monkeypatch):
    """Dropping the enforced schema without restating it would leave the model
    with no description of the shape at all."""
    serve(monkeypatch, [raw_model("lab/strict")])
    openrouter.catalog()
    provider = providers.PROVIDERS["openrouter"]

    prompts = []

    def responder(url, headers, body):
        prompts.append(body["messages"][-1]["content"][-1]["text"])
        if body["response_format"]["type"] == "json_schema":
            return httpx.Response(400, json={"error": {"message": "nope"}},
                                  request=httpx.Request("POST", url)), ""
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "{}"},
                               "finish_reason": "stop"}], "usage": {}},
            request=httpx.Request("POST", url)), ""

    monkeypatch.setattr(client, "_post_with_retry", responder)
    client._openai_compatible(provider, provider.model("lab/strict"), "k",
                              "sys", "describe it", [], SCHEMA, 1000)

    assert "JSON Schema" not in prompts[0], "enforced: no need to restate it"
    assert "JSON Schema" in prompts[1], "fallback: the model needs the shape"


def test_a_non_schema_400_is_not_retried_forever(monkeypatch):
    """The fallback runs once. A second 400 is reported, not retried."""
    serve(monkeypatch, [raw_model("lab/strict")])
    openrouter.catalog()
    provider = providers.PROVIDERS["openrouter"]
    calls = {"n": 0}

    def always_400(url, headers, body):
        calls["n"] += 1
        return httpx.Response(400, text="something else entirely",
                              request=httpx.Request("POST", url)), ""

    monkeypatch.setattr(client, "_post_with_retry", always_400)
    result = client._openai_compatible(provider, provider.model("lab/strict"),
                                       "k", "sys", "go", [], SCHEMA, 1000)

    assert calls["n"] == 2, "one enforced attempt, one fallback, then stop"
    assert result.ok is False


def test_reasoning_uses_openrouters_spelling_not_deepseeks(monkeypatch):
    """Sending DeepSeek's `thinking` block to OpenRouter would be ignored."""
    serve(monkeypatch, [raw_model("lab/thinks")])
    openrouter.catalog()

    body = body_for("lab/thinks", None, effort="high")
    assert body["reasoning"] == {"effort": "high", "exclude": True}
    assert "thinking" not in body

    off = body_for("lab/thinks", None, effort="none")
    assert off["reasoning"] == {"effort": "none"}


def test_deepseek_bodies_are_unchanged():
    """OpenRouter was added alongside DeepSeek, not on top of it."""
    provider = providers.PROVIDERS["deepseek"]
    model = provider.model("deepseek-flash")
    body = client._chat_body(provider, model, "sys",
                             [{"type": "text", "text": "hi"}],
                             64_000, "high", SCHEMA)
    assert body["response_format"] == {"type": "json_object"}
    assert body["thinking"] == {"type": "enabled", "reasoning_effort": "high"}
    assert "reasoning" not in body


def test_running_out_of_credit_is_explained_rather_than_dumped(monkeypatch):
    """The raw 402 body reads like a bug in this app. It is an empty account."""
    provider = providers.PROVIDERS["openrouter"]
    model = openrouter.synthesize("lab/a")

    def refuse(url, headers, body):
        return httpx.Response(
            402,
            json={"error": {"message": "This request requires more credits, "
                                       "or fewer max_tokens."}},
            request=httpx.Request("POST", url),
        ), ""

    monkeypatch.setattr(client, "_post_with_retry", refuse)
    result = client._openai_compatible(provider, model, "k", "sys", "go", [],
                                       SCHEMA, 64_000)

    assert result.ok is False
    assert "credit" in result.error
    assert "max_tokens" not in result.error, "speak to the user, not the API"


def test_attribution_headers_go_only_to_openrouter():
    assert "X-Title" in client._headers(providers.PROVIDERS["openrouter"], "k")
    assert "X-Title" not in client._headers(providers.PROVIDERS["deepseek"], "k")


# --------------------------------------------------------------------------- #
# Verifying the key
# --------------------------------------------------------------------------- #

def respond(monkeypatch, status, payload=None):
    def fake_get(url, headers=None, timeout=None):
        assert url.endswith("/key"), (
            "the model list is public, so verifying against it would pass any "
            f"string at all - got {url}"
        )
        return httpx.Response(status, json=payload or {},
                              request=httpx.Request("GET", url))

    monkeypatch.setattr(verify.httpx, "get", fake_get)


def test_a_bad_key_is_rejected(monkeypatch):
    respond(monkeypatch, 401)
    result = verify.verify(providers.PROVIDERS["openrouter"], "sk-or-v1-wrong",
                           "lab/a")
    assert result.ok is False
    assert "rejected" in result.message


def test_a_good_key_reports_the_remaining_credit(monkeypatch):
    respond(monkeypatch, 200, {"data": {"limit_remaining": 12.5}})
    result = verify.verify(providers.PROVIDERS["openrouter"], "sk-or-v1-ok",
                           "lab/a")
    assert result.ok is True
    assert "12.50" in result.message


def test_a_valid_key_with_no_credit_left_is_a_failure(monkeypatch):
    """It authenticates and it cannot run an extraction. Saying "connected"
    would be true and useless."""
    respond(monkeypatch, 200, {"data": {"limit_remaining": 0}})
    result = verify.verify(providers.PROVIDERS["openrouter"], "sk-or-v1-broke",
                           "lab/a")
    assert result.ok is False
    assert "credit" in result.message


def test_a_text_only_model_is_caught_at_verification(monkeypatch, ):
    """Better here than on the first upload."""
    serve(monkeypatch, [raw_model("lab/text",
                                  architecture={"input_modalities": ["text"]})])
    openrouter.catalog()
    respond(monkeypatch, 200, {"data": {"limit_remaining": 5}})

    result = verify.verify(providers.PROVIDERS["openrouter"], "sk-or-v1-ok",
                           "lab/text")
    assert result.ok is False
    assert "cannot read images" in result.message


def test_a_model_missing_from_the_catalogue_is_a_warning_not_a_failure(monkeypatch):
    serve(monkeypatch, [raw_model("lab/a")])
    openrouter.catalog()
    respond(monkeypatch, 200, {"data": {"limit_remaining": 5}})

    result = verify.verify(providers.PROVIDERS["openrouter"], "sk-or-v1-ok",
                           "lab/brand-new")
    assert result.ok is True
    assert "not in the fetched catalogue" in result.message
