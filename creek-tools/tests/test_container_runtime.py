"""Contract tests for the one-vault container runtime (#1772)."""

from __future__ import annotations

import os
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
import yaml

from creek.classify.llm.consent import (
    CLOUD_CONSENT_ENV,
    LEGACY_CONSENT_ENV,
    MANAGED_VAULT_ENV,
    has_cloud_consent,
)
from creek.config import CONFIG_PATH_ENV_VAR, VAULT_CONFIG_RELPATH, load_config
from creek_mcp.container_runtime import (
    CONFIG_FILE_ENV,
    CONSUMER_TOKENS_FILE_ENV,
    MOUNTINFO_FILE_ENV,
    TLS_CERT_FILE_ENV,
    TLS_KEY_FILE_ENV,
    VAULT_PATH_ENV,
    BootstrapState,
    ContainerConfigurationError,
    ContainerSettings,
    load_consumer_secret,
    prepare_vault,
    run,
)
from creek_mcp.remote_auth import CONSUMER_TOKENS_ENV

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from starlette.applications import Starlette

_TOKEN = "consumer-token-that-is-at-least-thirty-two-characters"


def _mountinfo(path: Path, mount: Path) -> None:
    """Write one synthetic Linux mountinfo row for *mount*."""
    escaped = str(mount).replace("\\", r"\134").replace(" ", r"\040")
    path.write_text(
        f"41 32 0:35 / {escaped} rw,nosuid - ext4 /dev/mapper/vault rw\n",
        encoding="utf-8",
    )


def _settings(tmp_path: Path, *, mounted: bool = True) -> ContainerSettings:
    """Return complete settings over an empty synthetic volume."""
    vault = tmp_path / "vault with space"
    vault.mkdir()
    mountinfo = tmp_path / "mountinfo"
    _mountinfo(mountinfo, vault if mounted else tmp_path / "elsewhere")
    secret = tmp_path / "consumer_tokens"
    secret.write_text(f"adepthood={_TOKEN}\n", encoding="utf-8")
    cert = tmp_path / "tls.crt"
    key = tmp_path / "tls.key"
    cert.write_text("test certificate", encoding="utf-8")
    key.write_text("test private key", encoding="utf-8")
    return ContainerSettings(
        vault_path=vault,
        config_path=vault / VAULT_CONFIG_RELPATH,
        consumer_tokens_file=secret,
        tls_cert_file=cert,
        tls_key_file=key,
        mountinfo_file=mountinfo,
    )


def test_settings_are_paths_not_secret_values(tmp_path: Path) -> None:
    """The container environment carries only mount paths and a port."""
    vault = tmp_path / "vault"
    env: Mapping[str, str] = {
        VAULT_PATH_ENV: str(vault),
        CONFIG_FILE_ENV: str(tmp_path / "config.yaml"),
        CONSUMER_TOKENS_FILE_ENV: str(tmp_path / "tokens"),
        TLS_CERT_FILE_ENV: str(tmp_path / "cert"),
        TLS_KEY_FILE_ENV: str(tmp_path / "key"),
        MOUNTINFO_FILE_ENV: str(tmp_path / "mountinfo"),
        "CREEK_CONTAINER_PORT": "9443",
    }

    settings = ContainerSettings.from_environ(env)

    assert settings.vault_path == vault
    assert settings.port == 9443
    assert _TOKEN not in repr(settings)


@pytest.mark.parametrize("value", ["zero", "0", "65536"])
def test_invalid_port_is_refused_without_binding(value: str) -> None:
    """A malformed or out-of-range port fails during settings load."""
    with pytest.raises(ContainerConfigurationError, match="port"):
        ContainerSettings.from_environ({"CREEK_CONTAINER_PORT": value})


def test_missing_volume_mount_is_refused_before_any_write(tmp_path: Path) -> None:
    """A plain image directory cannot masquerade as the durable volume."""
    settings = _settings(tmp_path, mounted=False)

    with pytest.raises(ContainerConfigurationError, match="mounted volume"):
        prepare_vault(settings)

    assert list(settings.vault_path.iterdir()) == []


def test_clean_mounted_volume_is_initialized_once(tmp_path: Path) -> None:
    """First boot scaffolds a usable vault and pins its absolute path."""
    settings = _settings(tmp_path)

    prepared = prepare_vault(settings)

    assert prepared.state is BootstrapState.INITIALIZED
    assert prepared.config_path == settings.config_path
    assert (settings.vault_path / "01-Fragments" / "Journal").is_dir()
    config = load_config(settings.config_path)
    assert config.vault_path == settings.vault_path


