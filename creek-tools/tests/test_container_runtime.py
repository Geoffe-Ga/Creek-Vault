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
from tests.agent_token_support import FakeClock

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
    monkeypatch.setattr(runtime, "create_app", lambda *, verifier, **_kwargs: verifier)
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
    monkeypatch.setattr(runtime, "create_app", lambda *, verifier, **_kwargs: verifier)
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
    monkeypatch.setattr(runtime, "create_app", lambda *, verifier, **_kwargs: verifier)
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
    monkeypatch.setattr(runtime, "create_app", lambda *, verifier, **_kwargs: verifier)
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


def test_a_grant_suffix_in_the_consumer_secret_gates_the_served_consumer(
    tmp_path: Path,
) -> None:
    """``adepthood=<token>@vault:read`` gates that consumer on ``/mcp`` (#1964).

    The secret file is parsed by the same parser as the environment; dropping
    the suffix there would serve the consumer ungated while the operator
    believed it gated.

    Args:
        tmp_path: Pytest's per-test directory.
    """
    from creek_mcp.policy import Grant

    secret_path = tmp_path / "tokens"
    secret_path.write_text(f"adepthood={_TOKEN}@vault:read\n", encoding="utf-8")
    plain_path = tmp_path / "plain"
    plain_path.write_text(f"adepthood={_TOKEN}\n", encoding="utf-8")

    gated = load_consumer_secret(secret_path)
    ungated = load_consumer_secret(plain_path)

    assert gated.grants == frozenset({Grant.READ})
    assert gated.verifier().ungated_notice() is None
    assert ungated.grants is None
    notice = ungated.verifier().ungated_notice()
    assert notice is not None
    assert "adepthood" in notice
    assert _TOKEN not in repr(gated)


