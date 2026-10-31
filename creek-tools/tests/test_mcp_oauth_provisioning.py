"""Managed allocations as OAuth resource servers, and their rollout (#1974).

The control plane writes ``CREEK_MCP_RESOURCE_URL`` and
``CREEK_MCP_AUTHORIZATION_SERVER`` into a managed vault's Machine environment
from the activation's own encrypted record, never from fleet policy, so a
newly provisioned vault boots as a resource server while every allocation
provisioned before the change keeps its exact legacy environment — and keeps
routing — until it is deliberately rolled.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

import httpx
import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from creek_mcp.mcp_path import MCP_PATH, PROTECTED_RESOURCE_PATH
from creek_mcp.oauth_resource import AUTHORIZATION_SERVER_ENV, RESOURCE_URL_ENV
from creek_mcp.provisioning import (
    mcp_oauth_rollout,
    pilot_cli,
    production_secrets,
    worker_cli,
)
from creek_mcp.provisioning.driver import ProviderError
from creek_mcp.provisioning.models import (
    FailureReason,
    McpOAuthSettings,
    RoutableAllocation,
    mcp_resource_url,
    public_vault_url,
)
from creek_mcp.provisioning.production_secrets import (
    EncryptedFileFlySecretManager,
    ReadOnlyFlySecretManager,
)
from tests.fly_support import (
    MCP_ISSUER,
    MCP_RESOURCE_URL,
    ROUTING_PUBLIC_URL,
    FakeFlyAPI,
    FakeSecretManager,
    fly_driver,
    fly_job,
    mcp_oauth_settings,
)
from tests.test_provisioning_production_adapters import _NOW, _ca_files, _owner_file

if TYPE_CHECKING:
    from pathlib import Path

    from creek_mcp.provisioning.fly import FlyProviderDriver

_ACTIVATION: Final[str] = "activation-fly-001"
_REQUESTER: Final[str] = "adepthood"
_CONSUMER: Final[str] = "adepthood-user-001"
_PILOT_ORIGIN: Final[str] = "https://creek-control-pilot.fly.dev"


def _routable(allocation_id: str) -> RoutableAllocation:
    """Return the store-owned routing record for the default job."""
    return RoutableAllocation(
        "job-fly-001", _ACTIVATION, _REQUESTER, _CONSUMER, allocation_id
    )


def _env(api: FakeFlyAPI) -> dict[str, Any]:
    """Return the one Machine's environment."""
    (machines,) = api.machines.values()
    (machine,) = machines
    env = machine["config"]["env"]
    assert isinstance(env, dict)
    return env


def _machine(api: FakeFlyAPI) -> dict[str, Any]:
    """Return the one Machine."""
    (machines,) = api.machines.values()
    (machine,) = machines
    return machine


def _legacy_allocation(
    api: FakeFlyAPI, *, replay: bool = True
) -> tuple[FakeSecretManager, FlyProviderDriver, str]:
    """Provision an allocation as the control plane did before #1974.

    Returns:
        The secret manager — now configured with settings, as a control plane
        is after this change ships — a driver over it, and the allocation id.
    """
    secrets = FakeSecretManager(set())
    driver = fly_driver(api, secrets, fly_replay_enabled=replay)
    allocation = driver.provision(fly_job())
    secrets.mcp_oauth = mcp_oauth_settings()
    return secrets, driver, allocation.allocation_id


# --------------------------------------------------------------------------- #
# The one derivation both sides of the handoff share
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "origin",
    [ROUTING_PUBLIC_URL, f"{ROUTING_PUBLIC_URL}/", _PILOT_ORIGIN],
)
def test_the_resource_url_is_adepthoods_derivation_of_the_handoff_url(
    origin: str,
) -> None:
    """Adepthood drops ``/v1`` from the vault URL it is handed and adds ``/mcp``."""
    vault_url = public_vault_url(origin)
    adepthood_derivation = vault_url.removesuffix("/v1") + "/mcp"

    assert vault_url == f"{origin.rstrip('/')}/v1"
    assert mcp_resource_url(vault_url) == adepthood_derivation
    assert mcp_resource_url(vault_url) == f"{origin.rstrip('/')}{MCP_PATH}"


