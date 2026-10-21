"""The model pin admits two runtimes, and pins both the same way (#1894).

Before ADR-0016 a pin mode admitted exactly one provider, ``ollama`` on
loopback. A managed vault has no local runtime to pin — that is the per-vault
model the record rejects on 1 GB of RAM — so the pin now admits the pair
``ollama`` and ``pool``, checked against the *same* manifest fields:
``model_name`` and ``runtime_inventory_digest``. The schema does not move
(R8); what moves is how a non-Ollama runtime fills the digest, which
:data:`~creek.classify.llm.providers.POOL_IDENTITY_SCHEME` defines.

What must not move: every cloud provider stays refused whatever key or
consent is present, a manifest that cannot be loaded fails closed, and the
writer's own request key still takes precedence over the pool without the
pool ever being dialled (ADR-0016 T10).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from creek.classify.llm.pool_boundary import POOL_URL_ENV
from creek.classify.llm.providers import PoolProvider
from creek.config import LLMConfig
from creek_mcp import model_package
from creek_mcp.model_package import MODEL_PACKAGE_FILE_ENV, ModelPackageManifest
from creek_mcp.model_pin import (
    PINNED_PROVIDERS,
    pin_refuses_stage,
    pinned_model_serves,
    provider_may_serve,
    request_key_admitted,
)
from tests.pool_support import (
    PINNED_MODEL,
    PINNED_RUNTIME_NAME,
    PINNED_RUNTIME_VERSION,
    FakePool,
    SocketSpy,
    identity_listing,
    pinned_identity_digest,
)

if TYPE_CHECKING:
    from pathlib import Path


def _pin_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    inventory_digest: str | None = None,
    model_name: str = PINNED_MODEL,
) -> ModelPackageManifest:
    """Configure an approved manifest pinning the pool's identity."""
    fields = {
        "schema_version": 1,
        "runtime_name": PINNED_RUNTIME_NAME,
        "runtime_version": PINNED_RUNTIME_VERSION,
        "runtime_digest": "c" * 64,
        "model_name": model_name,
        "model_blob_sha256": "b" * 64,
        "runtime_inventory_digest": inventory_digest
        or pinned_identity_digest(model_name),
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


def _pool_stage() -> LLMConfig:
    """Return a pool stage pinned to the test model."""
    return LLMConfig(provider="pool", model=PINNED_MODEL)


def test_the_pin_admits_exactly_the_two_runtimes() -> None:
    """``ollama`` and ``pool``; nothing else, and no cloud vendor."""
    assert frozenset({"ollama", "pool"}) == PINNED_PROVIDERS


@pytest.mark.parametrize("provider", ["anthropic", "openai", "gemini", "enclave"])
def test_a_pin_mode_still_refuses_every_other_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    """The pair widens the pin; it does not admit anything else."""
    _pin_manifest(tmp_path, monkeypatch)

    assert pin_refuses_stage(LLMConfig(provider=provider)) is True


@pytest.mark.parametrize("provider", ["ollama", "pool"])
def test_a_pin_mode_admits_both_pinned_runtimes_on_the_config_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    """Neither pinned runtime is refused on its provider name."""
    _pin_manifest(tmp_path, monkeypatch)

    assert pin_refuses_stage(LLMConfig(provider=provider)) is False


def test_outside_a_pin_mode_nothing_is_refused() -> None:
    """A self-hoster's own routing keeps working, pool included."""
    assert pin_refuses_stage(_pool_stage()) is False


def test_the_pinned_pool_serves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pool reporting the pinned runtime and model may be shown content."""
    _pin_manifest(tmp_path, monkeypatch)
    pool = FakePool.serving(identity_listing())
    pool.pin(monkeypatch)
    try:
        provider = PoolProvider(_pool_stage())
        assert pinned_model_serves(provider) is True
        assert provider_may_serve(provider) is True
    finally:
        pool.close()


@pytest.mark.parametrize(
    "listing",
    [
        pytest.param(identity_listing(runtime_name="other"), id="runtime"),
        pytest.param(identity_listing(runtime_version="9.9"), id="version"),
        pytest.param(identity_listing(model_name="substitute:1"), id="model"),
    ],
)
def test_a_pool_reporting_another_identity_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, listing: object
) -> None:
    """Identity drift fails the pin before any prompt is sent (T1)."""
    _pin_manifest(tmp_path, monkeypatch)
    pool = FakePool.serving(listing)
    pool.pin(monkeypatch)
    try:
        assert pinned_model_serves(PoolProvider(_pool_stage())) is False
    finally:
        pool.close()


def test_a_pool_stage_on_the_wrong_model_is_refused_without_a_dial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The model name is checked from the config, before the socket."""
    _pin_manifest(tmp_path, monkeypatch)
    monkeypatch.setenv(POOL_URL_ENV, "https://pool.vendor.test")
    spy = SocketSpy(monkeypatch)
    provider = PoolProvider(LLMConfig(provider="pool", model="other:1"))

    assert pinned_model_serves(provider) is False
    assert spy.destinations == []


def test_an_unloadable_manifest_fails_closed_for_the_pool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A configured manifest that will not load admits no pool at all."""
    monkeypatch.setenv(MODEL_PACKAGE_FILE_ENV, str(tmp_path / "absent.json"))
    pool = FakePool.serving(identity_listing())
    pool.pin(monkeypatch)
    try:
        assert pinned_model_serves(PoolProvider(_pool_stage())) is False
    finally:
        pool.close()


def test_an_unconfigured_manifest_admits_no_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``model-ready`` needs a loadable manifest *and* a matching pool."""
    monkeypatch.delenv(MODEL_PACKAGE_FILE_ENV, raising=False)
    pool = FakePool.serving(identity_listing())
    pool.pin(monkeypatch)
    try:
        assert pinned_model_serves(PoolProvider(_pool_stage())) is False
    finally:
        pool.close()


def test_a_request_key_stage_is_admitted_without_naming_the_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The writer's key wins on its own stage; the pool is never consulted."""
    from creek.classify.llm.consent import MANAGED_VAULT_ENV
    from creek.classify.llm.request_key import RequestKey

    monkeypatch.setenv(MANAGED_VAULT_ENV, "1")
    monkeypatch.setenv(POOL_URL_ENV, "https://pool.vendor.test")
    spy = SocketSpy(monkeypatch)
    key = RequestKey("anthropic", "placeholder-not-a-real-key")

    assert request_key_admitted(key.stage_config(), key) is True
    assert request_key_admitted(_pool_stage(), key) is False
    assert spy.destinations == []
