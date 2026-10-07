"""Loopback boundary for the "local" Ollama runtime (#1849).

``OllamaProvider.is_cloud`` is ``False`` unconditionally, and the router's
intimate-forces-local rule trusts that label. A free-form ``ollama_url`` would
let the provider labelled local dial any host, so in container mode every
Ollama dial goes through one chokepoint that refuses a non-loopback endpoint.
"""

from __future__ import annotations

import httpx
import pytest

from creek.classify.llm.local_boundary import (
    LOOPBACK_ONLY_ENV,
    NonLoopbackOllamaError,
    is_loopback_url,
    loopback_only_enforced,
    ollama_endpoint,
)
from creek.config import LLMConfig


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://127.0.0.1:11434", True),
        ("http://localhost:11434", True),
        ("http://LOCALHOST:11434", True),
        ("http://[::1]:11434", True),
        ("http://127.0.0.2:11434", True),
        ("https://127.0.0.1:11434", True),
        ("http://10.0.0.5:11434", False),
        ("http://192.168.1.2:11434", False),
        ("http://evil.example:11434", False),
        ("http://127.0.0.1@evil.example:11434", False),
        ("http://evil.example@127.0.0.1:11434", False),
        ("http://localhost.evil.example:11434", False),
        ("http://0.0.0.0:11434", False),
        ("http://[::]:11434", False),
        ("http://127.0.0.1:notaport", False),
        ("ftp://127.0.0.1:11434", False),
        ("file:///x", False),
        ("127.0.0.1:11434", False),
        ("", False),
    ],
)
def test_is_loopback_url(url: str, *, expected: bool) -> None:
    """Only a literal loopback host over HTTP(S) counts; no DNS is consulted."""
    assert is_loopback_url(url) is expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, False), ("", False), ("0", False), ("1", True), ("true", True)],
)
def test_loopback_only_enforced_reads_only_the_flag(
    value: str | None, *, expected: bool
) -> None:
    """Any non-empty value other than ``0`` turns the boundary on."""
    environ = {} if value is None else {LOOPBACK_ONLY_ENV: value}

    assert loopback_only_enforced(environ) is expected


def test_ollama_endpoint_refuses_remote_when_enforced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A remote URL raises a transport error that never echoes the host."""
    remote = LLMConfig(ollama_url="http://10.0.0.5:11434")
    monkeypatch.setenv(LOOPBACK_ONLY_ENV, "1")

    with pytest.raises(NonLoopbackOllamaError) as caught:
        ollama_endpoint(remote, "/api/tags")

    assert isinstance(caught.value, httpx.TransportError)
    assert isinstance(caught.value, httpx.HTTPError)
    assert "10.0.0.5" not in str(caught.value)
    assert "10.0.0.5" not in repr(caught.value)


def test_ollama_endpoint_joins_loopback_when_enforced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A loopback URL passes the boundary unchanged."""
    monkeypatch.setenv(LOOPBACK_ONLY_ENV, "1")

    joined = ollama_endpoint(
        LLMConfig(ollama_url="http://127.0.0.1:11434"), "/api/generate"
    )

    assert joined == "http://127.0.0.1:11434/api/generate"


def test_ollama_endpoint_is_unchanged_outside_container_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the flag a self-hosted LAN Ollama keeps working."""
    monkeypatch.delenv(LOOPBACK_ONLY_ENV, raising=False)

    joined = ollama_endpoint(LLMConfig(ollama_url="http://10.0.0.5:11434"), "/x")

    assert joined == "http://10.0.0.5:11434/x"
