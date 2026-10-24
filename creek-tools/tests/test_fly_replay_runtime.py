"""Fly replay-only vault ingress contract for the managed-vault pilot."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from creek.classify.llm.consent import (
    CLOUD_CONSENT_ENV,
    LEGACY_CONSENT_ENV,
    MANAGED_VAULT_ENV,
    has_cloud_consent,
)
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

    from starlette.applications import Starlette

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

    def fake_app(
        *, verifier: object, fly_replay_state: str, agent_verifier: object
    ) -> object:
        observed["state"] = fly_replay_state
        observed["verifier"] = verifier
        observed["agent_verifier"] = agent_verifier
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
    assert observed["agent_verifier"] is None
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


def test_fly_runtime_environment_enforces_loopback_and_restores_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Fly runtime sets the loopback boundary only while it serves."""
    from creek.classify.llm.local_boundary import LOOPBACK_ONLY_ENV
    from creek_mcp.fly_vault_runtime import _runtime_environment

    monkeypatch.setenv(LOOPBACK_ONLY_ENV, "")
    monkeypatch.delenv(LOOPBACK_ONLY_ENV)
    with _runtime_environment(tmp_path / "creek.yaml", tmp_path):
        assert os.environ[LOOPBACK_ONLY_ENV] == "1"
    assert LOOPBACK_ONLY_ENV not in os.environ

    monkeypatch.setenv(LOOPBACK_ONLY_ENV, "operator-value")
    with _runtime_environment(tmp_path / "creek.yaml", tmp_path):
        assert os.environ[LOOPBACK_ONLY_ENV] == "1"
    assert os.environ[LOOPBACK_ONLY_ENV] == "operator-value"


def _attested_environ() -> dict[str, str]:
    """Return a complete, matching set of Fly-injected coordinates."""
    return {
        "FLY_APP_NAME": "creek-vault-pilot",
        "FLY_MACHINE_ID": "machine-001",
        "FLY_REGION": "iad",
        "FLY_PRIVATE_IP": "fdaa:0:1::2",
        "CREEK_CONTAINER_EXPECTED_FLY_APP": "creek-vault-pilot",
        "CREEK_CONTAINER_EXPECTED_FLY_REGION": "iad",
    }


