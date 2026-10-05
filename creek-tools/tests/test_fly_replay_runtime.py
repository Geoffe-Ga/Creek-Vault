"""Fly replay-only vault ingress contract for the managed-vault pilot."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from creek.config import CONFIG_PATH_ENV_VAR
from creek_mcp.container_runtime import (
    BootstrapState,
    ContainerConfigurationError,
    PreparedVault,
    SingleConsumerSecret,
)
from creek_mcp.fly_vault_runtime import FlyVaultSettings, _load_replay_state
from creek_mcp.remote_auth import CONSUMER_TOKENS_ENV
from tests.v1_api_support import client, headers, seed_vault

if TYPE_CHECKING:
    from pathlib import Path

_STATE = "s" * 64
_TOKEN = "consumer-token-that-is-at-least-thirty-two-characters"


@pytest.mark.parametrize(
    "replay_source",
    [
        None,
        f"instance=router-machine,region=iad,t=1727395200,state={_STATE}",
        f"instance=router-machine;region=iad,t=1727395200;state={_STATE}",
        f"state={_STATE};",
        "state=wrong",
        "state=" + "x" * 64,
        f"state={_STATE};state={_STATE}",
        f"state={_STATE};unknown=value",
        f"state={_STATE};t=not-a-time",
        f"state={_STATE};region=iad;region=ord",
    ],
)
def test_vault_refuses_direct_or_malformed_replay_even_with_valid_bearer(
    tmp_path: Path,
    replay_source: str | None,
) -> None:
    """The bearer is necessary but never sufficient to bypass the Fly router."""
    vault = seed_vault(tmp_path)
    request_headers = headers()
    if replay_source is not None:
        request_headers["Fly-Replay-Src"] = replay_source

    response = client(vault_path=vault, fly_replay_state=_STATE).get(
        "/v1/health",
        headers=request_headers,
    )

    assert response.status_code == 401
    assert _STATE not in response.text


@pytest.mark.parametrize("separator", [";", "; "])
def test_vault_requires_replay_state_and_original_bearer(
    tmp_path: Path, separator: str
) -> None:
    """A closed Fly-Replay-Src plus the original bearer reaches the vault."""
    vault = seed_vault(tmp_path)
    replay = separator.join(
        ("instance=router-machine", "region=iad", "t=1727395200", f"state={_STATE}")
    )
    app_client = client(vault_path=vault, fly_replay_state=_STATE)

    missing_bearer = app_client.get(
        "/v1/health",
        headers={"Fly-Replay-Src": replay},
    )
    accepted = app_client.get(
        "/v1/health",
        headers={**headers(), "Fly-Replay-Src": replay},
    )

    assert missing_bearer.status_code == 401
    assert accepted.status_code == 200
    assert accepted.json() == {"status": "ok"}
    assert _STATE not in accepted.text


@pytest.mark.parametrize(
    ("app_name", "machine_id"),
    [
        ("", "machine-001"),
        ("bad;app", "machine-001"),
        ("creek-vault", ""),
        ("creek-vault", "bad,machine"),
    ],
)
def test_fly_runtime_requires_closed_provider_injected_coordinates(
    app_name: str,
    machine_id: str,
) -> None:
    """Missing or delimiter-bearing Fly coordinates fail before any bind."""
    with pytest.raises(ValueError, match="attestation"):
        FlyVaultSettings.from_environ(
            {"FLY_APP_NAME": app_name, "FLY_MACHINE_ID": machine_id}
        )


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("CREEK_CONTAINER_EXPECTED_FLY_APP", "other-vault"),
        ("CREEK_CONTAINER_EXPECTED_FLY_REGION", "ord"),
        ("FLY_REGION", "ord"),
        ("FLY_PRIVATE_IP", "203.0.113.7"),
        ("FLY_PRIVATE_IP", "fd00::1"),
    ],
)
def test_fly_runtime_refuses_app_region_or_private_network_drift(
    name: str,
    value: str,
) -> None:
    """Mounted replay secrets are unread until all immutable coordinates match."""
    environ = {
        "FLY_APP_NAME": "creek-vault-pilot",
        "FLY_MACHINE_ID": "machine-001",
        "FLY_REGION": "iad",
        "FLY_PRIVATE_IP": "fdaa:0:1::2",
        "CREEK_CONTAINER_EXPECTED_FLY_APP": "creek-vault-pilot",
        "CREEK_CONTAINER_EXPECTED_FLY_REGION": "iad",
    }
    environ[name] = value

    with pytest.raises(ValueError, match="attestation"):
        FlyVaultSettings.from_environ(environ)


@pytest.mark.parametrize("state", ["", "x" * 63, "x" * 65, "x" * 63 + ";"])
def test_replay_state_mount_has_one_exact_header_safe_shape(
    tmp_path: Path,
    state: str,
) -> None:
    """A malformed secret mount never reaches an HTTP response header."""
    path = tmp_path / "replay-state"
    path.write_text(state, encoding="ascii")
    path.chmod(0o600)

    with pytest.raises(ContainerConfigurationError, match="replay state"):
        _load_replay_state(path)


def test_fly_runtime_uses_plaintext_only_with_replay_and_bearer_guards(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Fly-only bootstrap omits leaf TLS but retains both auth factors."""
    from creek_mcp import fly_vault_runtime as runtime

    vault = tmp_path / "vault"
    vault.mkdir()
    config = vault / "creek.yaml"
    replay = tmp_path / "replay-state"
    replay.write_text(_STATE, encoding="ascii")
    replay.chmod(0o600)
    settings = FlyVaultSettings.from_environ(
        {
            "FLY_APP_NAME": "creek-vault-pilot",
            "FLY_MACHINE_ID": "machine-001",
            "FLY_REGION": "iad",
            "FLY_PRIVATE_IP": "fdaa:0:1::2",
            "CREEK_CONTAINER_EXPECTED_FLY_APP": "creek-vault-pilot",
            "CREEK_CONTAINER_EXPECTED_FLY_REGION": "iad",
            "CREEK_CONTAINER_VAULT_PATH": str(vault),
            "CREEK_CONTAINER_FLY_REPLAY_STATE_FILE": str(replay),
        }
    )
    observed: dict[str, object] = {}
    secret = SingleConsumerSecret("adepthood", (_TOKEN,))
    monkeypatch.setattr(
        runtime,
        "prepare_vault",
        lambda _settings: PreparedVault(config, BootstrapState.EXISTING),
    )
    monkeypatch.setattr(runtime, "load_consumer_secret", lambda _path: secret)
    monkeypatch.setattr(runtime, "announce_rotation_window", lambda _verifier: None)

    def fake_app(*, verifier: object, fly_replay_state: str) -> object:
        observed["state"] = fly_replay_state
        observed["verifier"] = verifier
        return object()

    def fake_serve(app: object, args: object) -> None:
        observed["app"] = app
        observed["args"] = args
        observed["token_env"] = os.environ.get(CONSUMER_TOKENS_ENV)
        observed["config_env"] = os.environ.get(CONFIG_PATH_ENV_VAR)
        observed["vault_env"] = os.environ.get("CREEK_VAULT_PATH")

    monkeypatch.setattr(runtime, "create_app", fake_app)
    monkeypatch.setattr(runtime, "serve", fake_serve)
    monkeypatch.delenv(CONFIG_PATH_ENV_VAR, raising=False)
    monkeypatch.delenv("CREEK_VAULT_PATH", raising=False)
    monkeypatch.setenv(CONSUMER_TOKENS_ENV, _TOKEN)

    runtime.run(settings)

    assert observed["state"] == _STATE
    assert observed["token_env"] is None
    assert observed["config_env"] == str(config)
    assert observed["vault_env"] == str(vault)
    server_args = vars(observed["args"])
    assert server_args["tls_cert"] is None
    assert server_args["tls_key"] is None
    assert os.environ.get(CONFIG_PATH_ENV_VAR) is None
    assert os.environ.get("CREEK_VAULT_PATH") is None
    assert os.environ[CONSUMER_TOKENS_ENV] == _TOKEN
    assert _TOKEN not in repr(observed)
    assert _STATE not in repr(observed["verifier"])


