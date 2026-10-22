"""The bench's pool client, over a real loopback listener and a mock transport.

The happy paths and every refusal run against an :mod:`http.server` listener
this test opens on loopback, so the client is exercised over real sockets with
real status codes rather than against a hand-written double. No vendor is
contacted and nothing is billed (ADR-0016 Decision 4: no spend yet).

What the client must do that the Ollama client does not:

- name the pinned identity on every request and refuse a pool that advertises
  a different one **before a prompt is sent** (ADR-0016 Decision 1 item 1, T1);
- discard an answer that echoes a different identity or model;
- refuse an unattested pool (Decision 3 item 3);
- classify ``rate_limited`` and ``insufficient_quota`` apart (Decision 3
  item 4), never as a fast success.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from creek.models import PrivacyTier
from creek_mcp.bench.fake import FakePool
from creek_mcp.bench.metadata import UNKNOWN
from creek_mcp.bench.ollama_client import DigestMismatchError
from creek_mcp.bench.outcome import (
    ContextOverflowError,
    Outcome,
    PoolQuotaExhaustedError,
    PoolRateLimitedError,
    ProviderUnavailableError,
    classify_outcome,
)
from creek_mcp.bench.pool_client import (
    POOL_GENERATE_PATH,
    POOL_MODELS_PATH,
    BenchPoolClient,
    PoolAttestationError,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

_IDENTITY = "tinfoil/gpt-oss-120b@v3"
_MODEL = "gpt-oss-120b"
_DIGEST = "sha256:" + "d" * 64
_OTHER_DIGEST = "sha256:" + "e" * 64
_PROMPT = "rules\n\nENTRY:\nalpha beta gamma delta"
_UNROUTABLE = "http://127.0.0.1:9"


def _pool(**overrides: Any) -> FakePool:
    """Build a fake pool serving the pinned identity, overridden by kwargs."""
    fields: dict[str, Any] = {
        "identity": _IDENTITY,
        "model_tag": _MODEL,
        "measurement_digest": _DIGEST,
    }
    fields.update(overrides)
    return FakePool(**fields)


def _client(pool: FakePool, **overrides: Any) -> BenchPoolClient:
    """Build a client on *pool*'s in-process transport."""
    fields: dict[str, Any] = {
        "identity": _IDENTITY,
        "num_ctx": 2048,
        "num_predict": 128,
        "transport": pool.transport(),
    }
    fields.update(overrides)
    return BenchPoolClient("http://pool.test", _MODEL, **fields)


class _Listener(BaseHTTPRequestHandler):
    """A loopback HTTP handler delegating to the test's :class:`FakePool`."""

    pool: FakePool

    def do_GET(self) -> None:
        """Answer a listing read."""
        self._answer(None)

    def do_POST(self) -> None:
        """Answer a generation request."""
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        self._answer(json.loads(raw) if raw else None)

    def _answer(self, body: dict[str, Any] | None) -> None:
        """Write the fake pool's answer for this request."""
        status, payload = type(self).pool.answer(self.command, self.path, body)
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: Any) -> None:
        """Stay quiet: a request line would carry the pinned identity."""


@pytest.fixture
def served() -> Iterator[tuple[FakePool, str]]:
    """Serve a fake pool on loopback; yield it with its base URL."""
    pool = _pool()
    handler = type("_Bound", (_Listener,), {"pool": pool})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield pool, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _live_client(base_url: str, **overrides: Any) -> BenchPoolClient:
    """Build a client dialling *base_url* over real sockets."""
    fields: dict[str, Any] = {
        "identity": _IDENTITY,
        "num_ctx": 2048,
        "num_predict": 128,
    }
    fields.update(overrides)
    return BenchPoolClient(base_url, _MODEL, **fields)


def test_loopback_listener_verifies_identity_then_generates(
    served: tuple[FakePool, str],
) -> None:
    """Over real sockets: the identity is verified, then a prompt answered."""
    pool, base_url = served
    client = _live_client(base_url, measurement_digest=_DIGEST)
    pinned = client.verify_identity()
    assert pinned.digest == _DIGEST
    assert pinned.license_id == UNKNOWN
    assert pool.generate_calls == 0
    note = json.loads(client.generate(_PROMPT, num_predict=64))["notes"][0]
    assert note["quote"] == "alpha beta gamma"
    assert pool.generate_calls == 1


