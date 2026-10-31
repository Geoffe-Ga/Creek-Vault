"""Roll existing allocations off the shared edge audience (#2004, R3-1).

#2002 gave every *new* allocation its own resource, ``<edge>/a/<slug>/mcp``,
and #2003 routes it at the edge. This suite pins the migration of every
allocation that already exists on the shared ``<edge>/mcp`` audience:

* a second roll transition, shared resource to per-allocation resource,
  recognized exactly — the record holds the configured shared edge resource,
  the Machine is exactly that record and stopped — mints the slug once, writes
  the record, then updates the Machine; an interrupted roll resumes with the
  slug it already minted;
* a running Machine is refused retryably and anything drifted is refused, both
  with record and Machine untouched;
* the rollout reports each allocation ``rolled`` / ``already-current`` /
  ``refused(<reason>)`` and logs nothing that names a vault;
* a fleet check lists every allocation not yet on its own resource, and the
  edge refuses to start with ``--refuse-shared-mcp-audience`` while it is
  non-empty.
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
from typing import TYPE_CHECKING, Any, Final

import pytest

from creek_mcp.mcp_path import MCP_PATH, is_allocation_mcp_path
from creek_mcp.oauth_resource import AUTHORIZATION_SERVER_ENV, RESOURCE_URL_ENV
from creek_mcp.provisioning import mcp_oauth_rollout, pilot_cli, routing_cli, worker_cli
from creek_mcp.provisioning.driver import ProviderError
from creek_mcp.provisioning.models import (
    FailureReason,
    McpOAuthSettings,
    RoutableAllocation,
)
from creek_mcp.provisioning.production_secrets import (
    EncryptedFileFlySecretManager,
    EncryptedFileRoutingCredentialVerifier,
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
_OTHER_ACTIVATION: Final[str] = "activation-fly-002"
_REQUESTER: Final[str] = "adepthood"
_CONSUMER: Final[str] = "adepthood-user-001"
_SLUG: Final[str] = "S" * 32
_PER_ALLOCATION: Final[McpOAuthSettings] = McpOAuthSettings(
    f"{ROUTING_PUBLIC_URL}/a/{_SLUG}{MCP_PATH}", MCP_ISSUER
)
_FOREIGN_EDGE: Final[str] = "https://other-router.example.com"


def _routable(allocation_id: str, activation: str = _ACTIVATION) -> RoutableAllocation:
    """Return the store-owned routing record for one provisioned job."""
    return RoutableAllocation(
        "job-fly-001", activation, _REQUESTER, _CONSUMER, allocation_id
    )


def _machine(api: FakeFlyAPI, index: int = 0) -> dict[str, Any]:
    """Return the *index*-th app's one Machine."""
    machines = list(api.machines.values())[index]
    (machine,) = machines
    return machine


def _env(api: FakeFlyAPI, index: int = 0) -> dict[str, Any]:
    """Return the *index*-th Machine's environment."""
    env = _machine(api, index)["config"]["env"]
    assert isinstance(env, dict)
    return env


def _shared_allocation(
    api: FakeFlyAPI,
) -> tuple[FakeSecretManager, FlyProviderDriver, str]:
    """Provision an allocation on the shared audience, as before #2002's flag.

    Returns:
        The secret manager — now asked for per-allocation resources, as the
        roll is — a driver over it, and the allocation id.
    """
    secrets = FakeSecretManager(set(), mcp_oauth=mcp_oauth_settings())
    driver = fly_driver(api, secrets, fly_replay_enabled=True)
    allocation = driver.provision(fly_job())
    secrets.per_allocation_mcp_resource = True
    return secrets, driver, allocation.allocation_id


def _stored(secrets: FakeSecretManager) -> McpOAuthSettings | None:
    """Return the default activation's stored settings."""
    return secrets.issued[_ACTIVATION].mcp_oauth


# --------------------------------------------------------------------------- #
# Settings: a per-allocation resource names the shared edge it scopes
# --------------------------------------------------------------------------- #


def test_per_allocation_settings_name_their_shared_edge() -> None:
    """The inverse of scoping: same origin and issuer, the shared ``/mcp``."""
    assert _PER_ALLOCATION.shared_edge() == mcp_oauth_settings()
    assert mcp_oauth_settings().for_new_allocation().shared_edge() == (
        mcp_oauth_settings()
    )