def test_fly_runtime_restores_existing_environment_when_server_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed server propagates its error without retaining runtime paths."""
    from creek_mcp import fly_vault_runtime as runtime

    vault = tmp_path / "vault"
    vault.mkdir()
    config = vault / "creek.yaml"
    replay = tmp_path / "replay-state"
    replay.write_text(_STATE, encoding="ascii")
    replay.chmod(0o600)
    settings = FlyVaultSettings.from_environ(
        {
            "FLY_APP_NAME": "creek-vault-pilot",
            "FLY_MACHINE_ID": "machine-001",
            "FLY_REGION": "iad",
            "FLY_PRIVATE_IP": "fdaa:0:1::2",
            "CREEK_CONTAINER_EXPECTED_FLY_APP": "creek-vault-pilot",
            "CREEK_CONTAINER_EXPECTED_FLY_REGION": "iad",
            "CREEK_CONTAINER_VAULT_PATH": str(vault),
            "CREEK_CONTAINER_FLY_REPLAY_STATE_FILE": str(replay),
        }
    )
    original = {
        CONFIG_PATH_ENV_VAR: "/operator/config.yaml",
        "CREEK_VAULT_PATH": "/operator/vault",
        CONSUMER_TOKENS_ENV: _TOKEN,
    }
    for name, value in original.items():
        monkeypatch.setenv(name, value)
    secret = SingleConsumerSecret("adepthood", (_TOKEN,))
    monkeypatch.setattr(
        runtime,
        "prepare_vault",
        lambda _settings: PreparedVault(config, BootstrapState.EXISTING),
    )
    monkeypatch.setattr(runtime, "load_consumer_secret", lambda _path: secret)
    monkeypatch.setattr(runtime, "announce_rotation_window", lambda _verifier: None)
    monkeypatch.setattr(runtime, "create_app", lambda **_kwargs: object())

    def failing_serve(_app: object, _args: object) -> None:
        assert os.environ[CONFIG_PATH_ENV_VAR] == str(config)
        assert os.environ["CREEK_VAULT_PATH"] == str(vault)
        assert CONSUMER_TOKENS_ENV not in os.environ
        raise RuntimeError("server failed")

    monkeypatch.setattr(runtime, "serve", failing_serve)

    with pytest.raises(RuntimeError, match="server failed"):
        runtime.run(settings)

    assert {name: os.environ.get(name) for name in original} == original