def test_loopback_identity_mismatch_sends_no_prompt(
    served: tuple[FakePool, str],
) -> None:
    """A pool advertising another identity is refused before any generate."""
    pool, base_url = served
    pool.served_identity = "other/model@v1"
    client = _live_client(base_url)
    with pytest.raises(DigestMismatchError):
        client.verify_identity()
    assert pool.generate_calls == 0


def test_loopback_quota_exhausted_is_not_a_success(
    served: tuple[FakePool, str],
) -> None:
    """A 429 ``insufficient_quota`` raises over real sockets, never returns."""
    pool, base_url = served
    pool.refusal_code = "insufficient_quota"
    with pytest.raises(PoolQuotaExhaustedError):
        _live_client(base_url).generate(_PROMPT, num_predict=16)


def test_request_names_the_pinned_identity_and_caps_output() -> None:
    """Every generation request carries the identity and the capped ceiling."""
    pool = _pool()
    _client(pool).generate(_PROMPT, num_predict=4096)
    body = pool.requests[-1]
    assert body["identity"] == _IDENTITY
    assert body["model"] == _MODEL
    assert body["max_tokens"] == 128
    assert body["stream"] is False
    assert pool.paths == [POOL_GENERATE_PATH]


def test_listing_path_is_read_once_per_run() -> None:
    """The identity listing is resolved once and then reused."""
    pool = _pool()
    client = _client(pool)
    assert client.verify_identity() == client.verify_identity()
    assert pool.paths == [POOL_MODELS_PATH]
    assert pool.listing_calls == 1


def test_unattested_pool_is_refused_before_a_prompt() -> None:
    """An unverified attestation refuses the run with nothing sent."""
    pool = _pool(attested=False)
    with pytest.raises(PoolAttestationError):
        _client(pool).verify_identity()
    assert pool.generate_calls == 0


def test_measurement_digest_mismatch_is_a_digest_mismatch() -> None:
    """A pool attesting another measurement set is refused by the pin."""
    pool = _pool()
    client = _client(pool, measurement_digest=_OTHER_DIGEST)
    with pytest.raises(DigestMismatchError):
        client.verify_identity()
    assert pool.generate_calls == 0


def test_verify_pinned_refuses_a_different_expected_digest() -> None:
    """``Backend.pin`` refuses when the attested set is not the pinned one."""
    client = _client(_pool())
    assert client.verify_pinned(_DIGEST).digest == _DIGEST
    with pytest.raises(DigestMismatchError):
        _client(_pool()).verify_pinned(_OTHER_DIGEST)


def test_pool_that_does_not_serve_the_model_is_refused() -> None:
    """A listing without the pinned model is refused, not silently accepted."""
    pool = _pool(model_tag="something-else")
    with pytest.raises(DigestMismatchError):
        _client(pool).verify_identity()


@pytest.mark.parametrize("field", ["identity", "model"])
def test_response_echoing_another_identity_is_discarded(field: str) -> None:
    """An answer naming a different identity or model never reaches a caller."""
    pool = _pool(**{f"echo_{field}": "impostor"})
    with pytest.raises(DigestMismatchError):
        _client(pool).generate(_PROMPT, num_predict=16)


def test_rate_limited_and_quota_are_classified_apart() -> None:
    """Both 429 codes are errors, and they are not the same error."""
    limited = _pool(refusal_code="rate_limited")
    exhausted = _pool(refusal_code="insufficient_quota")
    with pytest.raises(PoolRateLimitedError) as first:
        _client(limited).generate(_PROMPT, num_predict=16)
    with pytest.raises(PoolQuotaExhaustedError) as second:
        _client(exhausted).generate(_PROMPT, num_predict=16)
    assert classify_outcome(first.value) is Outcome.RATE_LIMITED
    assert classify_outcome(second.value) is Outcome.INSUFFICIENT_QUOTA


def test_unlabelled_429_is_treated_as_rate_limited() -> None:
    """A 429 with no code is admission pressure, not a quota verdict."""
    pool = _pool(refusal_code="unknown-code")
    with pytest.raises(PoolRateLimitedError):
        _client(pool).generate(_PROMPT, num_predict=16)


def test_server_error_keeps_its_http_status_error() -> None:
    """A 5xx is raised as an HTTP error so the OOM markers still classify it."""
    pool = _pool(status=503)
    with pytest.raises(httpx.HTTPStatusError):
        _client(pool).generate(_PROMPT, num_predict=16)