@pytest.mark.parametrize("resource", [MCP_RESOURCE_URL, f"{_FOREIGN_EDGE}{MCP_PATH}"])
def test_shared_settings_scope_no_shared_edge(resource: str) -> None:
    """Only exactly ``<origin>/a/<slug>/mcp`` is a per-allocation resource."""
    assert McpOAuthSettings(resource, MCP_ISSUER).shared_edge() is None


@pytest.mark.parametrize("per_allocation", [True, False])
def test_the_roll_target_of_each_record_shape(per_allocation: bool) -> None:
    """Legacy and shared move under the flag; per-allocation stays put."""
    shared = mcp_oauth_settings()

    from_legacy = shared.roll_target(None, per_allocation=per_allocation)
    from_shared = shared.roll_target(shared, per_allocation=per_allocation)

    assert shared.roll_target(_PER_ALLOCATION, per_allocation=per_allocation) == (
        _PER_ALLOCATION
    )
    assert shared.is_rolled(_PER_ALLOCATION, per_allocation=per_allocation)
    if per_allocation:
        assert from_legacy.shared_edge() == shared
        assert from_shared.shared_edge() == shared
        assert from_legacy != from_shared
        assert not shared.is_rolled(shared, per_allocation=True)
    else:
        assert from_legacy == shared
        assert from_shared == shared
        assert shared.is_rolled(shared, per_allocation=False)


@pytest.mark.parametrize(
    "other",
    [
        McpOAuthSettings(MCP_RESOURCE_URL, "https://issuer.other.example"),
        McpOAuthSettings(f"{_FOREIGN_EDGE}{MCP_PATH}", MCP_ISSUER),
        McpOAuthSettings(f"{_FOREIGN_EDGE}/a/{_SLUG}{MCP_PATH}", MCP_ISSUER),
    ],
)
def test_a_record_holding_other_settings_is_no_roll_shape(
    other: McpOAuthSettings,
) -> None:
    """Another issuer or edge is drift, never a transition this roll makes."""
    shared = mcp_oauth_settings()

    for per_allocation in (True, False):
        with pytest.raises(ValueError, match="other MCP OAuth settings"):
            shared.roll_target(other, per_allocation=per_allocation)
        with pytest.raises(ValueError, match="other MCP OAuth settings"):
            shared.is_rolled(other, per_allocation=per_allocation)


# --------------------------------------------------------------------------- #
# The driver: shared resource to per-allocation resource
# --------------------------------------------------------------------------- #


def test_a_stopped_shared_machine_rolls_to_its_own_resource() -> None:
    """Record and Machine move together; the Machine stays stopped and routes."""
    api = FakeFlyAPI()
    secrets, driver, allocation_id = _shared_allocation(api)
    assert _env(api)[RESOURCE_URL_ENV] == MCP_RESOURCE_URL

    assert driver.roll_mcp_oauth(_routable(allocation_id), secrets) is True

    stored = _stored(secrets)
    assert stored is not None
    assert stored.shared_edge() == mcp_oauth_settings()
    (update,) = api.updates
    assert update["skip_launch"] is True
    assert _machine(api)["state"] == "stopped"
    assert _env(api)[RESOURCE_URL_ENV] == stored.resource_url
    assert _env(api)[AUTHORIZATION_SERVER_ENV] == MCP_ISSUER
    driver.prepare_replay(_routable(allocation_id))


def test_a_rolled_allocation_is_already_current_even_when_running() -> None:
    """A second roll changes nothing and never asks for a stop."""
    api = FakeFlyAPI()
    secrets, driver, allocation_id = _shared_allocation(api)
    driver.roll_mcp_oauth(_routable(allocation_id), secrets)
    stored = _stored(secrets)
    driver.prepare_replay(_routable(allocation_id))

    assert driver.roll_mcp_oauth(_routable(allocation_id), secrets) is False
    assert len(api.updates) == 1
    assert _stored(secrets) == stored


def test_without_the_flag_a_shared_machine_is_already_current() -> None:
    """The second transition is opt-in: the flag off, the shared roll is done."""
    api = FakeFlyAPI()
    secrets, driver, allocation_id = _shared_allocation(api)
    secrets.per_allocation_mcp_resource = False
    driver.prepare_replay(_routable(allocation_id))

    assert driver.roll_mcp_oauth(_routable(allocation_id), secrets) is False
    assert api.updates == []
    assert _stored(secrets) == mcp_oauth_settings()


