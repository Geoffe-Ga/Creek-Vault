"""The bench's own Ollama client, exercised against ``httpx.MockTransport``.

It differs from the production provider in exactly the ways a benchmark needs:
it pins ``num_ctx`` on every request, can evict the model (``keep_alive: 0``)
for a cold trial, resolves the weights' digest for the report, and reads the
resident model size. It shares the production request timeout rather than
restating it.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from creek.classify.llm.providers import OllamaProvider
from creek.models import PrivacyTier
from creek_mcp.bench.ollama_client import BenchOllamaClient, DigestMismatchError
from creek_mcp.bench.outcome import ProviderUnavailableError

_HEX = "b" * 64
_LICENSE_CANARY = "CANARY-license-text-7d1e"


class _Recorder:
    """A MockTransport handler that records requests and serves fixtures."""

    def __init__(self, routes: dict[str, Any] | None = None) -> None:
        """Serve *routes* (path -> JSON body); record every request."""
        self.routes = routes or {}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        """Record *request* and answer from the route table."""
        self.requests.append(request)
        body = self.routes.get(request.url.path)
        if body is None:
            return httpx.Response(404, json={"error": "not found"})
        return httpx.Response(200, json=body)

    def payloads(self) -> list[dict[str, Any]]:
        """Return the decoded JSON bodies of every recorded request."""
        return [json.loads(r.content) for r in self.requests if r.content]


def _client(recorder: _Recorder, **kwargs: Any) -> BenchOllamaClient:
    """Build a client on *recorder*'s transport."""
    defaults: dict[str, Any] = {"num_ctx": 2048, "num_predict": 128}
    defaults.update(kwargs)
    return BenchOllamaClient(
        "http://ollama.test:11434",
        "mistral:7b",
        transport=httpx.MockTransport(recorder),
        **defaults,
    )


_TAGS = {
    "models": [
        {
            "name": "mistral:7b",
            "digest": _HEX,
            "details": {"quantization_level": "Q4_0", "parameter_size": "7.2B"},
        },
        {"name": "other:latest", "digest": "c" * 64},
    ]
}


def test_generate_sends_num_ctx_num_predict_think_false() -> None:
    """Every completion pins the context window and the output ceiling."""
    recorder = _Recorder({"/api/generate": {"response": "hello"}})
    llm = _client(recorder).factory()(PrivacyTier.OPEN, max_tokens=96)
    assert llm("prompt text") == "hello"
    (payload,) = recorder.payloads()
    assert payload == {
        "model": "mistral:7b",
        "prompt": "prompt text",
        "stream": False,
        "think": False,
        "options": {"num_ctx": 2048, "num_predict": 96},
    }


def test_num_predict_is_capped_by_the_run_ceiling() -> None:
    """The run's ``num_predict`` caps whatever reflect asks for."""
    recorder = _Recorder({"/api/generate": {"response": "x"}})
    _client(recorder, num_predict=32).factory()(PrivacyTier.OPEN, max_tokens=128)("p")
    assert recorder.payloads()[0]["options"]["num_predict"] == 32


def test_generated_callables_declare_local() -> None:
    """The bench client is local by construction."""
    llm = _client(_Recorder()).factory()(PrivacyTier.OPEN, max_tokens=8)
    assert getattr(llm, "is_cloud", True) is False


def test_non_string_response_is_empty_text() -> None:
    """A malformed body degrades to empty text rather than a crash."""
    recorder = _Recorder({"/api/generate": ["not", "a", "dict"]})
    assert _client(recorder).factory()(PrivacyTier.OPEN, max_tokens=8)("p") == ""


def test_evict_keep_alive_zero() -> None:
    """Eviction asks Ollama to unload the model now."""
    recorder = _Recorder({"/api/generate": {"done": True}})
    _client(recorder).evict()
    assert recorder.payloads() == [{"model": "mistral:7b", "keep_alive": 0}]


