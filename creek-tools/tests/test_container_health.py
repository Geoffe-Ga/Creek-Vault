"""Health-state contract for the one-vault container image (#1772)."""

from __future__ import annotations

from contextlib import nullcontext
from typing import TYPE_CHECKING

import httpx

from creek.config import VAULT_CONFIG_RELPATH
from creek_mcp.container_health import ProbeStatus, ProbeTarget, probe
from creek_mcp.container_runtime import ContainerSettings
from creek_mcp.fly_vault_runtime import REPLAY_STATE_FILE_ENV

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

_TOKEN = "consumer-token-that-is-at-least-thirty-two-characters"


def _settings(tmp_path: Path) -> ContainerSettings:
    """Return health settings with a synthetic mounted volume."""
    vault = tmp_path / "vault"
    vault.mkdir()
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        f"41 32 0:35 / {vault} rw,nosuid - ext4 /dev/vault rw\n",
        encoding="utf-8",
    )
    secret = tmp_path / "tokens"
    secret.write_text(f"adepthood={_TOKEN}\n", encoding="utf-8")
    cert = tmp_path / "tls.crt"
    key = tmp_path / "tls.key"
    cert.write_text("certificate", encoding="utf-8")
    key.write_text("private key", encoding="utf-8")
    return ContainerSettings(
        vault_path=vault,
        config_path=vault / VAULT_CONFIG_RELPATH,
        consumer_tokens_file=secret,
        tls_cert_file=cert,
        tls_key_file=key,
        mountinfo_file=mountinfo,
    )


def test_process_probe_does_not_require_a_vault_or_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Liveness answers only whether the API process owns its socket."""
    from creek_mcp import container_health as health

    settings = _settings(tmp_path)
    settings.consumer_tokens_file.unlink()
    settings.vault_path.rmdir()
    monkeypatch.setattr(health, "_process_is_up", lambda _settings: True)

    result = probe(settings, ProbeTarget.PROCESS)

    assert result.status is ProbeStatus.PROCESS_UP
    assert result.exit_code == 0


def test_process_socket_probe_reports_an_accepting_listener(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The concrete liveness check succeeds when loopback accepts TCP."""
    from creek_mcp import container_health as health

    settings = _settings(tmp_path)
    monkeypatch.setattr(
        health.socket,
        "create_connection",
        lambda *_args, **_kwargs: nullcontext(),
    )

    assert health._process_is_up(settings) is True


def test_process_socket_probe_contains_connection_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refused loopback socket becomes process-down without a traceback."""
    from creek_mcp import container_health as health

    settings = _settings(tmp_path)

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise OSError("refused")

    monkeypatch.setattr(health.socket, "create_connection", refuse)

    assert health._process_is_up(settings) is False


def test_volume_probe_distinguishes_unmounted_from_process_down(
    tmp_path: Path,
) -> None:
    """The storage signal is independent of the socket signal."""
    settings = _settings(tmp_path)
    settings.mountinfo_file.write_text("", encoding="utf-8")

    result = probe(settings, ProbeTarget.VOLUME)

    assert result.status is ProbeStatus.VOLUME_UNMOUNTED
    assert result.exit_code != 0


def test_readiness_reports_process_down_first(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dead listener is not collapsed into a generic readiness failure."""
    from creek_mcp import container_health as health

    settings = _settings(tmp_path)
    monkeypatch.setattr(health, "_process_is_up", lambda _settings: False)

    result = probe(settings, ProbeTarget.READY)

    assert result.status is ProbeStatus.PROCESS_DOWN


def test_readiness_reports_unmounted_volume_after_process_is_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live socket cannot hide loss of the persistent vault mount."""
    from creek_mcp import container_health as health

    settings = _settings(tmp_path)
    settings.mountinfo_file.write_text("", encoding="utf-8")
    monkeypatch.setattr(health, "_process_is_up", lambda _settings: True)

    result = probe(settings, ProbeTarget.READY)

    assert result.status is ProbeStatus.VOLUME_UNMOUNTED


def test_readiness_reports_authenticated_v1_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Process-up plus mounted storage is still not application readiness."""
    from creek_mcp import container_health as health

    settings = _settings(tmp_path)
    monkeypatch.setattr(health, "_process_is_up", lambda _settings: True)
    monkeypatch.setattr(health, "_v1_is_ready", lambda _settings: False)

    result = probe(settings, ProbeTarget.READY)

    assert result.status is ProbeStatus.V1_UNREADY


def test_readiness_requires_all_three_layers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only socket + mount + authenticated health produces v1-ready."""
    from creek_mcp import container_health as health

    settings = _settings(tmp_path)
    monkeypatch.setattr(health, "_process_is_up", lambda _settings: True)
    monkeypatch.setattr(health, "_v1_is_ready", lambda _settings: True)

    result = probe(settings, ProbeTarget.READY)

    assert result.status is ProbeStatus.V1_READY
    assert result.exit_code == 0


def test_v1_probe_reads_bearer_from_secret_mount_not_arguments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The authenticated probe passes the bearer only in the HTTP header."""
    from creek_mcp import container_health as health

    settings = _settings(tmp_path)
    observed: dict[str, object] = {}

    def fake_get(
        url: str,
        *,
        headers: dict[str, str],
        timeout: float,
        verify: object,
    ) -> httpx.Response:
        observed.update(
            url=url,
            headers=headers,
            timeout=timeout,
            verify=verify,
        )
        return httpx.Response(200, json={"status": "ok"})

    monkeypatch.setattr(health.httpx, "get", fake_get)
    monkeypatch.setattr(
        health.ssl,
        "create_default_context",
        lambda *, cafile: {"cafile": cafile},
    )

    assert health._v1_is_ready(settings) is True
    assert observed["url"] == "https://127.0.0.1:8823/v1/health"
    assert observed["headers"] == {"Authorization": f"Bearer {_TOKEN}"}
    assert _TOKEN not in str(observed["url"])