def test_a_running_shared_machine_is_refused_retryably_and_untouched() -> None:
    """Record and Machine stay shared, and the vault keeps routing."""
    api = FakeFlyAPI()
    secrets, driver, allocation_id = _shared_allocation(api)
    driver.prepare_replay(_routable(allocation_id))

    with pytest.raises(ProviderError) as raised:
        driver.roll_mcp_oauth(_routable(allocation_id), secrets)

    assert raised.value.retryable is True
    assert raised.value.reason is FailureReason.PROVIDER_UNAVAILABLE
    assert _stored(secrets) == mcp_oauth_settings()
    assert api.updates == []
    driver.prepare_replay(_routable(allocation_id))


def test_a_drifted_shared_machine_is_refused_and_untouched() -> None:
    """Only the exact shared Machine is admitted to the second transition."""
    api = FakeFlyAPI()
    secrets, driver, allocation_id = _shared_allocation(api)
    _env(api)["CREEK_EXTRA"] = "drift"

    with pytest.raises(ProviderError) as raised:
        driver.roll_mcp_oauth(_routable(allocation_id), secrets)

    assert raised.value.reason is FailureReason.PROVIDER_REJECTED
    assert raised.value.retryable is False
    assert _stored(secrets) == mcp_oauth_settings()
    assert api.updates == []


def test_a_machine_naming_another_edge_is_refused_as_drift() -> None:
    """A per-allocation record whose Machine names a foreign edge is not resumed."""
    api = FakeFlyAPI()
    secrets, driver, allocation_id = _shared_allocation(api)
    secrets.issued[_ACTIVATION] = dataclasses.replace(
        secrets.issued[_ACTIVATION], mcp_oauth=_PER_ALLOCATION
    )
    _env(api)[RESOURCE_URL_ENV] = f"{_FOREIGN_EDGE}{MCP_PATH}"

    with pytest.raises(ProviderError) as raised:
        driver.roll_mcp_oauth(_routable(allocation_id), secrets)

    assert raised.value.reason is FailureReason.PROVIDER_REJECTED
    assert _stored(secrets) == _PER_ALLOCATION
    assert api.updates == []


def test_a_record_holding_another_issuer_is_refused_untouched() -> None:
    """An exact Machine over an unrecognized record is refused, not rolled."""
    api = FakeFlyAPI()
    other = McpOAuthSettings(MCP_RESOURCE_URL, "https://issuer.other.example")
    secrets = FakeSecretManager(set(), mcp_oauth=other)
    driver = fly_driver(api, secrets, fly_replay_enabled=True)
    allocation_id = driver.provision(fly_job()).allocation_id
    secrets.mcp_oauth = mcp_oauth_settings()
    secrets.per_allocation_mcp_resource = True

    with pytest.raises(ValueError, match="other MCP OAuth settings"):
        driver.roll_mcp_oauth(_routable(allocation_id), secrets)

    assert _stored(secrets) == other
    assert api.updates == []


def test_an_interrupted_roll_resumes_with_the_slug_it_minted() -> None:
    """A failed Machine update leaves a retry that completes the same resource."""
    api = FakeFlyAPI()
    secrets, driver, allocation_id = _shared_allocation(api)
    api.fail_once("POST", f"/machines/{_machine(api)['id']}")

    with pytest.raises(ProviderError):
        driver.roll_mcp_oauth(_routable(allocation_id), secrets)
    minted = _stored(secrets)
    assert minted is not None
    assert minted.shared_edge() == mcp_oauth_settings()
    assert _env(api)[RESOURCE_URL_ENV] == MCP_RESOURCE_URL
    with pytest.raises(ProviderError):
        driver.prepare_replay(_routable(allocation_id))

    assert driver.roll_mcp_oauth(_routable(allocation_id), secrets) is True
    assert _stored(secrets) == minted
    assert _env(api)[RESOURCE_URL_ENV] == minted.resource_url
    driver.prepare_replay(_routable(allocation_id))