def test_restart_preserves_every_existing_byte(tmp_path: Path) -> None:
    """A second boot validates but does not refresh or rewrite the vault."""
    settings = _settings(tmp_path)
    prepare_vault(settings)
    note = settings.vault_path / "01-Fragments" / "Journal" / "kept.md"
    note.write_text("private journal content\n", encoding="utf-8")
    before = {
        path.relative_to(settings.vault_path): path.read_bytes()
        for path in settings.vault_path.rglob("*")
        if path.is_file()
    }

    prepared = prepare_vault(settings)

    after = {
        path.relative_to(settings.vault_path): path.read_bytes()
        for path in settings.vault_path.rglob("*")
        if path.is_file()
    }
    assert prepared.state is BootstrapState.EXISTING
    assert after == before


def test_nonempty_volume_missing_config_is_refused_untouched(tmp_path: Path) -> None:
    """A partial or foreign volume is never mistaken for a clean first boot."""
    settings = _settings(tmp_path)
    sentinel = settings.vault_path / "do-not-overwrite.txt"
    sentinel.write_text("operator data\n", encoding="utf-8")

    with pytest.raises(ContainerConfigurationError, match="missing config"):
        prepare_vault(settings)

    assert sentinel.read_text(encoding="utf-8") == "operator data\n"
    assert not settings.config_path.exists()


def test_explicit_missing_config_is_not_generated_outside_vault(tmp_path: Path) -> None:
    """A missing config mount is an error, not a place to synthesize state."""
    settings = _settings(tmp_path)
    external = tmp_path / "config-mount" / "creek.yaml"
    settings = settings.with_config_path(external)

    with pytest.raises(ContainerConfigurationError, match="explicit config"):
        prepare_vault(settings)

    assert not external.exists()
    assert list(settings.vault_path.iterdir()) == []


def test_config_cannot_redirect_writes_to_the_image_root(tmp_path: Path) -> None:
    """The configured vault must be exactly the mounted durable directory."""
    settings = _settings(tmp_path)
    settings.config_path.parent.mkdir(parents=True)
    settings.config_path.write_text(
        yaml.safe_dump({"vault_path": "/tmp/not-the-mounted-vault"}),
        encoding="utf-8",
    )

    with pytest.raises(ContainerConfigurationError, match="vault_path"):
        prepare_vault(settings)


def test_one_consumer_with_rotating_tokens_is_accepted(tmp_path: Path) -> None:
    """Rotation may retain an old token without creating a second identity."""
    secret_path = tmp_path / "tokens"
    second = "replacement-token-that-is-also-thirty-two-characters"
    secret_path.write_text(f"adepthood={_TOKEN},{second}\n", encoding="utf-8")

    secret = load_consumer_secret(secret_path)

    assert secret.consumer == "adepthood"
    assert secret.tokens == (_TOKEN, second)
    assert _TOKEN not in repr(secret)
    assert second not in repr(secret)


def test_multiple_consumer_identities_are_refused(tmp_path: Path) -> None:
    """One container serves exactly one configured consumer identity."""
    secret_path = tmp_path / "tokens"
    other = "other-consumer-token-that-is-thirty-two-characters"
    secret_path.write_text(
        f"adepthood={_TOKEN};other={other}\n",
        encoding="utf-8",
    )

    with pytest.raises(ContainerConfigurationError, match="exactly one") as caught:
        load_consumer_secret(secret_path)

    assert _TOKEN not in str(caught.value)
    assert other not in str(caught.value)


def test_missing_consumer_secret_is_refused_without_echoing_a_path_token(
    tmp_path: Path,
) -> None:
    """A missing secret mount fails closed and never invents credentials."""
    secret_path = tmp_path / f"missing-{_TOKEN}"

    with pytest.raises(ContainerConfigurationError, match="consumer secret") as caught:
        load_consumer_secret(secret_path)

    assert _TOKEN not in str(caught.value)