def test_provisioning_hands_off_the_url_the_machine_names_as_its_resource() -> None:
    """The handed-off ``/v1`` URL and the Machine's resource URL agree exactly."""
    api = FakeFlyAPI()
    secrets = FakeSecretManager(set(), mcp_oauth=mcp_oauth_settings())
    allocation = fly_driver(api, secrets, fly_replay_enabled=True).provision(fly_job())

    adepthood_derivation = allocation.vault_url.removesuffix("/v1") + "/mcp"
    assert _env(api)[RESOURCE_URL_ENV] == adepthood_derivation


def test_a_handoff_url_without_v1_has_no_resource() -> None:
    """Only a ``/v1`` vault URL names a resource; anything else is refused."""
    with pytest.raises(ValueError, match="/v1"):
        mcp_resource_url(f"{ROUTING_PUBLIC_URL}/v2")


# --------------------------------------------------------------------------- #
# The settings value
# --------------------------------------------------------------------------- #


def test_settings_build_from_the_vault_url_and_never_print_their_values() -> None:
    """The value derives the resource and keeps both URLs out of its repr."""
    settings = McpOAuthSettings.for_vault_url(
        public_vault_url(ROUTING_PUBLIC_URL), MCP_ISSUER
    )

    assert settings == mcp_oauth_settings()
    assert settings.runtime_env() == {
        RESOURCE_URL_ENV: MCP_RESOURCE_URL,
        AUTHORIZATION_SERVER_ENV: MCP_ISSUER,
    }
    assert MCP_ISSUER not in repr(settings)
    assert ROUTING_PUBLIC_URL not in repr(settings)
    assert McpOAuthSettings.decode(settings.encode()) == settings
    assert McpOAuthSettings.decode(None) is None


@pytest.mark.parametrize(
    ("resource", "issuer"),
    [
        ("http://vault-router.example.com/mcp", MCP_ISSUER),
        ("http://127.0.0.1/mcp", MCP_ISSUER),
        (f"{ROUTING_PUBLIC_URL}/v1", MCP_ISSUER),
        (MCP_RESOURCE_URL, "http://adepthood.example"),
        (f" {MCP_RESOURCE_URL}", MCP_ISSUER),
        (MCP_RESOURCE_URL, f"{MCP_ISSUER} "),
        (MCP_RESOURCE_URL, ""),
    ],
)
def test_settings_refuse_anything_a_managed_vault_would_refuse(
    resource: str, issuer: str
) -> None:
    """A managed vault's settings are public https, verbatim, and both present."""
    with pytest.raises(ValueError, match=r"CREEK_MCP_|verbatim|https") as raised:
        McpOAuthSettings(resource, issuer)

    assert MCP_ISSUER not in str(raised.value)
    assert "vault-router" not in str(raised.value)


@pytest.mark.parametrize(
    "stored",
    [
        "not-a-document",
        {"resource_url": MCP_RESOURCE_URL},
        {"resource_url": MCP_RESOURCE_URL, "authorization_server": 1},
        {
            "resource_url": MCP_RESOURCE_URL,
            "authorization_server": MCP_ISSUER,
            "extra": "x",
        },
    ],
)
def test_a_malformed_stored_value_is_refused(stored: object) -> None:
    """Only the exact stored shape decodes."""
    with pytest.raises(ValueError, match="stored MCP OAuth settings are invalid"):
        McpOAuthSettings.decode(stored)


# --------------------------------------------------------------------------- #
# A newly provisioned allocation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("replay", [True, False])
def test_a_new_allocation_boots_with_both_settings(replay: bool) -> None:
    """Both runtimes write the two settings beside the managed marker."""
    api = FakeFlyAPI()
    secrets = FakeSecretManager(set(), mcp_oauth=mcp_oauth_settings())
    driver = fly_driver(api, secrets, fly_replay_enabled=replay)

    allocation = driver.provision(fly_job())
    if replay:
        driver.prepare_replay(_routable(allocation.allocation_id))
    else:
        driver.prepare_route(_routable(allocation.allocation_id))

    env = _env(api)
    assert env[RESOURCE_URL_ENV] == MCP_RESOURCE_URL
    assert env[AUTHORIZATION_SERVER_ENV] == MCP_ISSUER
    assert env["CREEK_MANAGED_VAULT"] == "1"