def test_a_legacy_machine_rolls_straight_to_its_own_resource() -> None:
    """With the flag, the #1974 roll skips the shared audience entirely."""
    api = FakeFlyAPI()
    secrets = FakeSecretManager(set())
    driver = fly_driver(api, secrets, fly_replay_enabled=True)
    allocation_id = driver.provision(fly_job()).allocation_id
    secrets.mcp_oauth = mcp_oauth_settings()
    secrets.per_allocation_mcp_resource = True

    assert driver.roll_mcp_oauth(_routable(allocation_id), secrets) is True

    stored = _stored(secrets)
    assert stored is not None
    assert is_allocation_mcp_path(stored.resource_url.removeprefix(ROUTING_PUBLIC_URL))
    assert _env(api)[RESOURCE_URL_ENV] == stored.resource_url


# --------------------------------------------------------------------------- #
# The encrypted record: the record half, and the fleet check
# --------------------------------------------------------------------------- #


def _manager(
    tmp_path: Path,
    *,
    per_allocation: bool,
    mcp_oauth: McpOAuthSettings | None = None,
) -> EncryptedFileFlySecretManager:
    """Return an encrypted manager over one shared state root."""
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
        mcp_oauth=mcp_oauth_settings() if mcp_oauth is None else mcp_oauth,
        per_allocation_mcp_resource=per_allocation,
    )


def _reader(tmp_path: Path) -> EncryptedFileRoutingCredentialVerifier:
    """Return the edge's read-only view of the same records."""
    return EncryptedFileRoutingCredentialVerifier(
        tmp_path / "runtime-secrets", master_key_file=tmp_path / "master-key"
    )


def _issue(manager: EncryptedFileFlySecretManager, activation: str) -> Any:
    """Issue *activation*'s record and return its settings."""
    return manager.issue(activation, _CONSUMER, requester_identity=_REQUESTER).mcp_oauth


def test_the_record_mints_once_and_keeps_its_slug(tmp_path: Path) -> None:
    """Shared becomes per-allocation once; a repeat returns the same resource."""
    assert _issue(_manager(tmp_path, per_allocation=False), _ACTIVATION) == (
        mcp_oauth_settings()
    )
    manager = _manager(tmp_path, per_allocation=True)

    first = manager.roll_mcp_oauth(_ACTIVATION)
    again = manager.roll_mcp_oauth(_ACTIVATION)

    assert first.shared_edge() == mcp_oauth_settings()
    assert again == first
    assert _issue(manager, _ACTIVATION) == first
    assert manager.mcp_oauth_is_current(first) is True
    assert manager.mcp_oauth_is_current(mcp_oauth_settings()) is False


def test_without_the_flag_the_record_keeps_the_shared_resource(tmp_path: Path) -> None:
    """The flag off, a shared record is current and its roll writes nothing new."""
    _issue(_manager(tmp_path, per_allocation=False), _ACTIVATION)
    manager = _manager(tmp_path, per_allocation=False)

    assert manager.roll_mcp_oauth(_ACTIVATION) == mcp_oauth_settings()
    assert manager.mcp_oauth_is_current(mcp_oauth_settings()) is True


def test_with_nothing_configured_every_record_is_current(tmp_path: Path) -> None:
    """No configured issuer leaves nothing to roll toward."""
    state = tmp_path / "runtime-secrets"
    state.mkdir(mode=0o700)
    _owner_file(tmp_path / "master-key", b"m" * 32)
    _ca_files(tmp_path)
    manager = EncryptedFileFlySecretManager(
        state,
        master_key_file=tmp_path / "master-key",
        ca_certificate_file=tmp_path / "ca.crt",
        ca_private_key_file=tmp_path / "ca.key",
        app_prefix="creek-vault",
        clock=lambda: _NOW,
    )

    assert manager.mcp_oauth_is_current(mcp_oauth_settings()) is True
    with pytest.raises(ValueError, match="no MCP OAuth settings"):
        manager.shared_audience_activations()