def test_runtime_keeps_credentials_out_of_environment_and_process_args(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bearer is read into the verifier, never exported or exec'd."""
    from creek_mcp import container_runtime as runtime

    settings = _settings(tmp_path)
    observed: dict[str, object] = {}
    monkeypatch.delenv(CONSUMER_TOKENS_ENV, raising=False)
    monkeypatch.setattr(runtime, "create_app", lambda *, verifier: verifier)
    monkeypatch.setattr(runtime, "announce_rotation_window", lambda _verifier: None)

    def fake_serve(app: object, args: object) -> None:
        observed["app"] = app
        observed["args"] = args
        observed["token_env"] = os.environ.get(CONSUMER_TOKENS_ENV)

    monkeypatch.setattr(runtime, "serve", fake_serve)

    run(settings)

    assert observed["token_env"] is None
    assert _TOKEN not in repr(observed["app"])
    assert _TOKEN not in repr(observed["args"])
    server_args = vars(observed["args"])
    assert server_args["tls_cert"] == settings.tls_cert_file
    assert server_args["tls_key"] == settings.tls_key_file
    assert os.environ[CONFIG_PATH_ENV_VAR] == str(settings.config_path)


def _write_config(settings: ContainerSettings, stage: str, url: str) -> None:
    """Write an existing-vault config whose *stage* dials *url*."""
    from creek.config import CreekConfig

    data = CreekConfig(vault_path=settings.vault_path).model_dump(mode="json")
    override = {**data["llm"]["default"], "ollama_url": url}
    if stage == "default":
        data["llm"]["default"] = override
    elif stage.startswith("writing_desk."):
        data["llm"]["writing_desk"][stage.split(".", 1)[1]] = override
    else:
        data["llm"][stage] = override
    settings.config_path.parent.mkdir(parents=True, exist_ok=True)
    settings.config_path.write_text(yaml.safe_dump(data), encoding="utf-8")


@pytest.mark.parametrize("stage", ["default", "generation", "writing_desk.critic"])
@pytest.mark.parametrize(
    ("url", "host"),
    [
        ("http://10.0.0.5:11434", "10.0.0.5"),
        ("http://evil.example:11434", "evil.example"),
        ("http://127.0.0.1@evil.example:11434", "evil.example"),
    ],
)
def test_container_mode_refuses_non_loopback_ollama_url(
    tmp_path: Path, stage: str, url: str, host: str
) -> None:
    """The provider labelled local may not dial a remote host from a container."""
    settings = _settings(tmp_path)
    _write_config(settings, stage, url)

    with pytest.raises(ContainerConfigurationError, match="loopback") as caught:
        prepare_vault(settings)

    assert stage in str(caught.value)
    assert host not in str(caught.value)


@pytest.mark.parametrize(
    "url",
    ["http://127.0.0.1:11434", "http://localhost:11434", "http://[::1]:11434"],
)
@pytest.mark.parametrize("stage", ["default", "generation", "writing_desk.critic"])
def test_container_mode_accepts_loopback_ollama_url(
    tmp_path: Path, stage: str, url: str
) -> None:
    """Loopback endpoints keep booting an existing vault unchanged."""
    settings = _settings(tmp_path)
    _write_config(settings, stage, url)

    assert prepare_vault(settings).state is BootstrapState.EXISTING


def test_first_boot_default_config_passes_the_loopback_rule(tmp_path: Path) -> None:
    """The generated default config already satisfies the narrowing."""
    assert prepare_vault(_settings(tmp_path)).state is BootstrapState.INITIALIZED


def test_run_sets_loopback_only_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The serving process enforces the loopback boundary on every dial."""
    from creek.classify.llm.local_boundary import LOOPBACK_ONLY_ENV
    from creek_mcp import container_runtime as runtime

    settings = _settings(tmp_path)
    observed: dict[str, str | None] = {}
    monkeypatch.setenv(LOOPBACK_ONLY_ENV, "")
    monkeypatch.delenv(LOOPBACK_ONLY_ENV)
    monkeypatch.setattr(runtime, "create_app", lambda *, verifier: verifier)
    monkeypatch.setattr(runtime, "announce_rotation_window", lambda _verifier: None)

    def fake_serve(_app: object, _args: object) -> None:
        observed["flag"] = os.environ.get(LOOPBACK_ONLY_ENV)

    monkeypatch.setattr(runtime, "serve", fake_serve)

    run(settings)

    assert observed["flag"] == "1"


@pytest.mark.parametrize("var", [CLOUD_CONSENT_ENV, LEGACY_CONSENT_ENV])
def test_managed_container_refuses_cloud_consent_without_echoing_it(
    var: str,
) -> None:
    """A managed vault names the consent variable and never its value (#1863)."""
    with pytest.raises(ContainerConfigurationError) as raised:
        ContainerSettings.from_environ({MANAGED_VAULT_ENV: "1", var: " Yes "})

    message = str(raised.value)
    assert var in message
    assert "managed vaults are local-only" in message
    assert "Yes" not in message


@pytest.mark.parametrize("var", [CLOUD_CONSENT_ENV, LEGACY_CONSENT_ENV])
def test_managed_container_main_exits_two_for_cloud_consent(
    var: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The default image entry point refuses a consenting managed vault."""
    from creek_mcp import container_runtime as runtime

    monkeypatch.setenv(MANAGED_VAULT_ENV, "1")
    monkeypatch.setenv(var, "1")
    monkeypatch.setattr(
        runtime, "run", lambda _settings: pytest.fail("managed vault must not run")
    )

    with pytest.raises(SystemExit) as raised:
        runtime.main()

    assert raised.value.code == 2
    stderr = capsys.readouterr().err
    assert "creek-container: startup refused" in stderr
    assert var in stderr


@pytest.mark.parametrize(
    "environ",
    [
        {MANAGED_VAULT_ENV: "1"},
        {MANAGED_VAULT_ENV: "0", CLOUD_CONSENT_ENV: "0", LEGACY_CONSENT_ENV: "no"},
    ],
)
def test_managed_container_starts_without_affirmative_consent(
    environ: Mapping[str, str],
) -> None:
    """Neither consent variable affirmative leaves a managed vault bootable."""
    assert ContainerSettings.from_environ(environ).managed is True


def test_self_connected_container_still_honours_cloud_consent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the marker, a container keeps the user's cloud opt-in."""
    from creek_mcp import container_runtime as runtime

    monkeypatch.setenv(CLOUD_CONSENT_ENV, "1")
    assert ContainerSettings.from_environ({CLOUD_CONSENT_ENV: "1"}).managed is False
    observed: dict[str, bool] = {}
    monkeypatch.setattr(runtime, "create_app", lambda *, verifier: verifier)
    monkeypatch.setattr(runtime, "announce_rotation_window", lambda _verifier: None)
    monkeypatch.setattr(
        runtime,
        "serve",
        lambda _app, _args: observed.update(consent=has_cloud_consent()),
    )

    run(_settings(tmp_path))

    assert observed == {"consent": True}


def test_managed_container_pins_consent_off_while_serving(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Consent set after a managed boot is still refused by every provider."""
    from creek_mcp import container_runtime as runtime

    settings = replace(_settings(tmp_path), managed=True)
    observed: dict[str, bool] = {}
    monkeypatch.setattr(runtime, "create_app", lambda *, verifier: verifier)
    monkeypatch.setattr(runtime, "announce_rotation_window", lambda _verifier: None)

    def fake_serve(_app: object, _args: object) -> None:
        monkeypatch.setenv(CLOUD_CONSENT_ENV, "1")
        observed["consent"] = has_cloud_consent()

    monkeypatch.setattr(runtime, "serve", fake_serve)

    run(settings)

    assert observed == {"consent": False}


def test_the_served_container_app_answers_mcp_with_the_mounted_consumer_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one entry point serves ``/v1`` and ``/mcp`` together, no new flag (#1962).

    Nothing is patched between the secret mount and the served app: the token
    the container read from its secret file is the bearer that completes an
    MCP handshake, and the handshake reports the network transport.

    Args:
        tmp_path: Pytest's per-test directory.
        monkeypatch: Captures the served app and restores the runtime env.
    """
    from starlette.testclient import TestClient

    from creek.classify.llm.local_boundary import LOOPBACK_ONLY_ENV
    from creek_mcp import container_runtime as runtime
    from creek_mcp.policy import Transport
    from tests.mcp_http_support import call_handshake, open_session

    for name in (CONFIG_PATH_ENV_VAR, "CREEK_VAULT_PATH", LOOPBACK_ONLY_ENV):
        monkeypatch.setenv(name, "")
    monkeypatch.delenv(CONSUMER_TOKENS_ENV, raising=False)
    monkeypatch.setattr(runtime, "announce_rotation_window", lambda _verifier: None)
    served: list[Starlette] = []
    monkeypatch.setattr(runtime, "serve", lambda app, _args: served.append(app))

    run(_settings(tmp_path))

    (app,) = served
    assert app.state.mcp_server is not None
    with TestClient(app) as test_client:
        session = open_session(test_client, _TOKEN)
        handshake = call_handshake(test_client, session["headers"])
    assert handshake["transport"] == Transport.NETWORK.value