def test_a_new_allocation_with_its_settings_stripped_is_refused() -> None:
    """A record with settings verifies only the exact new environment."""
    api = FakeFlyAPI()
    secrets = FakeSecretManager(set(), mcp_oauth=mcp_oauth_settings())
    driver = fly_driver(api, secrets, fly_replay_enabled=True)
    allocation = driver.provision(fly_job())
    del _env(api)[AUTHORIZATION_SERVER_ENV]

    with pytest.raises(ProviderError) as raised:
        driver.prepare_replay(_routable(allocation.allocation_id))

    assert raised.value.reason is FailureReason.PROVIDER_REJECTED


def test_a_new_allocation_naming_another_issuer_is_refused() -> None:
    """The environment must carry the record's values, not merely the keys."""
    api = FakeFlyAPI()
    secrets = FakeSecretManager(set(), mcp_oauth=mcp_oauth_settings())
    driver = fly_driver(api, secrets, fly_replay_enabled=True)
    allocation = driver.provision(fly_job())
    _env(api)[AUTHORIZATION_SERVER_ENV] = "https://issuer.attacker.example"

    with pytest.raises(ProviderError):
        driver.prepare_replay(_routable(allocation.allocation_id))


def test_a_new_replay_vault_publishes_its_resource_and_pins_its_subject(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The booted vault names its own resource and Adepthood's issuer.

    The Machine's written environment, plus the coordinates Fly injects, is
    what the replay vault entry point reads; its served metadata names the
    allocation's resource URL and the issuer, and its agent verifier stays
    pinned to the one consumer identity the mounted secret names.
    """
    from starlette.testclient import TestClient

    from creek.config import generate_default_config
    from creek_mcp import fly_vault_runtime as runtime
    from creek_mcp.container_runtime import (
        BootstrapState,
        PreparedVault,
        SingleConsumerSecret,
        agent_verifier_for,
    )
    from tests.v1_api_support import seed_vault

    api = FakeFlyAPI()
    secrets = FakeSecretManager(set(), mcp_oauth=mcp_oauth_settings())
    fly_driver(api, secrets, fly_replay_enabled=True).provision(fly_job())
    written = _env(api)
    app_name = written["CREEK_CONTAINER_EXPECTED_FLY_APP"]
    vault = seed_vault(tmp_path)
    config = tmp_path / "creek.yaml"
    generate_default_config(config, vault_path=vault)
    state = "s" * 64
    replay_state = _owner_file(tmp_path / "replay-state", state.encode())
    environ = {
        **written,
        "FLY_APP_NAME": app_name,
        "FLY_MACHINE_ID": "machine-1",
        "FLY_REGION": "iad",
        "FLY_PRIVATE_IP": "fdaa:0:1::2",
        "CREEK_CONTAINER_VAULT_PATH": str(vault),
        "CREEK_CONTAINER_FLY_REPLAY_STATE_FILE": str(replay_state),
    }
    settings = runtime.FlyVaultSettings.from_environ(environ)
    secret = SingleConsumerSecret(_CONSUMER, ("t" * 48,))
    monkeypatch.setattr(
        runtime,
        "prepare_vault",
        lambda _settings: PreparedVault(config, BootstrapState.EXISTING),
    )
    monkeypatch.setattr(runtime, "load_consumer_secret", lambda _path: secret)
    monkeypatch.setattr(runtime, "announce_rotation_window", lambda _verifier: None)
    served: list[Any] = []
    monkeypatch.setattr(runtime, "serve", lambda app, _args: served.append(app))

    runtime.run(settings)

    (app,) = served
    replay = f"instance=router-machine;region=iad;t=1727395200;state={state}"
    with TestClient(app, headers={"Fly-Replay-Src": replay}) as client:
        response = client.get(PROTECTED_RESOURCE_PATH)
    assert response.status_code == 200
    assert response.json()["resource"] == MCP_RESOURCE_URL
    assert response.json()["authorization_servers"] == [MCP_ISSUER]
    verifier = agent_verifier_for(settings.container, secret)
    assert verifier is not None
    assert verifier.expected_subject == _CONSUMER


# --------------------------------------------------------------------------- #
# An allocation provisioned before #1974
# --------------------------------------------------------------------------- #


def test_a_legacy_allocation_keeps_routing_on_its_exact_legacy_env() -> None:
    """A record that predates the settings keeps verifying the legacy env."""
    api = FakeFlyAPI()
    _secrets, driver, allocation_id = _legacy_allocation(api)

    target = driver.prepare_replay(_routable(allocation_id))

    assert target.machine_id == "machine-1"
    assert RESOURCE_URL_ENV not in _env(api)
    assert AUTHORIZATION_SERVER_ENV not in _env(api)


def test_a_legacy_allocation_carrying_the_settings_is_refused() -> None:
    """No blanket relaxation: a record without settings refuses an env with them."""
    api = FakeFlyAPI()
    _secrets, driver, allocation_id = _legacy_allocation(api)
    _env(api).update(mcp_oauth_settings().runtime_env())

    with pytest.raises(ProviderError) as raised:
        driver.prepare_replay(_routable(allocation_id))

    assert raised.value.reason is FailureReason.PROVIDER_REJECTED


def test_a_rolled_record_refuses_its_unrolled_machine() -> None:
    """Once the record carries the settings, only the exact new env routes."""
    api = FakeFlyAPI()
    secrets, driver, allocation_id = _legacy_allocation(api)
    secrets.issued[_ACTIVATION] = dataclasses.replace(
        secrets.issued[_ACTIVATION], mcp_oauth=mcp_oauth_settings()
    )

    with pytest.raises(ProviderError):
        driver.prepare_replay(_routable(allocation_id))


# --------------------------------------------------------------------------- #
# Rolling a legacy allocation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("replay", [True, False])
def test_a_rolled_allocation_routes_on_its_exact_new_env(replay: bool) -> None:
    """The roll writes the record, updates the stopped Machine, and still routes."""
    api = FakeFlyAPI()
    secrets, driver, allocation_id = _legacy_allocation(api, replay=replay)

    rolled = driver.roll_mcp_oauth(_routable(allocation_id), secrets)

    assert rolled is True
    assert secrets.issued[_ACTIVATION].mcp_oauth == mcp_oauth_settings()
    (update,) = api.updates
    assert update["skip_launch"] is True
    assert _machine(api)["state"] == "stopped"
    assert _env(api)[RESOURCE_URL_ENV] == MCP_RESOURCE_URL
    assert _env(api)[AUTHORIZATION_SERVER_ENV] == MCP_ISSUER
    if replay:
        driver.prepare_replay(_routable(allocation_id))
    else:
        driver.prepare_route(_routable(allocation_id))


def test_rolling_a_current_allocation_changes_nothing() -> None:
    """A second roll, or a roll of a new allocation, issues no update."""
    api = FakeFlyAPI()
    secrets, driver, allocation_id = _legacy_allocation(api)
    driver.roll_mcp_oauth(_routable(allocation_id), secrets)

    assert driver.roll_mcp_oauth(_routable(allocation_id), secrets) is False
    assert len(api.updates) == 1


def test_a_running_machine_is_not_rolled_and_keeps_routing() -> None:
    """The roll refuses retryably before touching the record or the Machine."""
    api = FakeFlyAPI()
    secrets, driver, allocation_id = _legacy_allocation(api)
    driver.prepare_replay(_routable(allocation_id))

    with pytest.raises(ProviderError) as raised:
        driver.roll_mcp_oauth(_routable(allocation_id), secrets)

    assert raised.value.retryable is True
    assert raised.value.reason is FailureReason.PROVIDER_UNAVAILABLE
    assert secrets.issued[_ACTIVATION].mcp_oauth is None
    assert api.updates == []
    driver.prepare_replay(_routable(allocation_id))


def test_a_drifted_machine_is_not_rolled() -> None:
    """Only the exact legacy or exact new Machine is admitted to the roll."""
    api = FakeFlyAPI()
    secrets, driver, allocation_id = _legacy_allocation(api)
    _env(api)["CREEK_EXTRA"] = "drift"

    with pytest.raises(ProviderError) as raised:
        driver.roll_mcp_oauth(_routable(allocation_id), secrets)

    assert raised.value.reason is FailureReason.PROVIDER_REJECTED
    assert secrets.issued[_ACTIVATION].mcp_oauth is None
    assert api.updates == []


def test_a_roll_interrupted_after_the_record_resumes_on_retry() -> None:
    """A failed Machine update leaves a retry that completes the transition."""
    api = FakeFlyAPI()
    secrets, driver, allocation_id = _legacy_allocation(api)
    machine_path = f"/machines/{_machine(api)['id']}"
    api.fail_once("POST", machine_path)

    with pytest.raises(ProviderError):
        driver.roll_mcp_oauth(_routable(allocation_id), secrets)
    with pytest.raises(ProviderError):
        driver.prepare_replay(_routable(allocation_id))

    assert driver.roll_mcp_oauth(_routable(allocation_id), secrets) is True
    driver.prepare_replay(_routable(allocation_id))


def test_a_roll_without_configured_settings_writes_nothing() -> None:
    """With no settings to write, the record and Machine stay legacy."""
    api = FakeFlyAPI()
    secrets, driver, allocation_id = _legacy_allocation(api)
    secrets.mcp_oauth = None

    with pytest.raises(ValueError, match="no MCP OAuth settings"):
        driver.roll_mcp_oauth(_routable(allocation_id), secrets)

    assert api.updates == []
    driver.prepare_replay(_routable(allocation_id))


def test_a_roll_whose_record_stays_empty_is_refused() -> None:
    """A record writer that writes nothing cannot produce an unverified update."""

    class _SilentRecords:
        def roll_mcp_oauth(self, activation_id: str) -> McpOAuthSettings:
            del activation_id
            return mcp_oauth_settings()

        def mcp_oauth_is_current(self, settings: McpOAuthSettings) -> bool:
            del settings
            return False

    api = FakeFlyAPI()
    _secrets, driver, allocation_id = _legacy_allocation(api)

    with pytest.raises(ProviderError) as raised:
        driver.roll_mcp_oauth(_routable(allocation_id), _SilentRecords())

    assert raised.value.reason is FailureReason.PROVIDER_REJECTED
    assert api.updates == []


def test_a_roll_for_another_allocation_is_refused() -> None:
    """The store-owned allocation id must match the activation's derived one."""
    api = FakeFlyAPI()
    secrets, driver, _allocation_id = _legacy_allocation(api)

    with pytest.raises(ProviderError):
        driver.roll_mcp_oauth(_routable("fly-" + "0" * 24), secrets)


# --------------------------------------------------------------------------- #
# The encrypted per-activation record
# --------------------------------------------------------------------------- #


def _manager(
    tmp_path: Path, *, mcp_oauth: McpOAuthSettings | None
) -> EncryptedFileFlySecretManager:
    """Return an encrypted manager over one shared state root under *tmp_path*."""
    state = tmp_path / "runtime-secrets"
    if not state.exists():
        state.mkdir(mode=0o700)
        _owner_file(tmp_path / "master-key", b"m" * 32)
        _ca_files(tmp_path)
    return EncryptedFileFlySecretManager(
        state,
        master_key_file=tmp_path / "master-key",
        ca_certificate_file=tmp_path / "ca.crt",
        ca_private_key_file=tmp_path / "ca.key",
        app_prefix="creek-vault",
        clock=lambda: _NOW,
        mcp_oauth=mcp_oauth,
    )


def _issue(manager: EncryptedFileFlySecretManager | ReadOnlyFlySecretManager) -> Any:
    """Issue or load the default activation's bundle."""
    return manager.issue(_ACTIVATION, _CONSUMER, requester_identity=_REQUESTER)


def test_a_new_record_stores_the_settings_encrypted(tmp_path: Path) -> None:
    """The settings ride the AES-GCM bundle and are read back by the router."""
    issued = _issue(_manager(tmp_path, mcp_oauth=mcp_oauth_settings()))
    reader = ReadOnlyFlySecretManager(
        tmp_path / "runtime-secrets", master_key_file=tmp_path / "master-key"
    )

    (ciphertext,) = (tmp_path / "runtime-secrets").glob("*.bundle")
    assert issued.mcp_oauth == mcp_oauth_settings()
    assert _issue(reader).mcp_oauth == mcp_oauth_settings()
    assert b"adepthood.example" not in ciphertext.read_bytes()
    assert MCP_ISSUER not in repr(issued)


def test_a_record_issued_before_the_settings_keeps_none(tmp_path: Path) -> None:
    """A restart with settings configured does not rewrite an existing record."""
    _issue(_manager(tmp_path, mcp_oauth=None))

    reloaded = _issue(_manager(tmp_path, mcp_oauth=mcp_oauth_settings()))

    assert reloaded.mcp_oauth is None


def test_a_record_written_before_the_schema_change_decodes_as_none(
    tmp_path: Path,
) -> None:
    """A stored document with no ``mcp_oauth`` member is the legacy shape."""
    manager = _manager(tmp_path, mcp_oauth=mcp_oauth_settings())
    _issue(manager)
    (bundle,) = (tmp_path / "runtime-secrets").glob("*.bundle")
    cipher = AESGCM(b"m" * 32)
    aad = production_secrets._aad(bundle.stem)
    raw = bundle.read_bytes()
    document = json.loads(cipher.decrypt(raw[:12], raw[12:], aad))
    del document["mcp_oauth"]
    bundle.write_bytes(
        raw[:12] + cipher.encrypt(raw[:12], json.dumps(document).encode(), aad)
    )

    assert _issue(manager).mcp_oauth is None


def test_rolling_a_record_writes_the_settings_once(tmp_path: Path) -> None:
    """The roll is idempotent and leaves every other bundle member untouched."""
    before = _issue(_manager(tmp_path, mcp_oauth=None))
    manager = _manager(tmp_path, mcp_oauth=mcp_oauth_settings())

    assert manager.roll_mcp_oauth(_ACTIVATION) == mcp_oauth_settings()
    assert manager.roll_mcp_oauth(_ACTIVATION) == mcp_oauth_settings()

    after = _issue(manager)
    assert after.mcp_oauth == mcp_oauth_settings()
    assert dataclasses.replace(after, mcp_oauth=None) == before
    assert sorted(path.suffix for path in (tmp_path / "runtime-secrets").iterdir()) == [
        ".bundle"
    ]


def test_rolling_a_record_refuses_unconfigured_unknown_revoked_or_other(
    tmp_path: Path,
) -> None:
    """The roll writes only configured settings into one live legacy record."""
    _issue(_manager(tmp_path, mcp_oauth=McpOAuthSettings(MCP_RESOURCE_URL, MCP_ISSUER)))
    other = McpOAuthSettings(MCP_RESOURCE_URL, "https://issuer.other.example")

    with pytest.raises(ValueError, match="no MCP OAuth settings"):
        _manager(tmp_path, mcp_oauth=None).roll_mcp_oauth(_ACTIVATION)
    with pytest.raises(ValueError, match="other MCP OAuth settings"):
        _manager(tmp_path, mcp_oauth=other).roll_mcp_oauth(_ACTIVATION)
    with pytest.raises(ValueError, match="unreadable"):
        _manager(tmp_path, mcp_oauth=other).roll_mcp_oauth("activation-never-issued")
    manager = _manager(tmp_path, mcp_oauth=mcp_oauth_settings())
    manager.revoke(_ACTIVATION)
    with pytest.raises(ValueError, match="revoked"):
        manager.roll_mcp_oauth(_ACTIVATION)


# --------------------------------------------------------------------------- #
# Composition: the worker, the pilot, and the one-shot roll
# --------------------------------------------------------------------------- #


def _worker_bundle(tmp_path: Path, *extra: str) -> worker_cli.WorkerBundle:
    """Compose the production worker over synthetic mounts."""
    from tests.test_provisioning_worker_cli import _NOW as WORKER_NOW
    from tests.test_provisioning_worker_cli import _arguments

    parser = worker_cli.build_parser()
    args = parser.parse_args([*_arguments(tmp_path), *extra])
    return worker_cli.compose(
        args,
        parser,
        clock=lambda: WORKER_NOW,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(404, request=request)
        ),
    )