def test_the_fleet_check_lists_every_allocation_not_on_its_own_resource(
    tmp_path: Path,
) -> None:
    """Shared and legacy records are listed; per-allocation and revoked are not."""
    legacy = EncryptedFileFlySecretManager(
        _manager(tmp_path, per_allocation=False)._state_directory,
        master_key_file=tmp_path / "master-key",
        ca_certificate_file=tmp_path / "ca.crt",
        ca_private_key_file=tmp_path / "ca.key",
        app_prefix="creek-vault",
        clock=lambda: _NOW,
    )
    _issue(legacy, "activation-legacy")
    _issue(_manager(tmp_path, per_allocation=False), "activation-shared")
    _issue(_manager(tmp_path, per_allocation=False), "activation-revoked")
    manager = _manager(tmp_path, per_allocation=True)
    _issue(manager, "activation-scoped")
    manager.revoke("activation-revoked")

    pending = manager.shared_audience_activations()

    assert pending == ("activation-legacy", "activation-shared")
    assert _reader(tmp_path).shared_audience_activations(mcp_oauth_settings()) == (
        pending
    )


def test_the_fleet_check_counts_a_foreign_edge_as_not_rolled(tmp_path: Path) -> None:
    """Only this edge's per-allocation resource clears a record."""
    foreign = McpOAuthSettings(f"{_FOREIGN_EDGE}{MCP_PATH}", MCP_ISSUER)
    _issue(_manager(tmp_path, per_allocation=True, mcp_oauth=foreign), _ACTIVATION)

    assert _manager(tmp_path, per_allocation=True).shared_audience_activations() == (
        _ACTIVATION,
    )


def test_the_fleet_check_fails_closed_on_an_unreadable_record(tmp_path: Path) -> None:
    """A record it cannot read cannot be proven rolled."""
    manager = _manager(tmp_path, per_allocation=True)
    _issue(manager, _ACTIVATION)
    (bundle,) = (tmp_path / "runtime-secrets").glob("*.bundle")
    bundle.write_bytes(b"x" * 64)

    with pytest.raises(ValueError, match="invalid"):
        manager.shared_audience_activations()
    with pytest.raises(ValueError, match="invalid"):
        _reader(tmp_path).shared_audience_activations(mcp_oauth_settings())


def test_the_fleet_check_refuses_a_stray_bundle_name(tmp_path: Path) -> None:
    """A file that is not a derived record name is not silently skipped."""
    manager = _manager(tmp_path, per_allocation=True)
    _owner_file(tmp_path / "runtime-secrets" / "stray.bundle", b"x" * 64)

    with pytest.raises(ValueError, match="invalid"):
        manager.shared_audience_activations()


def test_a_full_roll_empties_the_fleet_check(tmp_path: Path) -> None:
    """Every shared allocation rolled leaves no shared-audience allocation."""
    api = FakeFlyAPI()
    driver = fly_driver(
        api, _manager(tmp_path, per_allocation=False), fly_replay_enabled=True
    )
    allocations = [
        _routable(driver.provision(fly_job(activation)).allocation_id, activation)
        for activation in (_ACTIVATION, _OTHER_ACTIVATION)
    ]
    manager = _manager(tmp_path, per_allocation=True)
    assert manager.shared_audience_activations() == (_ACTIVATION, _OTHER_ACTIVATION)
    rolling = fly_driver(api, manager, fly_replay_enabled=True)

    for allocation in allocations:
        assert rolling.roll_mcp_oauth(allocation, manager) is True

    assert manager.shared_audience_activations() == ()
    assert _reader(tmp_path).shared_audience_activations(mcp_oauth_settings()) == ()
    resources = {_env(api, index)[RESOURCE_URL_ENV] for index in (0, 1)}
    assert len(resources) == len(allocations)
    assert MCP_RESOURCE_URL not in resources


# --------------------------------------------------------------------------- #
# The rollout's per-allocation report
# --------------------------------------------------------------------------- #