def test_fly_replay_probe_uses_loopback_plaintext_with_both_auth_factors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Fly-only health probe proves replay state and bearer on loopback."""
    from creek_mcp import container_health as health

    settings = _settings(tmp_path)
    replay = tmp_path / "replay-state"
    state = "s" * 64
    replay.write_text(state, encoding="ascii")
    replay.chmod(0o600)
    observed: dict[str, object] = {}

    def fake_get(
        url: str,
        *,
        headers: dict[str, str],
        timeout: float,
        verify: object,
    ) -> httpx.Response:
        observed.update(url=url, headers=headers, timeout=timeout, verify=verify)
        return httpx.Response(200, json={"status": "ok"})

    monkeypatch.setenv(REPLAY_STATE_FILE_ENV, str(replay))
    monkeypatch.setattr(health.httpx, "get", fake_get)

    assert health._v1_is_ready(settings) is True
    assert observed == {
        "url": "http://127.0.0.1:8823/v1/health",
        "headers": {
            "Authorization": f"Bearer {_TOKEN}",
            "Fly-Replay-Src": f"state={state}",
        },
        "timeout": 2.0,
        "verify": False,
    }


def test_v1_probe_treats_transport_failure_as_unready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A TLS or network error is a bounded unhealthy result, not a traceback."""
    from creek_mcp import container_health as health

    settings = _settings(tmp_path)

    def fail(*_args: object, **_kwargs: object) -> httpx.Response:
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(health.httpx, "get", fail)

    assert health._v1_is_ready(settings) is False


def test_ready_probe_is_independent_of_model_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Storage-ready is not model-ready, and a dead model never fails READY."""
    from creek_mcp import container_health as health
    from creek_mcp.model_package import MODEL_PACKAGE_FILE_ENV

    settings = _settings(tmp_path)
    monkeypatch.setattr(health, "_process_is_up", lambda _settings: True)
    monkeypatch.setattr(health, "_volume_is_mounted", lambda _settings: True)
    monkeypatch.setattr(health, "_v1_is_ready", lambda _settings: True)
    monkeypatch.setattr(health, "configured_manifest", lambda: _any_manifest())

    def dead_runtime(*_args: object, **_kwargs: object) -> httpx.Response:
        raise httpx.ConnectError("runtime is down")

    monkeypatch.setattr(health.httpx, "get", dead_runtime)
    monkeypatch.delenv(MODEL_PACKAGE_FILE_ENV, raising=False)

    ready = probe(settings, ProbeTarget.READY)
    model = probe(settings, ProbeTarget.MODEL)

    assert ready.status is ProbeStatus.V1_READY
    assert ready.exit_code == 0
    assert model.status is ProbeStatus.RUNTIME_DOWN
    assert model.exit_code != 0
    assert health._parser().parse_args([]).check == "ready"


def _any_manifest() -> object:
    """Return a syntactically valid pin; the runtime is what is under test."""
    from creek_mcp.model_package import ModelPackageManifest

    return ModelPackageManifest(
        schema_version=1,
        runtime_name="creek-test-runtime",
        runtime_version="0.0.1",
        runtime_digest="c" * 64,
        model_name="creek-test-model:q4",
        model_blob_sha256="b" * 64,
        runtime_inventory_digest="d" * 64,
        quantization="Q4_K_M",
        parameter_count=1,
        size_bytes=1,
        license_spdx="Apache-2.0",
        license_url="https://example.test/LICENSE",
    )


def test_exit_code_table_is_exhaustive_and_stable() -> None:
    """Every status has one stable code; every target has a fallback status."""
    from creek_mcp import container_health as health

    codes = health._EXIT_CODES
    assert set(codes) == set(ProbeStatus)
    assert codes[ProbeStatus.PROCESS_DOWN] == 20
    assert codes[ProbeStatus.VOLUME_UNMOUNTED] == 21
    assert codes[ProbeStatus.V1_UNREADY] == 22
    assert codes[ProbeStatus.MODEL_READY] == 0
    model_failures = {
        ProbeStatus.RUNTIME_DOWN: 23,
        ProbeStatus.MODEL_MISSING: 24,
        ProbeStatus.MODEL_DIGEST_MISMATCH: 25,
        ProbeStatus.GENERATION_FAILED: 26,
    }
    assert {status: codes[status] for status in model_failures} == model_failures
    failures = [code for code in codes.values() if code != 0]
    assert len(failures) == len(set(failures))
    assert set(health._UNAVAILABLE) == set(ProbeTarget)
    assert health._UNAVAILABLE[ProbeTarget.MODEL] is ProbeStatus.MODEL_MISSING
    assert health._UNAVAILABLE[ProbeTarget.READY] is ProbeStatus.V1_UNREADY
    for target, status in health._UNAVAILABLE.items():
        assert codes[status] != 0, target
