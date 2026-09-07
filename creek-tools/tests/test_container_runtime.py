"""Contract tests for the one-vault container runtime (#1772)."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest
import yaml

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
    assert os.environ[CONFIG_PATH_ENV_VAR] == str(settings.config_path)