def test_each_allocation_reports_rolled_current_or_refused() -> None:
    """The three outcomes, with the refusal's reason and its retryability."""
    api = FakeFlyAPI()
    secrets, driver, allocation_id = _shared_allocation(api)
    allocation = _routable(allocation_id)

    driver.start(_ACTIVATION)
    running = mcp_oauth_rollout.roll_allocation(driver, secrets, allocation)
    driver.stop(_ACTIVATION)
    rolled = mcp_oauth_rollout.roll_allocation(driver, secrets, allocation)
    current = mcp_oauth_rollout.roll_allocation(driver, secrets, allocation)
    secrets.mcp_oauth = McpOAuthSettings(
        MCP_RESOURCE_URL, "https://issuer.other.example"
    )
    other = mcp_oauth_rollout.roll_allocation(driver, secrets, allocation)
    secrets.mcp_oauth = mcp_oauth_settings()
    _env(api)["CREEK_EXTRA"] = "drift"
    drifted = mcp_oauth_rollout.roll_allocation(driver, secrets, allocation)

    assert str(running) == "refused(provider_unavailable, retryable)"
    assert running.exit_status == mcp_oauth_rollout.EXIT_RETRY
    assert str(rolled) == "rolled"
    assert rolled.exit_status == mcp_oauth_rollout.EXIT_ROLLED
    assert str(current) == "already-current"
    assert current.exit_status == mcp_oauth_rollout.EXIT_ROLLED
    assert str(drifted) == "refused(provider_rejected)"
    assert drifted.exit_status == mcp_oauth_rollout.EXIT_REFUSED
    assert str(other) == "refused(record)"
    assert other.exit_status == mcp_oauth_rollout.EXIT_REFUSED


class _Store:
    """A store double answering one ownership lookup."""

    def __init__(self, allocation: RoutableAllocation | None) -> None:
        self.allocation = allocation

    def get_routable_allocation(
        self, requester_identity: str, consumer_identity: str
    ) -> RoutableAllocation | None:
        del requester_identity, consumer_identity
        return self.allocation


