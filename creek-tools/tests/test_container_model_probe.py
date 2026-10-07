"""Fail-closed model readiness, separate from storage readiness (#1849).

``--check ready`` answers "is the vault API serving?". ``--check model``
answers a different question — "can the pinned local model generate?" — and
it says yes only when the loopback runtime is reachable, serves the pinned
digest, and completes a fixed synthetic canary that never touches the vault.

The reflection factory carries the same pin per request, so a vault-config
edit after boot cannot route reflection to a remote host or another model.
"""

from __future__ import annotations

import builtins
import hashlib
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from creek.classify.llm.completion import Completion
from creek.classify.llm.local_boundary import is_loopback_url
from creek.config import LLMConfig
from creek_mcp import container_health as health
from creek_mcp import model_package
from creek_mcp.container_health import (
    CANARY_PROMPT,
    ProbeResult,
    ProbeStatus,
    ProbeTarget,
    main,
    probe,
)
from creek_mcp.container_runtime import PORT_ENV, ContainerSettings
from creek_mcp.model_package import MODEL_PACKAGE_FILE_ENV, ModelPackageManifest
from tests.ollama_stub import OllamaStub, serve_ollama

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

_MODEL = "creek-test-model:q4"
_SERVED_DIGEST = "d" * 64
_BLOB_DIGEST = hashlib.sha256(b"creek-test-blob").hexdigest()
_SENTINEL = "VAULT-SENTINEL"
_CANARY_OUTPUT = "CANARY-OUTPUT-MARKER"
_CONFIG_SUBPATH = ("00-Creek-Meta", "creek_config.yaml")


def _pin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModelPackageManifest:
    """Configure an approved, fully pinned manifest outside any vault."""
    fields = {
        "schema_version": 1,
        "runtime_name": "creek-test-runtime",
        "runtime_version": "0.0.1",
        "runtime_digest": "c" * 64,
        "model_name": _MODEL,
        "model_blob_sha256": _BLOB_DIGEST,
        "runtime_inventory_digest": _SERVED_DIGEST,
        "quantization": "Q4_K_M",
        "parameter_count": 1_000_000,
        "size_bytes": 4096,
        "license_spdx": "Apache-2.0",
        "license_url": "https://example.test/LICENSE",
    }
    path = tmp_path / "model-package" / "manifest.json"
    path.parent.mkdir()
    path.write_text(json.dumps(fields), encoding="utf-8")
    monkeypatch.setattr(
        model_package, "APPROVED_MODEL_LICENSES", frozenset({"Apache-2.0"})
    )
    monkeypatch.setenv(MODEL_PACKAGE_FILE_ENV, str(path))
    return ModelPackageManifest.model_validate(fields)


def _settings(tmp_path: Path) -> ContainerSettings:
    """Return container settings over a vault seeded with a private note."""
    vault = tmp_path / "vault"
    (vault / "01-Fragments").mkdir(parents=True)
    (vault / "01-Fragments" / "note.md").write_text(_SENTINEL, encoding="utf-8")
    return ContainerSettings(vault_path=vault, config_path=vault / "creek.yaml")


def _tags(*rows: dict[str, str]) -> httpx.Response:
    """Return an Ollama ``/api/tags`` response listing *rows*."""
    return httpx.Response(200, json={"models": list(rows)})