@pytest.mark.parametrize("var", [CLOUD_CONSENT_ENV, LEGACY_CONSENT_ENV])
@pytest.mark.parametrize("marker", [{}, {MANAGED_VAULT_ENV: "1"}])
def test_fly_runtime_main_exits_two_for_cloud_consent(
    var: str,
    marker: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The replay runtime is managed-only, so consent refuses boot (#1863)."""
    from creek_mcp import fly_vault_runtime as runtime

    for name, value in {**_attested_environ(), **marker}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv(var, "true")
    monkeypatch.setattr(
        runtime, "run", lambda _settings: pytest.fail("managed vault must not run")
    )

    with pytest.raises(SystemExit) as raised:
        runtime.main()

    assert raised.value.code == 2
    stderr = capsys.readouterr().err
    assert "creek-fly-vault: startup refused" in stderr
    assert var in stderr
    assert "managed vaults are local-only" in stderr
    assert "true" not in stderr


def test_fly_runtime_starts_without_affirmative_consent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Neither consent variable affirmative leaves the replay runtime bootable."""
    monkeypatch.delenv(CLOUD_CONSENT_ENV, raising=False)
    monkeypatch.delenv(LEGACY_CONSENT_ENV, raising=False)
    environ = {**_attested_environ(), MANAGED_VAULT_ENV: "1", CLOUD_CONSENT_ENV: "0"}

    settings = FlyVaultSettings.from_environ(environ)

    assert settings.fly_app_name == "creek-vault-pilot"
    assert settings.container.managed is True


def test_fly_runtime_pins_consent_off_while_serving(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Consent that appears after a managed boot never reaches a provider."""
    from creek_mcp import fly_vault_runtime as runtime

    vault = tmp_path / "vault"
    vault.mkdir()
    replay = tmp_path / "replay-state"
    replay.write_text(_STATE, encoding="ascii")
    replay.chmod(0o600)
    settings = FlyVaultSettings.from_environ(
        {
            **_attested_environ(),
            "CREEK_CONTAINER_VAULT_PATH": str(vault),
            "CREEK_CONTAINER_FLY_REPLAY_STATE_FILE": str(replay),
        }
    )
    secret = SingleConsumerSecret("adepthood", (_TOKEN,))
    monkeypatch.setattr(
        runtime,
        "prepare_vault",
        lambda _settings: PreparedVault(vault / "creek.yaml", BootstrapState.EXISTING),
    )
    monkeypatch.setattr(runtime, "load_consumer_secret", lambda _path: secret)
    monkeypatch.setattr(runtime, "announce_rotation_window", lambda _verifier: None)
    monkeypatch.setattr(runtime, "create_app", lambda **_kwargs: object())
    observed: dict[str, bool] = {}

    def fake_serve(_app: object, _args: object) -> None:
        monkeypatch.setenv(CLOUD_CONSENT_ENV, "1")
        observed["consent"] = has_cloud_consent()

    monkeypatch.setattr(runtime, "serve", fake_serve)

    runtime.run(settings)

    assert observed == {"consent": False}


def test_the_served_fly_app_answers_a_replayed_mcp_handshake(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Fly entry point serves ``/mcp`` behind both guards, with no new flag (#1962).

    Args:
        tmp_path: Pytest's per-test directory.
        monkeypatch: Captures the served app and stubs the mounted secrets.
    """
    from starlette.testclient import TestClient

    from creek.config import generate_default_config
    from creek_mcp import fly_vault_runtime as runtime
    from creek_mcp.policy import Transport
    from tests.mcp_http_support import (
        call_handshake,
        initialize_body,
        mcp_headers,
        open_session,
    )

    vault = seed_vault(tmp_path)
    config = tmp_path / "creek.yaml"
    generate_default_config(config, vault_path=vault)
    replay_state = tmp_path / "replay-state"
    replay_state.write_text(_STATE, encoding="ascii")
    replay_state.chmod(0o600)
    settings = FlyVaultSettings.from_environ(
        {
            "FLY_APP_NAME": "creek-vault-pilot",
            "FLY_MACHINE_ID": "machine-001",
            "FLY_REGION": "iad",
            "FLY_PRIVATE_IP": "fdaa:0:1::2",
            "CREEK_CONTAINER_EXPECTED_FLY_APP": "creek-vault-pilot",
            "CREEK_CONTAINER_EXPECTED_FLY_REGION": "iad",
            "CREEK_CONTAINER_VAULT_PATH": str(vault),
            "CREEK_CONTAINER_FLY_REPLAY_STATE_FILE": str(replay_state),
        }
    )
    monkeypatch.setattr(
        runtime,
        "prepare_vault",
        lambda _settings: PreparedVault(config, BootstrapState.EXISTING),
    )
    monkeypatch.setattr(
        runtime,
        "load_consumer_secret",
        lambda _path: SingleConsumerSecret("adepthood", (_TOKEN,)),
    )
    monkeypatch.setattr(runtime, "announce_rotation_window", lambda _verifier: None)
    served: list[Starlette] = []
    monkeypatch.setattr(runtime, "serve", lambda app, _args: served.append(app))

    runtime.run(settings)

    (app,) = served
    replay = f"instance=router-machine;region=iad;t=1727395200;state={_STATE}"
    with TestClient(app, headers={"Fly-Replay-Src": replay}) as test_client:
        session = open_session(test_client, _TOKEN)
        handshake = call_handshake(test_client, session["headers"])
    direct = TestClient(app).post(
        "/mcp", json=initialize_body(), headers=mcp_headers(_TOKEN)
    )
    assert handshake["transport"] == Transport.NETWORK.value
    assert direct.status_code == 401


def test_fly_runtime_announces_an_ungated_consumer_before_serving(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Fly entry point names an ungated consumer at start (#1964).

    Args:
        tmp_path: Pytest's per-test directory.
        monkeypatch: Records the announcement and stubs the server.
    """
    from creek_mcp import fly_vault_runtime as runtime

    vault = tmp_path / "vault"
    vault.mkdir()
    replay_state = tmp_path / "replay-state"
    replay_state.write_text(_STATE, encoding="ascii")
    replay_state.chmod(0o600)
    settings = FlyVaultSettings.from_environ(
        {
            "FLY_APP_NAME": "creek-vault-pilot",
            "FLY_MACHINE_ID": "machine-001",
            "FLY_REGION": "iad",
            "FLY_PRIVATE_IP": "fdaa:0:1::2",
            "CREEK_CONTAINER_EXPECTED_FLY_APP": "creek-vault-pilot",
            "CREEK_CONTAINER_EXPECTED_FLY_REGION": "iad",
            "CREEK_CONTAINER_VAULT_PATH": str(vault),
            "CREEK_CONTAINER_FLY_REPLAY_STATE_FILE": str(replay_state),
        }
    )
    monkeypatch.setattr(
        runtime,
        "prepare_vault",
        lambda _settings: PreparedVault(vault / "creek.yaml", BootstrapState.EXISTING),
    )
    monkeypatch.setattr(
        runtime,
        "load_consumer_secret",
        lambda _path: SingleConsumerSecret("adepthood", (_TOKEN,)),
    )
    announced: list[object] = []
    served: list[object] = []
    monkeypatch.setattr(runtime, "announce_rotation_window", lambda _verifier: None)
    monkeypatch.setattr(runtime, "announce_ungated_consumers", announced.append)
    monkeypatch.setattr(
        runtime,
        "create_app",
        lambda *, verifier, fly_replay_state, **_kwargs: (verifier, fly_replay_state),
    )
    monkeypatch.setattr(runtime, "serve", lambda app, _args: served.append(app))

    runtime.run(settings)

    assert len(announced) == 1
    assert served == [(announced[0], _STATE)]