def test_the_worker_writes_settings_derived_from_its_public_route(
    tmp_path: Path,
) -> None:
    """``--oauth-authorization-server`` configures every new activation."""
    bundle = _worker_bundle(tmp_path, "--oauth-authorization-server", MCP_ISSUER)
    try:
        assert bundle.secrets._mcp_oauth == mcp_oauth_settings()
    finally:
        bundle.close()


def test_the_worker_without_an_issuer_writes_no_settings(tmp_path: Path) -> None:
    """Without an issuer new activations are issued exactly as before."""
    bundle = _worker_bundle(tmp_path)
    try:
        assert bundle.secrets._mcp_oauth is None
    finally:
        bundle.close()


def test_the_worker_refuses_an_unsafe_issuer(tmp_path: Path) -> None:
    """A plain-http issuer fails composition, naming no value."""
    with pytest.raises(SystemExit):
        _worker_bundle(
            tmp_path, "--oauth-authorization-server", "http://adepthood.example"
        )


def test_the_pilot_edge_and_its_vaults_name_one_resource(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The edge's verified audience equals what each vault Machine is written."""
    from tests.test_provisioning_pilot_cli import (
        _app,
        _arguments,
        _RouterBundle,
        _WorkerBundle,
    )

    parser = pilot_cli.build_parser()
    args = parser.parse_args(
        [*_arguments(tmp_path), "--oauth-authorization-server", MCP_ISSUER]
    )
    written: list[McpOAuthSettings | None] = []

    def fake_worker(
        namespace: Any, _parser: object, *, fly_replay_enabled: bool
    ) -> Any:
        del fly_replay_enabled
        written.append(worker_cli.mcp_oauth_settings(namespace))
        return _WorkerBundle(object(), datetime.now(tz=UTC), timedelta(seconds=1))

    monkeypatch.setattr(pilot_cli.control_cli, "compose", lambda *_: _app("c"))
    monkeypatch.setattr(pilot_cli.worker_cli, "compose", fake_worker)
    monkeypatch.setattr(
        pilot_cli.routing_cli, "compose_replay", lambda *_: _RouterBundle(_app("r"))
    )

    pilot_cli.compose(args, parser).close()

    (settings,) = written
    assert settings is not None
    assert settings.resource_url == args.oauth_resource_url
    assert settings.resource_url == f"{_PILOT_ORIGIN}{MCP_PATH}"
    assert settings.authorization_server == MCP_ISSUER


class _Store:
    """A store double answering one ownership lookup."""

    def __init__(self, allocation: RoutableAllocation | None) -> None:
        self.allocation = allocation

    def get_routable_allocation(
        self, requester_identity: str, consumer_identity: str
    ) -> RoutableAllocation | None:
        if not consumer_identity.strip():
            raise ValueError("consumer_identity must contain characters")
        assert (requester_identity, consumer_identity) == (_REQUESTER, _CONSUMER)
        return self.allocation


def _roll(store: _Store, driver: FlyProviderDriver, secrets: FakeSecretManager) -> int:
    """Run the one-shot roll for the default owner pair."""
    return mcp_oauth_rollout.roll_owned_allocation(
        store,
        driver,
        secrets,
        requester_identity=_REQUESTER,
        consumer_identity=_CONSUMER,
    )


def test_the_one_shot_roll_maps_every_outcome_to_an_exit_status(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Rolled, current, absent, running and drifted each exit distinctly."""
    caplog.set_level(logging.INFO)
    api = FakeFlyAPI()
    secrets, driver, allocation_id = _legacy_allocation(api)
    owned = _Store(_routable(allocation_id))

    assert _roll(_Store(None), driver, secrets) == mcp_oauth_rollout.EXIT_NOT_FOUND
    driver.start(_ACTIVATION)
    assert _roll(owned, driver, secrets) == mcp_oauth_rollout.EXIT_RETRY
    driver.stop(_ACTIVATION)
    assert _roll(owned, driver, secrets) == mcp_oauth_rollout.EXIT_ROLLED
    assert _roll(owned, driver, secrets) == mcp_oauth_rollout.EXIT_ROLLED
    _env(api)["CREEK_EXTRA"] = "drift"
    assert _roll(owned, driver, secrets) == mcp_oauth_rollout.EXIT_REFUSED

    logged = caplog.text
    assert "rolled" in logged
    assert "already-current" in logged
    assert _CONSUMER not in logged
    assert MCP_ISSUER not in logged
    assert ROUTING_PUBLIC_URL not in logged


def test_the_one_shot_roll_refuses_an_unconfigured_or_invalid_request() -> None:
    """A record that cannot take the settings, or a bad owner, is refused."""
    api = FakeFlyAPI()
    secrets, driver, allocation_id = _legacy_allocation(api)
    secrets.mcp_oauth = None

    assert (
        _roll(_Store(_routable(allocation_id)), driver, secrets)
        == mcp_oauth_rollout.EXIT_REFUSED
    )
    assert (
        mcp_oauth_rollout.roll_owned_allocation(
            _Store(None),
            driver,
            secrets,
            requester_identity=_REQUESTER,
            consumer_identity=" ",
        )
        == mcp_oauth_rollout.EXIT_REFUSED
    )


def test_the_worker_roll_resolves_through_the_durable_store(tmp_path: Path) -> None:
    """The worker's roll asks the real store, which owns no such allocation."""
    bundle = _worker_bundle(tmp_path, "--oauth-authorization-server", MCP_ISSUER)
    args = argparse.Namespace(
        database=tmp_path / "jobs.sqlite3", roll_mcp_oauth=[_REQUESTER, _CONSUMER]
    )
    try:
        status = worker_cli.run_mcp_oauth_roll(args, bundle.driver, bundle.secrets)
    finally:
        bundle.close()

    assert status == mcp_oauth_rollout.EXIT_NOT_FOUND


@dataclasses.dataclass
class _ClosingBundle:
    """A composed bundle double that records its close and refuses to serve."""

    driver: object = None
    secrets: object = None
    closed: bool = False

    @property
    def worker(self) -> _ClosingBundle:
        return self

    @property
    def supervisor(self) -> Any:
        raise AssertionError("a one-shot roll must not start the supervisor")

    def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize("module", ["worker", "pilot"])
def test_main_runs_only_the_roll_and_exits_with_its_status(
    module: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--roll-mcp-oauth`` rolls one allocation, closes, and claims nothing."""
    bundle = _ClosingBundle()
    target = worker_cli if module == "worker" else pilot_cli
    monkeypatch.setattr(target, "build_parser", _roll_only_parser)
    monkeypatch.setattr(target, "compose", lambda *_: bundle)
    monkeypatch.setattr(
        worker_cli,
        "run_mcp_oauth_roll",
        lambda *_: mcp_oauth_rollout.EXIT_RETRY,
    )

    with pytest.raises(SystemExit) as raised:
        target.main(["--roll-mcp-oauth", _REQUESTER, _CONSUMER])

    assert raised.value.code == mcp_oauth_rollout.EXIT_RETRY
    assert bundle.closed is True


def _roll_only_parser() -> argparse.ArgumentParser:
    """Return a parser carrying only the roll selector."""
    parser = argparse.ArgumentParser()
    worker_cli.add_mcp_oauth_roll_argument(parser)
    return parser