class _Runtime:
    """Record and answer the probe's inventory and canary calls."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        inventory: httpx.Response | Exception,
        canary: str | Exception = "ready",
    ) -> None:
        self.inventory_urls: list[str] = []
        self.canary_calls: list[dict[str, Any]] = []
        self._inventory = inventory
        self._canary = canary
        monkeypatch.setattr(health, "ollama_get", self._get)
        monkeypatch.setattr(health, "call_ollama", self._call)

    def _get(self, config: LLMConfig, path: str, **_kwargs: object) -> httpx.Response:
        self.inventory_urls.append(config.ollama_url + path)
        if isinstance(self._inventory, Exception):
            raise self._inventory
        return self._inventory

    def _call(self, config: LLMConfig, prompt: str, **kwargs: object) -> Completion:
        self.canary_calls.append({"config": config, "prompt": prompt, **kwargs})
        if isinstance(self._canary, Exception):
            raise self._canary
        return Completion(text=self._canary, stop_reason="end_turn")


def _served(digest: str = _SERVED_DIGEST) -> httpx.Response:
    """Return an inventory serving the pinned name at *digest*."""
    return _tags({"name": _MODEL, "digest": f"sha256:{digest}"})


@pytest.mark.parametrize(
    "inventory",
    [
        httpx.ConnectError("refused"),
        httpx.ReadTimeout("slow"),
        httpx.Response(500, text="boom"),
        httpx.Response(200, text="not json"),
    ],
)
def test_model_probe_reports_runtime_down_on_transport_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    inventory: httpx.Response | Exception,
) -> None:
    """A dead, failing or garbled runtime is ``runtime-down``, exit 23."""
    _pin(tmp_path, monkeypatch)
    runtime = _Runtime(monkeypatch, inventory=inventory)

    result = probe(_settings(tmp_path), ProbeTarget.MODEL)

    assert result.status is ProbeStatus.RUNTIME_DOWN
    assert result.exit_code == 23
    assert runtime.canary_calls == []
    assert len(runtime.inventory_urls) == 1
    assert is_loopback_url(runtime.inventory_urls[0])
    assert runtime.inventory_urls[0].endswith("/api/tags")


@pytest.mark.parametrize(
    ("inventory", "status", "code"),
    [
        (_tags(), ProbeStatus.MODEL_MISSING, 24),
        (
            _tags({"name": "creek-other-model:q4", "digest": "d" * 64}),
            ProbeStatus.MODEL_MISSING,
            24,
        ),
        (_served("e" * 64), ProbeStatus.MODEL_DIGEST_MISMATCH, 25),
        (_tags({"name": _MODEL}), ProbeStatus.MODEL_DIGEST_MISMATCH, 25),
        (_tags({"name": _MODEL, "digest": ""}), ProbeStatus.MODEL_DIGEST_MISMATCH, 25),
    ],
)
def test_model_probe_reports_missing_name_and_mismatched_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    inventory: httpx.Response,
    status: ProbeStatus,
    code: int,
) -> None:
    """A name match is not enough: the served digest must be the pinned one."""
    _pin(tmp_path, monkeypatch)
    runtime = _Runtime(monkeypatch, inventory=inventory)

    result = probe(_settings(tmp_path), ProbeTarget.MODEL)

    assert result.status is status
    assert result.exit_code == code
    assert runtime.canary_calls == []


@pytest.mark.parametrize(
    ("canary", "status", "code"),
    [
        (
            httpx.HTTPStatusError(
                "boom",
                request=httpx.Request("POST", "http://127.0.0.1"),
                response=httpx.Response(500),
            ),
            ProbeStatus.GENERATION_FAILED,
            26,
        ),
        (httpx.ReadTimeout("slow"), ProbeStatus.GENERATION_FAILED, 26),
        (ValueError("not json"), ProbeStatus.GENERATION_FAILED, 26),
        ("", ProbeStatus.GENERATION_FAILED, 26),
        ("   \n", ProbeStatus.GENERATION_FAILED, 26),
        ("ready", ProbeStatus.MODEL_READY, 0),
    ],
)
def test_model_probe_requires_successful_canary_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    canary: str | Exception,
    status: ProbeStatus,
    code: int,
) -> None:
    """Inventory alone is not readiness; the model must actually generate."""
    _pin(tmp_path, monkeypatch)
    runtime = _Runtime(monkeypatch, inventory=_served(), canary=canary)

    result = probe(_settings(tmp_path), ProbeTarget.MODEL)

    assert result.status is status
    assert result.exit_code == code
    assert len(runtime.canary_calls) == 1


def test_model_probe_sends_only_the_constant_canary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The canary is a module constant; no vault file is opened or sent."""
    _pin(tmp_path, monkeypatch)
    settings = _settings(tmp_path)
    vault = settings.vault_path.resolve()
    opened: list[str] = []

    def _guard(real: Callable[..., Any]) -> Callable[..., Any]:
        def _wrapped(target: object, *args: object, **kwargs: object) -> Any:
            inside = isinstance(target, (str, Path)) and (
                Path(target).resolve().is_relative_to(vault)
            )
            if inside:
                opened.append(str(target))
                raise AssertionError("model probe opened a vault file")
            return real(target, *args, **kwargs)

        return _wrapped

    monkeypatch.setattr(builtins, "open", _guard(builtins.open))
    for name in ("open", "read_text", "read_bytes"):
        monkeypatch.setattr(Path, name, _guard(getattr(Path, name)))
    runtime = _Runtime(monkeypatch, inventory=_served())

    result = probe(settings, ProbeTarget.MODEL)

    assert result.status is ProbeStatus.MODEL_READY
    assert opened == []
    assert len(runtime.canary_calls) == 1
    call = runtime.canary_calls[0]
    assert call["prompt"] == CANARY_PROMPT
    assert call["config"].model == _MODEL
    assert call["config"].provider == "ollama"
    assert is_loopback_url(call["config"].ollama_url)
    assert call["max_tokens"] == health._CANARY_MAX_TOKENS
    recorded = repr(runtime.canary_calls) + repr(runtime.inventory_urls)
    assert _SENTINEL not in recorded
    assert str(vault) not in recorded