def test_unreachable_pool_is_provider_unavailable() -> None:
    """A refused connection is reported as the provider being unavailable."""
    client = _live_client(_UNROUTABLE)
    with pytest.raises(ProviderUnavailableError):
        client.verify_identity()


@pytest.mark.parametrize("reported", [1920, 2048])
def test_prompt_that_filled_the_window_is_a_context_overflow(reported: int) -> None:
    """A reported prompt size leaving no room for the output is an overflow."""
    pool = _pool(prompt_tokens=reported)
    with pytest.raises(ContextOverflowError):
        _client(pool).generate(_PROMPT, num_predict=128)


def test_prompt_that_left_room_is_not_an_overflow() -> None:
    """A prompt well inside the window answers normally."""
    pool = _pool(prompt_tokens=100)
    assert _client(pool).generate(_PROMPT, num_predict=128)


def test_malformed_listing_is_refused() -> None:
    """A listing that is not an object cannot establish an identity."""
    pool = _pool(listing_payload=["not", "an", "object"])
    with pytest.raises(DigestMismatchError):
        _client(pool).verify_identity()


def test_malformed_generation_answer_yields_no_text() -> None:
    """An answer with no usable text is empty, never a partial object."""
    pool = _pool(drop_response_text=True)
    assert _client(pool).generate(_PROMPT, num_predict=16) == ""


def test_factory_builds_a_local_declared_callable_after_verifying() -> None:
    """The built callable declares itself local and the identity is checked."""
    pool = _pool()
    llm = _client(pool).factory()(PrivacyTier.OPEN, max_tokens=32)
    assert llm.is_cloud is False  # type: ignore[attr-defined]
    assert pool.listing_calls == 1
    assert pool.generate_calls == 0
    assert json.loads(llm(_PROMPT))["notes"]


def test_factory_refuses_a_mismatched_pool_without_generating() -> None:
    """A build against a foreign identity refuses with no prompt sent."""
    pool = _pool(served_identity="other/model@v1")
    with pytest.raises(DigestMismatchError):
        _client(pool).factory()(PrivacyTier.OPEN, max_tokens=32)
    assert pool.generate_calls == 0


def test_evict_and_residency_are_honest_about_a_pool() -> None:
    """The pool's residency is the vendor's: no eviction, no resident size."""
    pool = _pool()
    client = _client(pool)
    assert client.evict() is None
    assert client.resident_bytes() is None
    assert pool.requests == []


def _raw_client(handler: Any, **overrides: Any) -> BenchPoolClient:
    """Build a client over a hand-written transport, for malformed answers."""
    fields: dict[str, Any] = {
        "identity": _IDENTITY,
        "num_ctx": 2048,
        "num_predict": 128,
        "transport": httpx.MockTransport(handler),
    }
    fields.update(overrides)
    return BenchPoolClient("http://pool.test", _MODEL, **fields)


def test_listing_that_is_not_json_is_refused() -> None:
    """A pool answering prose instead of JSON establishes no identity."""

    def _prose(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="pool is fine, honestly")

    with pytest.raises(DigestMismatchError):
        _raw_client(_prose).verify_identity()


def test_attestation_without_a_usable_digest_is_refused() -> None:
    """A verified attestation that names no ``sha256:<hex>`` pins nothing."""
    pool = _pool(
        listing_payload={
            "identity": _IDENTITY,
            "models": [{"name": _MODEL}],
            "attestation": {"verified": True, "measurement_digest": "trust-me"},
        }
    )
    with pytest.raises(DigestMismatchError):
        _client(pool).verify_identity()


def test_answer_without_usage_is_not_judged_an_overflow() -> None:
    """A pool reporting no prompt size cannot be judged, so it is not flagged."""

    def _no_usage(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "identity": _IDENTITY,
                "model": _MODEL,
                "response": "generated",
            },
        )

    assert _raw_client(_no_usage).generate(_PROMPT, num_predict=16) == "generated"


def test_fake_pool_refuses_a_path_it_does_not_serve() -> None:
    """The fake answers no status for an unknown path — that is the oracle.

    ``tests/test_v1_api_structure.py`` keeps a 404 and the ``not_found`` wire
    string out of ``creek_mcp`` entirely (#1098), so the fake raises instead.
    Nothing in the harness dials another path.
    """
    with pytest.raises(ValueError, match="serves only"):
        _pool().answer("GET", "/v1/elsewhere", None)
