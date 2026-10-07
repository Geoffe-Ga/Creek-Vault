"""Environment proxies cannot carry "local" Ollama traffic off the host (#1849).

``ollama_endpoint`` checks the *URL*, but under httpx's default
``trust_env=True`` a request to ``http://127.0.0.1:11434`` is routed through
``HTTP_PROXY`` / ``ALL_PROXY`` whenever ``NO_PROXY`` does not exempt loopback.
The provider stays labelled local while the prompt — Intimate tier included —
leaves the machine. So the boundary owns the transport too, and ignores the
proxy environment for every Ollama dial.

These tests drive the real dial path against a loopback stand-in runtime with a
proxy configured that points at a dead port. A dial that honours the proxy
fails; a dial that bypasses it reaches the stand-in.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import httpx
import pytest

from creek.classify.llm.local_boundary import LOOPBACK_ONLY_ENV
from creek.classify.llm.providers import call_ollama, check_ollama_available
from creek.config import LLMConfig
from creek_mcp import container_health as health
from creek_mcp import model_package
from creek_mcp.container_health import CANARY_PROMPT, ProbeStatus, ProbeTarget, probe
from creek_mcp.container_runtime import ContainerSettings
from creek_mcp.model_package import MODEL_PACKAGE_FILE_ENV
from tests.ollama_stub import (
    NO_PROXY_ENV_NAMES,
    PROXY_ENV_NAMES,
    OllamaStub,
    dead_proxy_url,
    serve_ollama,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

_MODEL = "creek-test-model:q4"
_DIGEST = "d" * 64


@pytest.fixture
def ollama() -> Iterator[OllamaStub]:
    """Serve a loopback runtime listing the pinned model at its digest."""
    stub = OllamaStub(tags={"models": [{"name": _MODEL, "digest": _DIGEST}]})
    yield from serve_ollama(stub)


@pytest.fixture(autouse=True)
def _proxied_environment(monkeypatch: pytest.MonkeyPatch) -> str:
    """Route every proxy-honouring HTTP request to a dead loopback port."""
    proxy = dead_proxy_url()
    for name in PROXY_ENV_NAMES:
        monkeypatch.setenv(name, proxy)
    for name in NO_PROXY_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(LOOPBACK_ONLY_ENV, "1")
    return proxy


def test_the_proxy_environment_really_intercepts_a_default_client(
    ollama: OllamaStub,
) -> None:
    """Guard the guard: a proxy-honouring dial must fail in this environment."""
    with pytest.raises(httpx.TransportError), httpx.Client(timeout=2.0) as client:
        client.get(f"{ollama.url}/api/tags")

    assert ollama.requests == []


def test_inventory_check_bypasses_environment_proxies(ollama: OllamaStub) -> None:
    """``/api/tags`` reaches the loopback runtime, not the proxy."""
    config = LLMConfig(ollama_url=ollama.url, model=_MODEL)

    assert check_ollama_available(config, timeout=2.0, expected_digest=_DIGEST)
    assert ollama.requests == [("GET", "/api/tags", None)]


def test_generation_bypasses_environment_proxies(ollama: OllamaStub) -> None:
    """``/api/generate`` — the call that carries the prompt — stays on loopback."""
    config = LLMConfig(ollama_url=ollama.url, model=_MODEL)

    completion = call_ollama(config, "a private prompt", timeout=2.0, max_tokens=4)

    assert completion.text == "ready"
    assert [(method, path) for method, path, _ in ollama.requests] == [
        ("POST", "/api/generate")
    ]


def test_model_probe_bypasses_environment_proxies(
    ollama: OllamaStub, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The readiness probe's inventory and canary both stay on loopback."""
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "runtime_name": "creek-test-runtime",
                "runtime_version": "0.0.1",
                "runtime_digest": "c" * 64,
                "model_name": _MODEL,
                "model_blob_sha256": "b" * 64,
                "runtime_inventory_digest": _DIGEST,
                "quantization": "Q4_K_M",
                "parameter_count": 1,
                "size_bytes": 1,
                "license_spdx": "Apache-2.0",
                "license_url": "https://example.test/LICENSE",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        model_package, "APPROVED_MODEL_LICENSES", frozenset({"Apache-2.0"})
    )
    monkeypatch.setenv(MODEL_PACKAGE_FILE_ENV, str(manifest))
    monkeypatch.setattr(health, "_LOOPBACK_RUNTIME", LLMConfig(ollama_url=ollama.url))

    result = probe(ContainerSettings(vault_path=tmp_path), ProbeTarget.MODEL)

    assert result.status is ProbeStatus.MODEL_READY
    assert [(method, path) for method, path, _ in ollama.requests] == [
        ("GET", "/api/tags"),
        ("POST", "/api/generate"),
    ]
    assert ollama.requests[1][2] is not None
    assert ollama.requests[1][2]["prompt"] == CANARY_PROMPT
