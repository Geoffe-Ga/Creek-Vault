"""Health-state contract for the one-vault container image (#1772)."""

from __future__ import annotations

from contextlib import nullcontext
from typing import TYPE_CHECKING

import httpx

from creek.config import VAULT_CONFIG_RELPATH
from creek_mcp.container_health import ProbeStatus, ProbeTarget, probe
from creek_mcp.container_runtime import ContainerSettings

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
