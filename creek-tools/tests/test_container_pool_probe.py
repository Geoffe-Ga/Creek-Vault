"""``--check pool``: pool readiness, with no generation at all (#1894).

``--check model`` asks whether the pinned *local* model can generate, and
proves it with a free loopback canary. The pool cannot be asked that way: a
generation on every healthcheck would be a billed vendor call, which the
owner's "no spend yet" decision rules out. So ADR-0016 Decision 1 item 9
drops the canary for the pool and stops at the identity listing — reachable,
offering the pinned model, at the pinned digest — with stable exit codes.

``--check ready`` is unchanged and never consults the pool: storage-ready
implies neither model-ready nor pool-ready.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from creek.classify.llm.pool_boundary import (
    POOL_GENERATE_PATH,
    POOL_MODELS_PATH,
    POOL_URL_ENV,
)
from creek_mcp import container_health as health
from creek_mcp import model_package
from creek_mcp.container_health import ProbeStatus, ProbeTarget, main, probe
from creek_mcp.container_runtime import PORT_ENV, ContainerSettings
from creek_mcp.model_package import MODEL_PACKAGE_FILE_ENV
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

_POOL_EXIT_CODES = {
    ProbeStatus.POOL_READY: 0,
    ProbeStatus.POOL_UNCONFIGURED: 30,
    ProbeStatus.POOL_UNREACHABLE: 31,
    ProbeStatus.POOL_MODEL_MISSING: 32,
    ProbeStatus.POOL_IDENTITY_MISMATCH: 33,
}


def _pin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Configure an approved manifest pinning the pool's identity."""
    fields = {
        "schema_version": 1,
        "runtime_name": PINNED_RUNTIME_NAME,
        "runtime_version": PINNED_RUNTIME_VERSION,
        "runtime_digest": "c" * 64,
        "model_name": PINNED_MODEL,
        "model_blob_sha256": "b" * 64,
        "runtime_inventory_digest": pinned_identity_digest(),
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


def _settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ContainerSettings:
    """Return container settings; the pool probe reads none of them."""
    vault = tmp_path / "vault"
    (vault / "01-Fragments").mkdir(parents=True)
    monkeypatch.setenv("CREEK_VAULT_PATH", str(vault))
    monkeypatch.setenv(PORT_ENV, "8080")
    return ContainerSettings.from_environ()


def test_the_pinned_pool_is_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reachable, offering the pinned model, at the pinned digest."""
    _pin(tmp_path, monkeypatch)
    pool = FakePool.serving(identity_listing())
    pool.pin(monkeypatch)
    try:
        result = probe(_settings(tmp_path, monkeypatch), ProbeTarget.POOL)
    finally:
        pool.close()

    assert result.status is ProbeStatus.POOL_READY
    assert result.exit_code == 0


def test_the_pool_probe_makes_no_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One request, the identity listing; never a billed generation.

    Asserted on what the pool actually received, not on a stubbed helper: a
    generation on every healthcheck would be a real vendor charge, so the
    claim has to be about the wire.
    """
    _pin(tmp_path, monkeypatch)
    pool = FakePool.serving(identity_listing())
    pool.pin(monkeypatch)
    try:
        result = probe(_settings(tmp_path, monkeypatch), ProbeTarget.POOL)
    finally:
        pool.close()

    assert result.status is ProbeStatus.POOL_READY
    assert pool.requests == [("GET", POOL_MODELS_PATH)]
    assert POOL_GENERATE_PATH not in [path for _method, path in pool.requests]


@pytest.mark.parametrize(
    ("listing", "expected"),
    [
        pytest.param(
            identity_listing(model_name="other:1"),
            ProbeStatus.POOL_MODEL_MISSING,
            id="model-missing",
        ),
        pytest.param(
            identity_listing(runtime_version="9.9"),
            ProbeStatus.POOL_IDENTITY_MISMATCH,
            id="identity-mismatch",
        ),
    ],
)
def test_a_drifted_pool_is_graded_precisely(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    listing: object,
    expected: ProbeStatus,
) -> None:
    """The probe says which half failed, with no prose and no URL."""
    _pin(tmp_path, monkeypatch)
    pool = FakePool.serving(listing)
    pool.pin(monkeypatch)
    try:
        result = probe(_settings(tmp_path, monkeypatch), ProbeTarget.POOL)
    finally:
        pool.close()

    assert result.status is expected
    assert result.exit_code == _POOL_EXIT_CODES[expected]


@pytest.mark.parametrize("status", [403, 500])
def test_a_refusing_pool_is_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    """An admission refusal or a server error is ``pool-unreachable``."""
    _pin(tmp_path, monkeypatch)
    pool = FakePool.serving({}, status=status)
    pool.pin(monkeypatch)
    try:
        result = probe(_settings(tmp_path, monkeypatch), ProbeTarget.POOL)
    finally:
        pool.close()

    assert result.status is ProbeStatus.POOL_UNREACHABLE


def test_a_down_pool_is_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transport failure is graded, not raised."""
    _pin(tmp_path, monkeypatch)
    settings = _settings(tmp_path, monkeypatch)
    monkeypatch.setenv(POOL_URL_ENV, "https://pool.vendor.test")
    SocketSpy(monkeypatch)

    assert probe(settings, ProbeTarget.POOL).status is ProbeStatus.POOL_UNREACHABLE


def test_no_pinned_destination_is_unconfigured_without_a_dial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With nowhere pinned there is nothing to probe, and nothing is dialled."""
    _pin(tmp_path, monkeypatch)
    settings = _settings(tmp_path, monkeypatch)
    monkeypatch.delenv(POOL_URL_ENV, raising=False)
    spy = SocketSpy(monkeypatch)

    result = probe(settings, ProbeTarget.POOL)

    assert result.status is ProbeStatus.POOL_UNCONFIGURED
    assert result.exit_code == 30
    assert spy.destinations == []


@pytest.mark.parametrize("manifest", ["absent", "unset"])
def test_a_missing_manifest_is_unconfigured_without_a_dial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, manifest: str
) -> None:
    """Pool readiness needs a loadable manifest to compare against."""
    settings = _settings(tmp_path, monkeypatch)
    if manifest == "absent":
        monkeypatch.setenv(MODEL_PACKAGE_FILE_ENV, str(tmp_path / "absent.json"))
    else:
        monkeypatch.delenv(MODEL_PACKAGE_FILE_ENV, raising=False)
    monkeypatch.setenv(POOL_URL_ENV, "https://pool.vendor.test")
    spy = SocketSpy(monkeypatch)

    assert probe(settings, ProbeTarget.POOL).status is ProbeStatus.POOL_UNCONFIGURED
    assert spy.destinations == []