def test_resolve_pinned_license_id_only_never_text() -> None:
    """The digest and details are pinned; license text never escapes."""
    recorder = _Recorder(
        {
            "/api/tags": _TAGS,
            "/api/show": {
                "license": f"Apache License\nVersion 2.0 {_LICENSE_CANARY}",
            },
        }
    )
    pinned = _client(recorder).resolve_pinned()
    assert pinned.digest == f"sha256:{_HEX}"
    assert pinned.quantization == "Q4_0"
    assert pinned.parameter_count == "7.2B"
    assert pinned.license_id == "Apache-2.0"
    assert _LICENSE_CANARY not in repr(pinned)


def test_resolve_pinned_unknown_license_and_missing_details() -> None:
    """An unrecognised license is ``unknown``; absent details are ``None``."""
    recorder = _Recorder(
        {
            "/api/tags": {"models": [{"name": "mistral:7b", "digest": _HEX}]},
            "/api/show": {"license": "Some Bespoke Terms"},
        }
    )
    pinned = _client(recorder).resolve_pinned()
    assert pinned.license_id == "unknown"
    assert pinned.quantization is None
    assert pinned.parameter_count is None


def test_bare_tag_matches_latest() -> None:
    """Ollama treats an omitted tag as ``latest``."""
    recorder = _Recorder(
        {
            "/api/tags": {"models": [{"name": "mistral:latest", "digest": _HEX}]},
            "/api/show": {},
        }
    )
    client = BenchOllamaClient(
        "http://ollama.test:11434",
        "mistral",
        num_ctx=2048,
        num_predict=128,
        transport=httpx.MockTransport(recorder),
    )
    assert client.resolve_pinned().digest == f"sha256:{_HEX}"


def test_verify_digest_mismatch_and_missing_refused() -> None:
    """A different or absent digest is refused; the right one is returned."""
    recorder = _Recorder({"/api/tags": _TAGS, "/api/show": {}})
    client = _client(recorder)
    assert client.verify_digest(f"sha256:{_HEX}").digest == f"sha256:{_HEX}"
    with pytest.raises(DigestMismatchError, match="does not match"):
        client.verify_digest("sha256:" + "d" * 64)
    missing = _client(_Recorder({"/api/tags": {"models": []}, "/api/show": {}}))
    with pytest.raises(DigestMismatchError, match="not installed"):
        missing.verify_digest(f"sha256:{_HEX}")


def test_resident_bytes_reads_api_ps() -> None:
    """The loaded model's size comes from ``/api/ps``; absent is ``None``."""
    loaded = _Recorder({"/api/ps": {"models": [{"name": "mistral:7b", "size": 42}]}})
    assert _client(loaded).resident_bytes() == 42
    unloaded = _Recorder({"/api/ps": {"models": []}})
    assert _client(unloaded).resident_bytes() is None


def test_timeout_equals_ollama_request_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The client shares the production request timeout, read at call time."""
    monkeypatch.setattr(OllamaProvider, "REQUEST_TIMEOUT", 3.5)
    seen: list[dict[str, Any]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions["timeout"])
        return httpx.Response(200, json={"models": []})

    client = BenchOllamaClient(
        "http://ollama.test:11434",
        "mistral:7b",
        num_ctx=2048,
        num_predict=128,
        transport=httpx.MockTransport(_handler),
    )
    assert client.resident_bytes() is None
    assert seen[0]["read"] == 3.5


def test_connect_error_raises_provider_unavailable() -> None:
    """An unreachable endpoint is a typed availability failure."""

    def _refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = BenchOllamaClient(
        "http://ollama.test:11434",
        "mistral:7b",
        num_ctx=2048,
        num_predict=128,
        transport=httpx.MockTransport(_refuse),
    )
    with pytest.raises(ProviderUnavailableError):
        client.evict()


def test_http_error_status_raises() -> None:
    """A non-2xx answer surfaces as ``HTTPStatusError`` for classification."""
    with pytest.raises(httpx.HTTPStatusError):
        _client(_Recorder()).evict()