def test_the_one_shot_roll_logs_its_report_content_free(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The log names the outcome and nothing about the vault."""
    caplog.set_level(logging.INFO)
    api = FakeFlyAPI()
    secrets, driver, allocation_id = _shared_allocation(api)
    store = _Store(_routable(allocation_id))

    for _ in range(2):
        status = mcp_oauth_rollout.roll_owned_allocation(
            store,
            driver,
            secrets,
            requester_identity=_REQUESTER,
            consumer_identity=_CONSUMER,
        )
        assert status == mcp_oauth_rollout.EXIT_ROLLED

    stored = _stored(secrets)
    assert stored is not None
    assert "mcp oauth roll: rolled" in caplog.text
    assert "mcp oauth roll: already-current" in caplog.text
    for secret in (_CONSUMER, _ACTIVATION, MCP_ISSUER, ROUTING_PUBLIC_URL, _SLUG):
        assert secret not in caplog.text
    assert stored.resource_url not in caplog.text


# --------------------------------------------------------------------------- #
# The fleet check as an operator one-shot
# --------------------------------------------------------------------------- #


class _Pending:
    """A fleet-check double returning a fixed answer, or raising."""

    def __init__(self, pending: tuple[str, ...] | None) -> None:
        self.pending = pending

    def shared_audience_activations(self) -> tuple[str, ...]:
        if self.pending is None:
            raise ValueError("runtime secret state is invalid")
        return self.pending


def test_the_fleet_check_maps_its_answer_to_an_exit_status(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Empty exits 0; any remaining exits distinctly; unreadable is refused."""
    caplog.set_level(logging.INFO)

    assert mcp_oauth_rollout.check_shared_audience(_Pending(())) == (
        mcp_oauth_rollout.EXIT_ROLLED
    )
    assert mcp_oauth_rollout.check_shared_audience(
        _Pending(("activation-secret-a", "activation-secret-b"))
    ) == (mcp_oauth_rollout.EXIT_SHARED_AUDIENCE_REMAINS)
    assert mcp_oauth_rollout.check_shared_audience(_Pending(None)) == (
        mcp_oauth_rollout.EXIT_REFUSED
    )
    assert "2 allocation(s) remain on the shared audience" in caplog.text
    assert "activation-secret" not in caplog.text


class _ClosingBundle:
    """A composed bundle double that records its close and refuses to serve."""

    def __init__(self) -> None:
        self.driver: object = None
        self.secrets = _Pending(("activation-a",))
        self.closed = False

    @property
    def worker(self) -> _ClosingBundle:
        return self

    @property
    def supervisor(self) -> Any:
        raise AssertionError("a one-shot check must not start the supervisor")

    def close(self) -> None:
        self.closed = True


def _one_shot_parser() -> argparse.ArgumentParser:
    """Return a parser carrying only the one-shot selectors."""
    parser = argparse.ArgumentParser()
    worker_cli.add_mcp_oauth_roll_argument(parser)
    return parser


@pytest.mark.parametrize("module", ["worker", "pilot"])
def test_main_runs_only_the_fleet_check_and_exits_with_its_status(
    module: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--check-mcp-audience`` checks the records, closes, and claims nothing."""
    bundle = _ClosingBundle()
    target = worker_cli if module == "worker" else pilot_cli
    monkeypatch.setattr(target, "build_parser", _one_shot_parser)
    monkeypatch.setattr(target, "compose", lambda *_: bundle)

    with pytest.raises(SystemExit) as raised:
        target.main(["--check-mcp-audience"])

    assert raised.value.code == mcp_oauth_rollout.EXIT_SHARED_AUDIENCE_REMAINS
    assert bundle.closed is True


def test_the_check_and_the_roll_are_one_shot_alternatives() -> None:
    """One process runs one operation."""
    with pytest.raises(SystemExit):
        _one_shot_parser().parse_args(
            ["--check-mcp-audience", "--roll-mcp-oauth", _REQUESTER, _CONSUMER]
        )
    assert _one_shot_parser().parse_args([]).check_mcp_audience is False


# --------------------------------------------------------------------------- #
# The edge flag cannot be flipped while any allocation remains
# --------------------------------------------------------------------------- #


def _routing_args(tmp_path: Path, *extra: str) -> argparse.Namespace:
    """Parse the edge's OAuth arguments over *tmp_path*'s records."""
    parser = routing_cli.build_parser()
    namespace = parser.parse_args(
        [
            "--database",
            "db",
            "--fly-token-file",
            "t",
            "--fly-organization",
            "o",
            "--fly-image",
            "i",
            "--fly-token-expires-at",
            "2026-09-28T12:00:00+00:00",
            "--secret-state-directory",
            str(tmp_path / "runtime-secrets"),
            "--secret-master-key-file",
            str(tmp_path / "master-key"),
            "--private-tls-ca-certificate-file",
            "c",
            "--oauth-authorization-server",
            MCP_ISSUER,
            "--per-allocation-mcp-resource",
            *extra,
        ]
    )
    namespace.oauth_resource_url = MCP_RESOURCE_URL
    return namespace


def test_the_edge_refuses_the_flip_while_any_allocation_remains(
    tmp_path: Path,
) -> None:
    """Refusing the shared audience with a shared allocation left fails startup."""
    _issue(_manager(tmp_path, per_allocation=False), _ACTIVATION)
    _issue(_manager(tmp_path, per_allocation=True), _OTHER_ACTIVATION)
    args = _routing_args(tmp_path, "--refuse-shared-mcp-audience")

    with pytest.raises(ValueError, match="1 allocation") as raised:
        routing_cli.agent_routing(args, _reader(tmp_path))

    assert _ACTIVATION not in str(raised.value)
    assert MCP_RESOURCE_URL not in str(raised.value)
    shared = routing_cli.agent_routing(_routing_args(tmp_path), _reader(tmp_path))
    assert shared is not None
    assert shared.shared_audience is True


def test_the_edge_flips_once_every_allocation_is_rolled(tmp_path: Path) -> None:
    """After a full roll the same startup succeeds and refuses ``/mcp``."""
    _issue(_manager(tmp_path, per_allocation=False), _ACTIVATION)
    manager = _manager(tmp_path, per_allocation=True)
    manager.roll_mcp_oauth(_ACTIVATION)

    flipped = routing_cli.agent_routing(
        _routing_args(tmp_path, "--refuse-shared-mcp-audience"), _reader(tmp_path)
    )

    assert flipped is not None
    assert flipped.shared_audience is False


def test_the_edge_refuses_the_flip_when_the_records_are_unreadable(
    tmp_path: Path,
) -> None:
    """An unprovable fleet check is a refusal, not a pass."""
    manager = _manager(tmp_path, per_allocation=True)
    _issue(manager, _ACTIVATION)
    (bundle,) = (tmp_path / "runtime-secrets").glob("*.bundle")
    bundle.write_bytes(b"x" * 64)

    with pytest.raises(ValueError, match="invalid"):
        routing_cli.agent_routing(
            _routing_args(tmp_path, "--refuse-shared-mcp-audience"), _reader(tmp_path)
        )