def test_storage_readiness_never_consults_the_pool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--check ready`` is unchanged: no pool dial, whatever the pool does."""
    _pin(tmp_path, monkeypatch)
    settings = _settings(tmp_path, monkeypatch)
    monkeypatch.setenv(POOL_URL_ENV, "https://pool.vendor.test")
    spy = SocketSpy(monkeypatch)

    result = probe(settings, ProbeTarget.READY)

    assert result.status is ProbeStatus.PROCESS_DOWN
    assert "pool.vendor.test" not in spy.hosts


def test_the_pool_target_is_selectable_and_exits_with_its_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--check pool`` reaches the probe and prints one content-free state."""
    _pin(tmp_path, monkeypatch)
    _settings(tmp_path, monkeypatch)
    monkeypatch.delenv(POOL_URL_ENV, raising=False)

    with pytest.raises(SystemExit) as caught:
        main(["--check", "pool"])

    assert caught.value.code == 30
    assert health._parser().parse_args(["--check", "pool"]).check == "pool"


def test_the_pool_exit_codes_are_stable_and_unique() -> None:
    """Every pool status has its own code, distinct from the model probe's."""
    codes = health._EXIT_CODES

    assert {status: codes[status] for status in _POOL_EXIT_CODES} == _POOL_EXIT_CODES
    assert ProbeTarget.POOL in health._UNAVAILABLE
    failures = [code for code in codes.values() if code != 0]
    assert len(failures) == len(set(failures))
