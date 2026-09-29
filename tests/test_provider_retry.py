"""Transient upstream failures must not destroy a two-minute extraction.

An extraction is the most expensive thing this application does: one to two
minutes of generation producing tens of thousands of tokens. Losing all of it
to a single `{"error":{"message":"upstream error"}}` - which is the provider
briefly failing, not anything wrong with the request - is the worst trade
available, and it is what happened before these retries existed.
"""

from __future__ import annotations

import httpx
import pytest

from forge.vision import client


class FakeResponse:
    def __init__(self, status_code: int, text: str = "", headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}


@pytest.fixture(autouse=True)
def _no_real_waiting(monkeypatch):
    """Keep the backoff arithmetic honest without paying for it."""
    slept: list[float] = []
    monkeypatch.setattr(client.time, "sleep", slept.append)
    return slept


def post_returning(monkeypatch, *outcomes):
    """Stub httpx.post with one outcome per call; exceptions are raised."""
    calls = {"n": 0}

    def fake_post(url, headers=None, json=None, timeout=None):
        outcome = outcomes[min(calls["n"], len(outcomes) - 1)]
        calls["n"] += 1
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(client.httpx, "post", fake_post)
    return calls


def test_a_transient_upstream_error_is_retried_and_succeeds(monkeypatch):
    """The exact failure that was reported: one bad 502, then a good reply."""
    calls = post_returning(
        monkeypatch,
        FakeResponse(502, '{"error":{"message":"upstream error"}}'),
        FakeResponse(200, '{"ok":true}'),
    )
    response, detail = client._post_with_retry("http://x", {}, {})
    assert calls["n"] == 2
    assert response.status_code == 200
    assert detail == ""


def test_a_permanent_failure_is_not_retried(monkeypatch):
    """Repeating a rejected key wastes a minute and changes nothing."""
    calls = post_returning(monkeypatch, FakeResponse(401, "bad key"))
    response, _ = client._post_with_retry("http://x", {}, {})
    assert calls["n"] == 1
    assert response.status_code == 401


def test_a_bad_request_is_not_retried(monkeypatch):
    calls = post_returning(monkeypatch, FakeResponse(400, "malformed"))
    response, _ = client._post_with_retry("http://x", {}, {})
    assert calls["n"] == 1
    assert response.status_code == 400


def test_retrying_gives_up_rather_than_hammering_a_provider_that_is_down(monkeypatch):
    calls = post_returning(monkeypatch, FakeResponse(503, "down"))
    response, detail = client._post_with_retry("http://x", {}, {})
    assert calls["n"] == client.MAX_ATTEMPTS
    assert response.status_code == 503
    assert "503" in detail


def test_a_dropped_connection_is_treated_like_a_502(monkeypatch):
    """A read timeout mid-generation is the same class of problem."""
    calls = post_returning(
        monkeypatch,
        httpx.ReadTimeout("timed out"),
        FakeResponse(200, "{}"),
    )
    response, _ = client._post_with_retry("http://x", {}, {})
    assert calls["n"] == 2
    assert response.status_code == 200


def test_a_connection_that_never_comes_back_reports_the_reason(monkeypatch):
    calls = post_returning(monkeypatch, httpx.ConnectError("refused"))
    response, detail = client._post_with_retry("http://x", {}, {})
    assert calls["n"] == client.MAX_ATTEMPTS
    assert response is None
    assert "ConnectError" in detail


def test_the_providers_own_retry_after_is_honoured(monkeypatch, _no_real_waiting):
    post_returning(
        monkeypatch,
        FakeResponse(429, "slow down", headers={"retry-after": "7"}),
        FakeResponse(200, "{}"),
    )
    client._post_with_retry("http://x", {}, {})
    assert _no_real_waiting == [7.0]


def test_an_absurd_retry_after_does_not_stall_the_request(monkeypatch,
                                                          _no_real_waiting):
    """A request already waits ten minutes; it must not also wait an hour."""
    post_returning(
        monkeypatch,
        FakeResponse(429, "slow down", headers={"retry-after": "3600"}),
        FakeResponse(200, "{}"),
    )
    client._post_with_retry("http://x", {}, {})
    assert _no_real_waiting == [30.0]


def test_a_nonsense_retry_after_falls_back_to_the_normal_backoff(monkeypatch,
                                                                 _no_real_waiting):
    post_returning(
        monkeypatch,
        FakeResponse(503, "x", headers={"retry-after": "in a bit"}),
        FakeResponse(200, "{}"),
    )
    client._post_with_retry("http://x", {}, {})
    assert _no_real_waiting == [client.RETRY_BACKOFF_SECONDS[0]]