def test_runtime_announces_an_ungated_consumer_before_serving(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The container names an ungated consumer at start, as the network CLI does.

    Args:
        tmp_path: Pytest's per-test directory.
        monkeypatch: Records the announcement and stubs the server.
    """
    from creek.classify.llm.local_boundary import LOOPBACK_ONLY_ENV
    from creek_mcp import container_runtime as runtime

    for name in (CONFIG_PATH_ENV_VAR, "CREEK_VAULT_PATH", LOOPBACK_ONLY_ENV):
        monkeypatch.setenv(name, "")
    announced: list[object] = []
    served: list[object] = []
    monkeypatch.setattr(runtime, "announce_rotation_window", lambda _verifier: None)
    monkeypatch.setattr(runtime, "announce_ungated_consumers", announced.append)
    monkeypatch.setattr(runtime, "create_app", lambda *, verifier, **_kwargs: verifier)
    monkeypatch.setattr(runtime, "serve", lambda app, _args: served.append(app))

    run(_settings(tmp_path))

    assert announced == served
    assert len(announced) == 1


# --------------------------------------------------------------------------- #
# OAuth resource server settings (#1963)
# --------------------------------------------------------------------------- #

_RESOURCE = "https://v-7q2k.creek.example/mcp"
_ISSUER = "https://api.adepthood.example"


def _oauth_environ(tmp_path: Path, **extra: str) -> dict[str, str]:
    """Return a container environment with both resource-server settings.

    Args:
        tmp_path: Pytest's per-test directory.
        extra: Further settings.

    Returns:
        The environment.
    """
    from creek_mcp.oauth_resource import AUTHORIZATION_SERVER_ENV, RESOURCE_URL_ENV

    return {
        VAULT_PATH_ENV: str(tmp_path / "vault"),
        RESOURCE_URL_ENV: _RESOURCE,
        AUTHORIZATION_SERVER_ENV: _ISSUER,
        **extra,
    }


def test_the_resource_server_settings_load_from_the_environment(
    tmp_path: Path,
) -> None:
    """The two non-secret settings the control plane writes reach the settings."""
    settings = ContainerSettings.from_environ(_oauth_environ(tmp_path))

    assert settings.resource_server is not None
    assert settings.resource_server.resource_url == _RESOURCE
    assert settings.resource_server.authorization_server == _ISSUER


def test_no_resource_server_settings_is_a_bearer_only_container(
    tmp_path: Path,
) -> None:
    """Without them the container serves exactly as before #1963."""
    settings = ContainerSettings.from_environ({VAULT_PATH_ENV: str(tmp_path)})

    assert settings.resource_server is None


def test_a_half_configured_resource_server_refuses_startup(tmp_path: Path) -> None:
    """One setting without the other is a startup refusal, not half OAuth."""
    from creek_mcp.oauth_resource import AUTHORIZATION_SERVER_ENV

    environ = _oauth_environ(tmp_path)
    del environ[AUTHORIZATION_SERVER_ENV]

    with pytest.raises(ContainerConfigurationError, match="set together") as refused:
        ContainerSettings.from_environ(environ)

    assert _RESOURCE not in str(refused.value)


def _served_agent_verifier(
    settings: ContainerSettings, monkeypatch: pytest.MonkeyPatch
) -> object:
    """Run the container and return the agent verifier it served ``/mcp`` behind.

    Args:
        settings: The container settings.
        monkeypatch: Pytest's monkeypatch.

    Returns:
        The ``agent_verifier`` handed to ``create_app``.
    """
    from creek_mcp import container_runtime as runtime

    captured: dict[str, object] = {}

    def fake_create_app(*, verifier: object, agent_verifier: object) -> object:
        captured["agent"] = agent_verifier
        return verifier

    monkeypatch.delenv(CONSUMER_TOKENS_ENV, raising=False)
    monkeypatch.setattr(runtime, "create_app", fake_create_app)
    monkeypatch.setattr(runtime, "announce_rotation_window", lambda _verifier: None)
    monkeypatch.setattr(runtime, "announce_ungated_consumers", lambda _verifier: None)
    monkeypatch.setattr(runtime, "serve", lambda _app, _args: None)
    run(settings)
    return captured["agent"]


def test_a_managed_oauth_container_pins_the_subject_to_its_consumer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A managed Machine accepts only its own person's tokens."""
    from creek_mcp.agent_tokens import JwtAgentTokenVerifier
    from creek_mcp.oauth_resource import ResourceServerConfig

    config = ResourceServerConfig.build(
        resource_url=_RESOURCE, authorization_server=_ISSUER
    )
    settings = replace(_settings(tmp_path), managed=True, resource_server=config)

    agent = _served_agent_verifier(settings, monkeypatch)

    assert isinstance(agent, JwtAgentTokenVerifier)
    assert agent.config == config
    assert agent.expected_subject == "adepthood"


def test_a_self_hosted_oauth_container_does_not_pin_the_subject(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A self-hosted vault's static consumer name is not the person's ``sub``."""
    from creek_mcp.agent_tokens import JwtAgentTokenVerifier
    from creek_mcp.oauth_resource import ResourceServerConfig

    config = ResourceServerConfig.build(
        resource_url=_RESOURCE, authorization_server=_ISSUER
    )
    settings = replace(_settings(tmp_path), resource_server=config)

    agent = _served_agent_verifier(settings, monkeypatch)

    assert isinstance(agent, JwtAgentTokenVerifier)
    assert agent.expected_subject is None


def test_a_bearer_only_container_serves_no_agent_verifier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No resource server: ``/mcp`` keeps the static registry alone."""
    assert _served_agent_verifier(_settings(tmp_path), monkeypatch) is None


# --------------------------------------------------------------------------- #
# The consumer secret is followed, not read once (#1965)
# --------------------------------------------------------------------------- #

_ROTATED = "rotated-consumer-token-that-is-thirty-two-characters"


def _follow_on(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    """Put the reloading verifier on a hand-moved clock.

    Args:
        monkeypatch: Pytest's monkeypatch.

    Returns:
        The clock, to advance past the poll interval.
    """
    from creek_mcp import credential_reload

    clock = FakeClock()
    monkeypatch.setattr(credential_reload, "_monotonic", clock)
    return clock


def test_the_secret_records_the_digest_of_the_bytes_it_was_parsed_from(
    tmp_path: Path,
) -> None:
    """The reload baseline is the boot read itself, so no edit can slip between."""
    from creek_mcp.credential_reload import content_digest

    secret_path = tmp_path / "tokens"
    secret_path.write_text(f"adepthood={_TOKEN}\n", encoding="utf-8")

    secret = load_consumer_secret(secret_path)

    assert secret.source_digest == content_digest(secret_path.read_bytes())
    assert secret.source_digest not in repr(secret)


def test_a_secret_that_is_not_utf8_is_refused_at_boot(tmp_path: Path) -> None:
    """A decode failure is the same generic refusal as any invalid secret."""
    secret_path = tmp_path / "tokens"
    secret_path.write_bytes(b"adepthood=\xff" + _TOKEN.encode())

    with pytest.raises(ContainerConfigurationError, match="invalid") as caught:
        load_consumer_secret(secret_path)

    assert "0xff" not in str(caught.value)


def test_a_rotated_secret_reaches_v1_and_mcp_without_a_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Narrowing the mounted set revokes the old token on both surfaces (#1965).

    Nothing is patched between the secret mount and the served app: one
    verifier instance answers ``/v1`` and ``/mcp``, and it follows the file.
    The reload reads the file itself, so no token reaches the environment.

    Args:
        tmp_path: Pytest's per-test directory.
        monkeypatch: Captures the served app and moves the poll clock.
    """
    from starlette.testclient import TestClient

    from creek.classify.llm.local_boundary import LOOPBACK_ONLY_ENV
    from creek_mcp import container_runtime as runtime
    from creek_mcp.credential_reload import RELOAD_POLL_SECONDS
    from tests.mcp_http_support import initialize_body, mcp_headers, open_session
    from tests.v1_api_support import CAPABILITIES_PATH, headers

    for name in (CONFIG_PATH_ENV_VAR, "CREEK_VAULT_PATH", LOOPBACK_ONLY_ENV):
        monkeypatch.setenv(name, "")
    monkeypatch.delenv(CONSUMER_TOKENS_ENV, raising=False)
    clock = _follow_on(monkeypatch)
    served: list[Starlette] = []
    monkeypatch.setattr(runtime, "serve", lambda app, _args: served.append(app))
    settings = _settings(tmp_path)

    run(settings)

    (app,) = served
    with TestClient(app) as test_client:
        assert test_client.get(
            CAPABILITIES_PATH, headers=headers(token=_TOKEN)
        ).is_success
        settings.consumer_tokens_file.write_text(
            f"adepthood={_ROTATED}\n", encoding="utf-8"
        )
        clock.advance(RELOAD_POLL_SECONDS)
        old_v1 = test_client.get(CAPABILITIES_PATH, headers=headers(token=_TOKEN))
        new_v1 = test_client.get(CAPABILITIES_PATH, headers=headers(token=_ROTATED))
        old_mcp = test_client.post(
            "/mcp", json=initialize_body(), headers=mcp_headers(_TOKEN)
        )
        open_session(test_client, _ROTATED)

    assert old_v1.status_code == 401
    assert new_v1.status_code == 200
    assert old_mcp.status_code == 401
    assert CONSUMER_TOKENS_ENV not in os.environ
    assert all(_ROTATED not in value for value in os.environ.values())


def _following(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str
) -> tuple[Path, FakeClock, object]:
    """Boot a container secret from *content* and follow its file.

    Args:
        tmp_path: Pytest's per-test directory.
        monkeypatch: Moves the poll clock.
        content: The secret file's boot-time text.

    Returns:
        The file, the clock and the reloading verifier.
    """
    clock = _follow_on(monkeypatch)
    secret_path = tmp_path / "tokens"
    secret_path.write_text(content, encoding="utf-8")
    secret = load_consumer_secret(secret_path)
    return secret_path, clock, secret.reloading_verifier(secret_path)


def _client_of(verifier: object, token: str) -> str | None:
    """Return the consumer *token* authenticates as through *verifier*.

    Args:
        verifier: A token verifier.
        token: The bearer.

    Returns:
        The consumer, or ``None``.
    """
    import asyncio

    from creek_mcp.remote_auth import ConsumerTokenVerifier

    assert isinstance(verifier, ConsumerTokenVerifier)
    access = asyncio.run(verifier.verify_token(token))
    return None if access is None else access.client_id


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (f"renamed-identity={_ROTATED}\n", "different identity"),
        (
            f"adepthood={_TOKEN};renamed-identity={_ROTATED}\n",
            "exactly one identity",
        ),
        ("adepthood=short\n", "below the 32-char minimum"),
    ],
    ids=["renamed", "second-identity", "weak-token"],
)
def test_a_reload_the_container_would_refuse_at_boot_keeps_the_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    content: str,
    reason: str,
) -> None:
    """A container serves one identity for its life; a reload rotates its tokens.

    The limiter's buckets, the journal's single-owner rule and a managed
    vault's JWT subject pin are all built from the identity the container
    booted with, so a reload may not swap it.

    Args:
        tmp_path: Pytest's per-test directory.
        monkeypatch: Moves the poll clock.
        capsys: Captures the refusal.
        content: A secret the reload must refuse.
        reason: The refusal's wording.
    """
    from creek_mcp.credential_reload import RELOAD_POLL_SECONDS

    secret_path, clock, verifier = _following(
        tmp_path, monkeypatch, f"adepthood={_TOKEN}\n"
    )
    secret_path.write_text(content, encoding="utf-8")
    clock.advance(RELOAD_POLL_SECONDS)

    assert _client_of(verifier, _TOKEN) == "adepthood"
    assert _client_of(verifier, _ROTATED) is None
    err = capsys.readouterr().err
    assert reason in err
    assert "renamed-identity" not in err
    assert _TOKEN not in err
    assert _ROTATED not in err


def test_a_reload_regates_the_container_consumer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A grant suffix added to the mounted entry takes effect without a restart."""
    import asyncio

    from creek_mcp.credential_reload import RELOAD_POLL_SECONDS
    from creek_mcp.remote_auth import REMOTE_SCOPE, ConsumerTokenVerifier

    secret_path, clock, verifier = _following(
        tmp_path, monkeypatch, f"adepthood={_TOKEN}\n"
    )
    secret_path.write_text(f"adepthood={_TOKEN}@vault:read\n", encoding="utf-8")
    clock.advance(RELOAD_POLL_SECONDS)

    assert isinstance(verifier, ConsumerTokenVerifier)
    access = asyncio.run(verifier.verify_token(_TOKEN))
    assert access is not None
    assert access.scopes == [REMOTE_SCOPE, "vault:read"]


def test_the_container_serves_the_reloading_verifier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``run`` hands the app the verifier that follows the mounted file."""
    from creek_mcp import container_runtime as runtime
    from creek_mcp.credential_reload import ReloadingConsumerTokenVerifier

    monkeypatch.delenv(CONSUMER_TOKENS_ENV, raising=False)
    served: list[object] = []
    monkeypatch.setattr(runtime, "create_app", lambda *, verifier, **_kwargs: verifier)
    monkeypatch.setattr(runtime, "serve", lambda app, _args: served.append(app))

    run(_settings(tmp_path))

    (verifier,) = served
    assert isinstance(verifier, ReloadingConsumerTokenVerifier)