def test_canary_prompt_is_a_fixed_content_free_constant() -> None:
    """The canary is synthetic: short, fixed, and free of vault vocabulary."""
    assert isinstance(CANARY_PROMPT, str)
    assert CANARY_PROMPT.strip()
    assert len(CANARY_PROMPT) < 200


def test_storage_only_vault_without_manifest_is_model_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An upgraded storage-only vault stays model-unavailable, with no dial."""
    monkeypatch.delenv(MODEL_PACKAGE_FILE_ENV, raising=False)
    runtime = _Runtime(monkeypatch, inventory=_served())

    assert probe(_settings(tmp_path), ProbeTarget.MODEL).status is (
        ProbeStatus.MODEL_MISSING
    )
    invalid = tmp_path / "invalid.json"
    invalid.write_text("{}", encoding="utf-8")
    monkeypatch.setenv(MODEL_PACKAGE_FILE_ENV, str(invalid))

    assert probe(_settings_again(tmp_path), ProbeTarget.MODEL).status is (
        ProbeStatus.MODEL_MISSING
    )
    assert runtime.inventory_urls == []
    assert runtime.canary_calls == []


def _settings_again(tmp_path: Path) -> ContainerSettings:
    """Return settings over the vault :func:`_settings` already seeded."""
    vault = tmp_path / "vault"
    return ContainerSettings(vault_path=vault, config_path=vault / "creek.yaml")


@pytest.fixture
def ollama_runtime() -> Iterator[OllamaStub]:
    """Serve a real loopback stand-in runtime; set its inventory per test."""
    yield from serve_ollama(OllamaStub(tags={"models": []}))


@pytest.mark.parametrize(
    ("digest", "status"),
    [
        (_SERVED_DIGEST, ProbeStatus.MODEL_READY),
        ("e" * 64, ProbeStatus.MODEL_DIGEST_MISMATCH),
    ],
)
def test_model_probe_logs_contain_no_prompt_or_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    ollama_runtime: OllamaStub,
    digest: str,
    status: ProbeStatus,
) -> None:
    """Only the status line leaves the probe: no prompt, output or blob hash.

    The real dial path runs — ``ollama_get``, ``call_ollama`` and the boundary
    transport — against a loopback stand-in, so a log line added anywhere on
    that path is caught, not just one in the probe module.
    """
    manifest = _pin(tmp_path, monkeypatch)
    ollama_runtime.tags = {"models": [{"name": _MODEL, "digest": f"sha256:{digest}"}]}
    ollama_runtime.generation = _CANARY_OUTPUT
    monkeypatch.setattr(
        health, "_LOOPBACK_RUNTIME", LLMConfig(ollama_url=ollama_runtime.url)
    )
    monkeypatch.delenv(PORT_ENV, raising=False)
    caplog.set_level(logging.DEBUG)

    with pytest.raises(SystemExit) as caught:
        main(["--check", "model"])

    out = capsys.readouterr()
    assert out.out == f"{status.value}\n"
    assert out.err == ""
    assert caught.value.code == ProbeResult(status).exit_code
    sent = [path for _method, path, _body in ollama_runtime.requests]
    expected = ["/api/tags", "/api/generate"]
    assert sent == (expected if status is ProbeStatus.MODEL_READY else expected[:1])
    forbidden = (CANARY_PROMPT, _CANARY_OUTPUT, manifest.model_blob_sha256)
    for record in caplog.records:
        for needle in forbidden:
            assert needle not in record.getMessage()


def test_model_target_config_error_maps_to_model_missing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A broken environment stays inside the model target's closed state set."""
    monkeypatch.setenv(PORT_ENV, "not-a-port")

    with pytest.raises(SystemExit) as model:
        main(["--check", "model"])
    assert capsys.readouterr().out == "model-missing\n"
    assert model.value.code == 24

    with pytest.raises(SystemExit) as ready:
        main([])
    assert capsys.readouterr().out == "v1-unready\n"
    assert ready.value.code == 22