def test_the_message_for_a_transient_failure_says_it_is_not_the_users_fault():
    """'upstream error' reads like a bug here when it is the opposite.

    Someone who knows the failure is the provider's waits a minute. Someone
    shown the raw body goes looking through their own files and their own key.
    """
    from forge.ai.providers import PROVIDERS

    provider = PROVIDERS["deepseek"]
    model = next(m for m in provider.models if m.vision)

    import forge.vision.client as c
    calls = {"n": 0}

    def fake_post(url, headers=None, json=None, timeout=None):
        calls["n"] += 1
        return FakeResponse(502, '{"error":{"message":"upstream error"}}')

    original_post, original_sleep = c.httpx.post, c.time.sleep
    c.httpx.post = fake_post
    c.time.sleep = lambda _: None
    try:
        result = c._openai_compatible(
            provider, model, "key", "system", "text", [], {"type": "object"},
            1000,
        )
    finally:
        c.httpx.post, c.time.sleep = original_post, original_sleep

    assert not result.ok
    assert calls["n"] == c.MAX_ATTEMPTS
    lowered = result.error.lower()
    assert "nothing is wrong with your files" in lowered
    assert "try again" in lowered


# --------------------------------------------------------------------------- #
# Truncation: retry without the model thinking out loud
# --------------------------------------------------------------------------- #

def _reply(content, finish="stop", out=1000):
    return {"choices": [{"message": {"content": content},
                         "finish_reason": finish}],
            "usage": {"completion_tokens": out, "prompt_tokens": 100}}


class _Json(FakeResponse):
    def __init__(self, payload, status=200):
        super().__init__(status, "")
        self._payload = payload

    def json(self):
        return self._payload


def test_a_truncated_reply_is_retried_without_reasoning(monkeypatch):
    """Reasoning and the answer share one output budget, and reasoning wins.

    Measured on a display stand: 88,000 characters of private reasoning against
    38,000 of actual specification, against a 64,000 token ceiling. Long
    objects cross it, so the same app succeeds on a chair and fails on a stand
    - which reads as random breakage. Turning thinking off hands the whole
    budget to the answer.
    """
    from forge.ai.providers import PROVIDERS
    import forge.vision.client as c

    provider = PROVIDERS["deepseek"]
    model = next(m for m in provider.models if m.vision)
    assert model.reasoning, "this retry only applies to a reasoning model"

    bodies = []

    def fake_post(url, headers=None, json=None, timeout=None):
        bodies.append(json)
        if len(bodies) == 1:
            return _Json(_reply('{"partial":', finish="length", out=64000))
        return _Json(_reply('{"object":{"category":"chair"}}'))

    monkeypatch.setattr(c.httpx, "post", fake_post)
    monkeypatch.setattr(c.time, "sleep", lambda _: None)

    result = c._openai_compatible(provider, model, "key", "sys", "text", [],
                                  {"type": "object"}, 64000, effort="high")

    assert len(bodies) == 2, "a truncated reply must be retried once"
    assert bodies[0]["thinking"] == {"type": "enabled", "reasoning_effort": "high"}
    assert bodies[1]["thinking"] == {"type": "disabled"}
    assert result.ok and result.data == {"object": {"category": "chair"}}


def test_the_retry_happens_only_once(monkeypatch):
    """Two truncations means the object is genuinely too big to describe."""
    from forge.ai.providers import PROVIDERS
    import forge.vision.client as c

    provider = PROVIDERS["deepseek"]
    model = next(m for m in provider.models if m.vision)
    calls = {"n": 0}

    def fake_post(url, headers=None, json=None, timeout=None):
        calls["n"] += 1
        return _Json(_reply("", finish="length", out=64000))

    monkeypatch.setattr(c.httpx, "post", fake_post)
    monkeypatch.setattr(c.time, "sleep", lambda _: None)

    result = c._openai_compatible(provider, model, "key", "sys", "text", [],
                                  {"type": "object"}, 64000, effort="high")
    assert calls["n"] == 2
    assert not result.ok
    assert "too many parts" in result.error


def test_a_reply_that_fits_is_never_retried(monkeypatch):
    from forge.ai.providers import PROVIDERS
    import forge.vision.client as c

    provider = PROVIDERS["deepseek"]
    model = next(m for m in provider.models if m.vision)
    calls = {"n": 0}

    def fake_post(url, headers=None, json=None, timeout=None):
        calls["n"] += 1
        return _Json(_reply('{"object":{"category":"table"}}'))

    monkeypatch.setattr(c.httpx, "post", fake_post)
    result = c._openai_compatible(provider, model, "key", "sys", "text", [],
                                  {"type": "object"}, 64000, effort="high")
    assert calls["n"] == 1
    assert result.ok
