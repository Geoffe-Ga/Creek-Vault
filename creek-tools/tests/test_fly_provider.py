"""Fly Machines lifecycle contract for issue #1770."""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from creek_mcp.provisioning.driver import FakeOneTimeHandoff, ProviderError
from creek_mcp.provisioning.fly import (
    FlyCredential,
    FlyCredentialScope,
    FlyProviderDriver,
    FlyProviderPolicy,
)
from creek_mcp.provisioning.inventory import ProviderResource
from creek_mcp.provisioning.models import (
    DeletionOutcome,
    FailureReason,
    ResourceClass,
    ResourceState,
    RoutableAllocation,
)
from creek_mcp.provisioning.store import ProvisioningStore
from creek_mcp.provisioning.worker import ProvisioningWorker
from tests.fly_support import (
    API_BASE_URL,
    CONSUMER_TOKEN,
    IMAGE,
    ORGANIZATION,
    PROVIDER_TOKEN,
    ROUTING_PUBLIC_URL,
    TLS_KEY,
    FakeFlyAPI,
    FakeSecretManager,
    ProviderNormalizedFlyAPI,
    fly_client,
    fly_driver,
    fly_job,
)

if TYPE_CHECKING:
    from collections.abc import Callable

_NOW = datetime(2026, 9, 7, 4, tzinfo=UTC)
_RUNBOOK = (
    Path(__file__).resolve().parents[1] / "docs" / "provisioning-control-plane.md"
)
_FLY_VOLUME_NAME = re.compile(r"[a-z0-9_]{1,30}")


def _only_app(api: FakeFlyAPI) -> str:
    assert len(api.apps) == 1
    return next(iter(api.apps))


def _only_machine(api: FakeFlyAPI) -> dict[str, Any]:
    machines = api.machines[_only_app(api)]
    assert len(machines) == 1
    return machines[0]


def _only_volume(api: FakeFlyAPI) -> dict[str, Any]:
    volumes = api.volumes[_only_app(api)]
    assert len(volumes) == 1
    return volumes[0]


def test_reference_policy_creates_one_private_scale_to_zero_allocation() -> None:
    """Reference defaults remain config while the request stays private."""
    api = FakeFlyAPI()
    driver = fly_driver(api)

    allocation = driver.provision(fly_job())

    app_name = _only_app(api)
    volume = api.volumes[app_name][0]
    machine = _only_machine(api)
    config = machine["config"]
    assert allocation.allocation_id == config["metadata"]["creek_allocation_id"]
    assert config["metadata"]["creek_activation_id"] == "activation-fly-001"
    assert api.apps[app_name]["network"] == allocation.allocation_id
    assert volume == {
        "id": "vol-1",
        "name": "vault_" + allocation.allocation_id.removeprefix("fly-"),
        "region": "iad",
        "size_gb": 5,
        "encrypted": True,
        "state": "created",
    }
    assert machine["region"] == "iad"
    assert machine["state"] == "stopped"
    assert config["guest"] == {"cpu_kind": "shared", "cpus": 1, "memory_mb": 1024}
    assert config["rootfs"] == {"size_gb": 1, "persist": "never"}
    assert config["mounts"] == [
        {"volume": "vol-1", "path": "/vault", "encrypted": True}
    ]
    assert config["restart"] == {"policy": "no"}
    assert {item["guest_path"] for item in config["files"]} == {
        "/run/secrets/creek_consumer_tokens",
        "/run/secrets/tls.crt",
        "/run/secrets/tls.key",
    }
    assert config["services"] == [
        {
            "protocol": "tcp",
            "internal_port": 8823,
            "ports": [],
            "autostart": True,
            "autostop": "stop",
            "min_machines_running": 0,
        }
    ]
    assert all("ips" not in path for _, path in api.requests)
    assert allocation.vault_url == f"{ROUTING_PUBLIC_URL}/v1"
    assert ".internal" not in allocation.vault_url


def test_provision_accepts_exact_live_provider_normalization() -> None:
    """Fly's reviewed response enrichments are equivalent to the request policy."""
    api = ProviderNormalizedFlyAPI()
    driver = fly_driver(api, fly_replay_enabled=True)

    allocation = driver.provision(fly_job())
    target = driver.prepare_replay(
        RoutableAllocation(
            "job-fly-001",
            "activation-fly-001",
            "adepthood",
            "adepthood-user-001",
            allocation.allocation_id,
        )
    )

    machine = _only_machine(api)
    mount = machine["config"]["mounts"][0]
    service = machine["config"]["services"][0]
    digest = allocation.allocation_id.removeprefix("fly-")
    assert target.machine_id == "machine-1"
    assert mount == {
        "volume": "vol-1",
        "path": "/vault",
        "encrypted": True,
        "name": f"vault_{digest}",
        "size_gb": 5,
    }
    assert service == {
        "protocol": "tcp",
        "internal_port": 8823,
        "ports": [{"port": 443, "handlers": ["tls", "http"]}],
        "autostart": True,
        "autostop": True,
        "min_machines_running": 0,
        "force_instance_key": None,
    }


@pytest.mark.parametrize(
    ("_label", "mutate"),
    [
        (
            "mount-name",
            lambda machine: machine["config"]["mounts"][0].update(name="other"),
        ),
        (
            "mount-size",
            lambda machine: machine["config"]["mounts"][0].update(size_gb=6),
        ),
        (
            "autostop",
            lambda machine: machine["config"]["services"][0].update(autostop=False),
        ),
        (
            "instance-key",
            lambda machine: machine["config"]["services"][0].update(
                force_instance_key="unreviewed"
            ),
        ),
        (
            "service-field",
            lambda machine: machine["config"]["services"][0].update(unreviewed=True),
        ),
        (
            "config-field",
            lambda machine: machine["config"].update(unreviewed=True),
        ),
    ],
)
def test_provision_rejects_drift_inside_provider_normalization(
    _label: str,
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    """Known enrichment never permits policy drift or unknown provider fields."""
    api = ProviderNormalizedFlyAPI()
    driver = fly_driver(api, fly_replay_enabled=True)
    job = fly_job()
    driver.provision(job)
    mutate(_only_machine(api))

    with pytest.raises(ProviderError) as raised:
        driver.provision(job)

    assert raised.value.reason is FailureReason.PROVIDER_REJECTED


def test_volume_names_meet_fly_runtime_contract_and_remain_deterministic() -> None:
    """Every activation maps stably to one distinct Fly-valid volume name."""
    api = FakeFlyAPI()
    driver = fly_driver(api)

    first = driver.provision(fly_job("activation-volume-name-first"))
    repeated = driver.provision(fly_job("activation-volume-name-first"))
    second = driver.provision(fly_job("activation-volume-name-second"))

    names = {volume["name"] for volumes in api.volumes.values() for volume in volumes}
    assert first == repeated
    assert first.allocation_id != second.allocation_id
    assert len(names) == 2
    assert all(isinstance(name, str) for name in names)
    assert all(_FLY_VOLUME_NAME.fullmatch(name) is not None for name in names)
    assert all(len(name) == 30 for name in names)


def test_cross_network_route_uses_fly_replay_not_private_dns() -> None:
    """An isolated vault is addressed only by a checked Fly replay target."""
    api = FakeFlyAPI()
    driver = fly_driver(api, fly_replay_enabled=True)
    job = fly_job()
    allocation = driver.provision(job)
    routable = RoutableAllocation(
        job.job_id,
        job.activation_id,
        job.requester_identity,
        job.consumer_identity,
        allocation.allocation_id,
    )

    target = driver.prepare_replay(routable)

    app_name = _only_app(api)
    machine = _only_machine(api)
    assert target.app_name == app_name
    assert target.machine_id == machine["id"]
    assert ".internal" not in repr(target)
    config = machine["config"]
    assert config["user"] == "root"
    assert config["init"] == {
        "exec": [
            "python",
            "-m",
            "creek_mcp.provisioning.fly_vault_bootstrap",
        ]
    }
    assert {item["guest_path"] for item in config["files"]} == {
        "/run/secrets/creek_consumer_tokens",
        "/run/secrets/creek_replay_state",
    }
    assert config["services"][0]["ports"] == [
        {"port": 443, "handlers": ["tls", "http"]}
    ]


@pytest.mark.parametrize("network", [None, "default", "other-allocation"])
def test_replay_refuses_missing_or_drifted_allocation_network(
    network: str | None,
) -> None:
    """Cross-network replay never weakens per-vault network isolation."""
    api = FakeFlyAPI()
    driver = fly_driver(api, fly_replay_enabled=True)
    job = fly_job()
    allocation = driver.provision(job)
    api.apps[_only_app(api)]["network"] = network
    routed = RoutableAllocation(
        job.job_id,
        job.activation_id,
        job.requester_identity,
        job.consumer_identity,
        allocation.allocation_id,
    )

    with pytest.raises(ProviderError) as raised:
        driver.prepare_replay(routed)

    assert raised.value.reason is FailureReason.PROVIDER_REJECTED
    assert not any(path.endswith("/start") for _, path in api.requests)


def test_replay_rechecks_allocation_network_after_machine_readiness() -> None:
    """A network swap during cold start cannot emit stale replay coordinates."""

    class NetworkSwapAPI(FakeFlyAPI):
        def handle(self, request):
            response = super().handle(request)
            if request.url.path.endswith("/wait"):
                self.apps[_only_app(self)]["network"] = "other-allocation"
            return response

    api = NetworkSwapAPI()
    driver = fly_driver(api, fly_replay_enabled=True)
    job = fly_job()
    allocation = driver.provision(job)
    routed = RoutableAllocation(
        job.job_id,
        job.activation_id,
        job.requester_identity,
        job.consumer_identity,
        allocation.allocation_id,
    )

    with pytest.raises(ProviderError) as raised:
        driver.prepare_replay(routed)

    assert raised.value.reason is FailureReason.PROVIDER_REJECTED


@pytest.mark.parametrize(
    ("_label", "mutate"),
    [
        ("name", lambda machine: machine.update(name="wrong-machine")),
        ("region", lambda machine: machine.update(region="ord")),
        (
            "image",
            lambda machine: machine["config"].update(
                image="registry.example/other@sha256:" + "b" * 64
            ),
        ),
        (
            "rootfs",
            lambda machine: machine["config"].update(
                rootfs={"size_gb": 2, "persist": "never"}
            ),
        ),
        (
            "guest",
            lambda machine: machine["config"].update(
                guest={"cpu_kind": "shared", "cpus": 2, "memory_mb": 1024}
            ),
        ),
        ("mount", lambda machine: machine["config"].update(mounts=[])),
        (
            "restart",
            lambda machine: machine["config"].update(restart={"policy": "always"}),
        ),
        ("environment", lambda machine: machine["config"].update(env={})),
        ("init", lambda machine: machine["config"].update(init={})),
        ("service", lambda machine: machine["config"].update(services=[])),
        (
            "replay-secret",
            lambda machine: machine["config"].update(
                files=machine["config"]["files"][:1]
            ),
        ),
        (
            "unexpected-secret",
            lambda machine: machine["config"]["files"].append(
                {"guest_path": "/run/secrets/tls.key", "raw_value": "YQ=="}
            ),
        ),
    ],
)
def test_replay_refuses_every_machine_policy_drift_before_emitting_target(
    _label: str,
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    """Replay is issued only for the exact reviewed immutable Machine shape."""
    api = FakeFlyAPI()
    driver = fly_driver(api, fly_replay_enabled=True)
    job = fly_job()
    allocation = driver.provision(job)
    mutate(_only_machine(api))
    routed = RoutableAllocation(
        job.job_id,
        job.activation_id,
        job.requester_identity,
        job.consumer_identity,
        allocation.allocation_id,
    )

    with pytest.raises(ProviderError) as raised:
        driver.prepare_replay(routed)

    assert raised.value.reason is FailureReason.PROVIDER_REJECTED
    assert not any(path.endswith("/start") for _, path in api.requests)


@pytest.mark.parametrize("secret_index", [0, 1])
def test_replay_refuses_same_path_stale_secret_material_before_start(
    secret_index: int,
) -> None:
    """A retry cannot adopt stale credentials hidden behind expected paths."""
    api = FakeFlyAPI()
    driver = fly_driver(api, fly_replay_enabled=True)
    job = fly_job()
    allocation = driver.provision(job)
    _only_machine(api)["config"]["files"][secret_index]["raw_value"] = "c3RhbGU="
    routed = RoutableAllocation(
        job.job_id,
        job.activation_id,
        job.requester_identity,
        job.consumer_identity,
        allocation.allocation_id,
    )

    with pytest.raises(ProviderError) as raised:
        driver.prepare_replay(routed)

    assert raised.value.reason is FailureReason.PROVIDER_REJECTED
    assert not any(path.endswith("/start") for _, path in api.requests)


def test_provision_retry_refuses_same_path_wrong_secret_material() -> None:
    """A retry never adopts a Machine whose mounted values predate the bundle."""
    api = FakeFlyAPI()
    driver = fly_driver(api, fly_replay_enabled=True)
    job = fly_job()
    driver.provision(job)
    _only_machine(api)["config"]["files"][0]["raw_value"] = "c3RhbGU="
    request_count = len(api.requests)

    with pytest.raises(ProviderError) as raised:
        driver.provision(job)

    assert raised.value.reason is FailureReason.PROVIDER_REJECTED
    assert len(_only_machine(api)["config"]["files"]) == 2
    assert not any(
        method == "POST" and path.endswith("/machines")
        for method, path in api.requests[request_count:]
    )


def test_routing_start_wait_and_machine_rediscovery_are_store_bound() -> None:
    """The driver wakes its canonical allocation and targets its current Machine."""
    api = FakeFlyAPI()
    driver = fly_driver(api)
    job = fly_job()
    allocation = driver.provision(job)
    routed = RoutableAllocation(
        job.job_id,
        job.activation_id,
        job.requester_identity,
        job.consumer_identity,
        allocation.allocation_id,
    )

    first = driver.prepare_route(routed)
    second = driver.prepare_route(routed)

    app_name = _only_app(api)
    machine_id = _only_machine(api)["id"]
    assert first == second
    assert first.base_url == f"https://{machine_id}.vm.{app_name}.internal:8823"
    starts = [request for request in api.requests if request[1].endswith("/start")]
    waits = [request for request in api.requests if request[1].endswith("/wait")]
    assert len(starts) == 1
    assert len(waits) == 2


def test_routing_rejects_a_stale_provider_allocation_before_start() -> None:
    """A corrupted ownership row cannot redirect a canonical activation."""
    api = FakeFlyAPI()
    driver = fly_driver(api)
    job = fly_job()
    driver.provision(job)
    routed = RoutableAllocation(
        job.job_id,
        job.activation_id,
        job.requester_identity,
        job.consumer_identity,
        "fly-stale-provider-id",
    )

    with pytest.raises(ProviderError) as raised:
        driver.prepare_route(routed)

    assert raised.value.reason is FailureReason.PROVIDER_REJECTED
    assert not any(path.endswith("/start") for _, path in api.requests)


def test_routing_rejects_drifted_machine_policy_before_start() -> None:
    """A canonical name cannot bypass exact route-time Machine ownership checks."""
    api = FakeFlyAPI()
    driver = fly_driver(api)
    job = fly_job()
    allocation = driver.provision(job)
    _only_machine(api)["config"]["metadata"] = {
        "creek_allocation_id": "fly-other-allocation",
        "creek_activation_id": "activation-other",
    }
    routed = RoutableAllocation(
        job.job_id,
        job.activation_id,
        job.requester_identity,
        job.consumer_identity,
        allocation.allocation_id,
    )

    with pytest.raises(ProviderError) as raised:
        driver.prepare_route(routed)

    assert raised.value.reason is FailureReason.PROVIDER_REJECTED
    assert not any(path.endswith("/start") for _, path in api.requests)


def test_routing_rejects_drifted_volume_policy_before_start() -> None:
    """Route-time custody rejects an allocation whose volume is no longer encrypted."""
    api = FakeFlyAPI()
    driver = fly_driver(api)
    job = fly_job()
    allocation = driver.provision(job)
    _only_volume(api)["encrypted"] = False
    routed = RoutableAllocation(
        job.job_id,
        job.activation_id,
        job.requester_identity,
        job.consumer_identity,
        allocation.allocation_id,
    )

    with pytest.raises(ProviderError) as raised:
        driver.prepare_route(routed)

    assert raised.value.reason is FailureReason.PROVIDER_REJECTED
    assert not any(path.endswith("/start") for _, path in api.requests)


def test_routing_rediscovers_a_replacement_machine_after_readiness_wait() -> None:
    """The final private target follows a provider replacement, not stale state."""
    api = FakeFlyAPI()
    driver = fly_driver(api)
    job = fly_job()
    allocation = driver.provision(job)
    api.replace_machine_on_wait = True
    routed = RoutableAllocation(
        job.job_id,
        job.activation_id,
        job.requester_identity,
        job.consumer_identity,
        allocation.allocation_id,
    )

    target = driver.prepare_route(routed)

    assert target.base_url.startswith("https://machine-replacement.vm.")


def test_routing_rejects_replacement_without_the_encrypted_mount() -> None:
    """Post-wait rediscovery revalidates replacement custody before private dial."""
    api = FakeFlyAPI()
    driver = fly_driver(api)
    job = fly_job()
    allocation = driver.provision(job)
    api.replace_machine_on_wait = True
    api.replacement_drops_mount = True
    routed = RoutableAllocation(
        job.job_id,
        job.activation_id,
        job.requester_identity,
        job.consumer_identity,
        allocation.allocation_id,
    )

    with pytest.raises(ProviderError) as raised:
        driver.prepare_route(routed)

    assert raised.value.reason is FailureReason.PROVIDER_REJECTED


def test_routing_revalidates_volume_custody_after_readiness_wait() -> None:
    """A volume policy race after start is refused before returning a target."""
    api = FakeFlyAPI()
    driver = fly_driver(api)
    job = fly_job()
    allocation = driver.provision(job)
    api.volume_loses_encryption_on_wait = True
    routed = RoutableAllocation(
        job.job_id,
        job.activation_id,
        job.requester_identity,
        job.consumer_identity,
        allocation.allocation_id,
    )

    with pytest.raises(ProviderError) as raised:
        driver.prepare_route(routed)

    assert raised.value.reason is FailureReason.PROVIDER_REJECTED


def test_routing_readiness_timeout_is_retryable_and_target_free() -> None:
    """A Machine that never starts becomes one bounded secret-free failure."""
    api = FakeFlyAPI()
    driver = fly_driver(api)
    job = fly_job()
    allocation = driver.provision(job)
    api.start_stays_stopped = True
    routed = RoutableAllocation(
        job.job_id,
        job.activation_id,
        job.requester_identity,
        job.consumer_identity,
        allocation.allocation_id,
    )

    with pytest.raises(ProviderError) as raised:
        driver.prepare_route(routed)

    assert raised.value.reason is FailureReason.PROVIDER_UNAVAILABLE
    assert raised.value.retryable is True
    assert "machine-1" not in str(raised.value)


def test_routing_start_failure_is_retryable_and_target_free() -> None:
    """A provider start failure exposes neither the Machine nor its topology."""
    api = FakeFlyAPI()
    driver = fly_driver(api)
    job = fly_job()
    allocation = driver.provision(job)
    api.fail_once("POST", "/start")
    routed = RoutableAllocation(
        job.job_id,
        job.activation_id,
        job.requester_identity,
        job.consumer_identity,
        allocation.allocation_id,
    )

    with pytest.raises(ProviderError) as raised:
        driver.prepare_route(routed)

    assert raised.value.reason is FailureReason.PROVIDER_UNAVAILABLE
    assert raised.value.retryable is True
    assert "machine-1" not in str(raised.value)


def test_fly_runbook_pins_scope_reconciliation_and_background_ownership() -> None:
    """Operators receive the security and lifecycle rules the adapter enforces."""
    text = " ".join(_RUNBOOK.read_text(encoding="utf-8").lower().split())

    for phrase in (
        "org-scoped deploy token",
        "never a personal access token",
        "partial create",
        "partial delete",
        "no dedicated ipv4",
        "drain, commit, close, and stop",
        "ordinary fly machine does not advertise intimate",
    ):
        assert phrase in text


def test_provider_and_runtime_secrets_are_repr_safe_and_never_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Provider bodies and every credential stay out of exceptions and logs."""
    api = FakeFlyAPI()
    api.failure_body = f"echo {PROVIDER_TOKEN} {CONSUMER_TOKEN} {TLS_KEY}"
    api.fail_once("POST", "/machines")
    driver = fly_driver(api)

    with caplog.at_level(logging.DEBUG), pytest.raises(ProviderError) as raised:
        driver.provision(fly_job())

    rendered = caplog.text + repr(driver) + str(raised.value)
    assert PROVIDER_TOKEN not in rendered
    assert CONSUMER_TOKEN not in rendered
    assert TLS_KEY not in rendered


@pytest.mark.parametrize(
    ("status", "retryable"),
    [(401, True), (403, False)],
)
def test_only_provider_authentication_expiry_is_recoverable(
    status: int,
    retryable: bool,
) -> None:
    """A replaced token can repair 401; an authorization refusal stays terminal."""
    api = FakeFlyAPI()
    api.fail_once("POST", "/machines", status=status)

    with pytest.raises(ProviderError) as raised:
        fly_driver(api).provision(fly_job("activation-provider-auth"))

    assert raised.value.reason is FailureReason.PROVIDER_REJECTED
    assert raised.value.retryable is retryable


def test_credential_file_requires_an_org_deploy_scope_and_hides_token(
    tmp_path: Path,
) -> None:
    """A personal token cannot accidentally become the fleet credential."""
    token_file = tmp_path / "fly-token"
    token_file.write_text(f"{PROVIDER_TOKEN}\n", encoding="utf-8")
    token_file.chmod(0o600)

    credential = FlyCredential.from_file(
        token_file,
        organization="creek-vaults",
        scope=FlyCredentialScope.ORG_DEPLOY,
        expires_at=_NOW + timedelta(days=1),
    )

    assert PROVIDER_TOKEN not in repr(credential)
    with pytest.raises(ValueError, match="org-scoped deploy"):
        FlyCredential(
            token=PROVIDER_TOKEN,
            organization="creek-vaults",
            scope=FlyCredentialScope.PERSONAL,
            expires_at=_NOW + timedelta(days=1),
        )


def test_credential_file_rejects_unsafe_permissions(tmp_path: Path) -> None:
    """Provider credentials are never loaded from a group-readable file."""
    token_file = tmp_path / "fly-token"
    token_file.write_text(PROVIDER_TOKEN, encoding="utf-8")
    token_file.chmod(0o640)

    with pytest.raises(ValueError, match="owner-only and regular"):
        FlyCredential.from_file(
            token_file,
            organization="creek-vaults",
            scope=FlyCredentialScope.ORG_DEPLOY,
            expires_at=_NOW + timedelta(days=1),
        )


@pytest.mark.parametrize(
    "image",
    [
        "registry.example/creek:latest",
        "registry.example/creek@sha256:abc123",
        "registry.example/creek@sha256:" + "z" * 64,
    ],
)
def test_policy_rejects_mutable_or_malformed_image_references(image: str) -> None:
    """Only an exact OCI sha256 digest can reach a Fly Machine request."""
    with pytest.raises(ValueError, match="immutable sha256 digest"):
        FlyProviderPolicy(
            organization="creek-vaults",
            image=image,
        )


def test_policy_rejects_plaintext_provider_transport() -> None:
    """The deploy token can never be sent over a plaintext API connection."""
    with pytest.raises(ValueError, match="HTTPS"):
        FlyProviderPolicy(
            organization="creek-vaults",
            image="registry.example/creek@sha256:" + "a" * 64,
            api_base_url="http://fly.example.test",
        )


@pytest.mark.parametrize(
    "routing_url",
    [
        "http://router.example.com",
        "https://machine.vm.app.internal",
        "https://machine.vm.app.internal.",
        "https://127.0.0.1",
        "https://[fdaa::1]",
        "https://user@router.example.com",
        "https://router.example.com/private-proxy",
        "https://router.example.com?target=internal",
        "https://router.example.com#private-target",
    ],
)
def test_policy_refuses_non_public_handoff_routes(routing_url: str) -> None:
    """A worker cannot hand Adepthood a private or credential-bearing endpoint."""
    with pytest.raises(ValueError, match="public HTTPS"):
        FlyProviderPolicy(
            organization="creek-vaults",
            image="registry.example/creek@sha256:" + "a" * 64,
            routing_public_url=routing_url,
        )


def test_provision_refuses_to_handoff_without_a_public_route() -> None:
    """An omitted route fails closed instead of falling back to private DNS."""
    api = FakeFlyAPI()
    policy = FlyProviderPolicy(
        organization=ORGANIZATION,
        image=IMAGE,
        api_base_url=API_BASE_URL,
    )
    credential = FlyCredential(
        token=PROVIDER_TOKEN,
        organization=ORGANIZATION,
        scope=FlyCredentialScope.ORG_DEPLOY,
        expires_at=_NOW + timedelta(days=1),
    )
    driver = FlyProviderDriver(
        policy,
        credential,
        FakeSecretManager(set()),
        fly_client(api),
    )

    with pytest.raises(ProviderError) as raised:
        driver.provision(fly_job())

    assert raised.value.reason is FailureReason.PROVIDER_REJECTED
    assert api.apps == {}


def test_driver_rejects_cross_organization_credentials() -> None:
    """A rollout cannot silently cross provider tenant boundaries."""
    policy = FlyProviderPolicy(
        organization="creek-vaults",
        image="registry.example/creek@sha256:" + "a" * 64,
    )
    credential = FlyCredential(
        token=PROVIDER_TOKEN,
        organization="another-organization",
        scope=FlyCredentialScope.ORG_DEPLOY,
        expires_at=_NOW + timedelta(days=1),
    )
    with pytest.raises(ValueError, match="organization does not match"):
        FlyProviderDriver(policy, credential, FakeSecretManager(set()))


def test_provision_start_stop_and_delete_are_idempotent() -> None:
    """Every lifecycle call is safe to replay after an unknown outcome."""
    api = FakeFlyAPI()
    secrets = FakeSecretManager(set())
    driver = fly_driver(api, secrets)
    job = fly_job()

    first = driver.provision(job)
    second = driver.provision(job)
    driver.start(job.activation_id)
    driver.start(job.activation_id)
    driver.stop(job.activation_id)
    driver.stop(job.activation_id)
    driver.delete(job, first.allocation_id)
    driver.delete(job, first.allocation_id)

    assert first == second
    assert api.apps == {}
    assert not any(api.volumes.values())
    assert not any(api.machines.values())
    assert secrets.revoked == {job.activation_id}


def test_stopped_machine_restarts_with_the_same_encrypted_volume() -> None:
    """Scale-to-zero preserves the durable volume across demand starts."""
    api = FakeFlyAPI()
    driver = fly_driver(api)
    job = fly_job()
    driver.provision(job)
    original_volume = _only_machine(api)["config"]["mounts"][0]["volume"]

    driver.start(job.activation_id)
    driver.stop(job.activation_id)
    driver.start(job.activation_id)

    machine = _only_machine(api)
    assert machine["state"] == "started"
    assert machine["config"]["mounts"][0] == {
        "volume": original_volume,
        "path": "/vault",
        "encrypted": True,
    }


def test_background_work_owns_commit_close_and_shutdown_order() -> None:
    """Returning from initiation cannot stop work; the durable worker does."""
    api = FakeFlyAPI()
    driver = fly_driver(api)
    job = fly_job()
    driver.provision(job)

    driver.start(job.activation_id)
    assert _only_machine(api)["state"] == "started"
    events: list[str] = []

    def record(event: str) -> Callable[[], None]:
        return lambda: events.append(event)

    driver.run_background_job(
        job.activation_id,
        drain=record("drain"),
        commit=record("commit"),
        close_vault=record("close"),
    )

    assert events == ["drain", "commit", "close"]
    assert _only_machine(api)["state"] == "stopped"


def test_background_failure_still_closes_and_stops_the_vault() -> None:
    """Failed durable work cannot strand an unlocked, billable Machine."""
    api = FakeFlyAPI()
    driver = fly_driver(api)
    job = fly_job()
    driver.provision(job)
    events: list[str] = []

    def fail_drain() -> None:
        events.append("drain")
        raise RuntimeError("synthetic durable-work failure")

    with pytest.raises(RuntimeError, match="durable-work failure"):
        driver.run_background_job(
            job.activation_id,
            drain=fail_drain,
            commit=lambda: events.append("commit"),
            close_vault=lambda: events.append("close"),
        )

    assert events == ["drain", "close"]
    assert _only_machine(api)["state"] == "stopped"


def test_delete_rejects_a_cross_activation_allocation_before_revocation() -> None:
    """A stale queue record cannot delete or revoke another allocation."""
    api = FakeFlyAPI()
    secrets = FakeSecretManager(set())
    driver = fly_driver(api, secrets)
    job = fly_job()
    allocation = driver.provision(job)

    with pytest.raises(ProviderError) as raised:
        driver.delete(job, "fly-allocation-for-a-different-activation")

    assert raised.value.retryable is False
    assert allocation.allocation_id != "fly-allocation-for-a-different-activation"
    assert len(api.apps) == 1
    assert secrets.revoked == set()


def test_reconciliation_rejects_colliding_app_or_machine_identity() -> None:
    """Deterministic names cannot make foreign Fly resources adoptable."""
    api = FakeFlyAPI()
    driver = fly_driver(api)
    job = fly_job()
    driver.provision(job)
    app_name = _only_app(api)
    api.apps[app_name]["organization"] = {"slug": "foreign-organization"}

    with pytest.raises(ProviderError) as app_error:
        driver.provision(job)
    assert app_error.value.retryable is False

    api.apps[app_name]["organization"] = {"slug": "creek-vaults"}
    api.machines[app_name].append(dict(_only_machine(api)))
    with pytest.raises(ProviderError) as machine_error:
        driver.provision(job)
    assert machine_error.value.retryable is False


def test_worker_supplies_activation_context_to_the_provider(tmp_path: Path) -> None:
    """Crash reconciliation is keyed by activation, not an opaque queue id."""
    api = FakeFlyAPI()
    store = ProvisioningStore(tmp_path / "provisioning.sqlite3")
    job = store.submit("activation-from-worker", "adepthood", now=_NOW)
    worker = ProvisioningWorker(store, fly_driver(api), FakeOneTimeHandoff())

    assert worker.run_once(now=_NOW) is True

    assert _only_machine(api)["config"]["metadata"]["creek_activation_id"] == (
        job.activation_id
    )


@pytest.mark.integration
def test_fake_fly_api_reconciles_partial_creation() -> None:
    """A retry adopts one app and volume left by a failed Machine create."""
    api = FakeFlyAPI()
    api.fail_once("POST", "/machines")
    driver = fly_driver(api)
    job = fly_job("activation-partial-create")

    with pytest.raises(ProviderError) as raised:
        driver.provision(job)
    assert raised.value.retryable is True
    app_name = _only_app(api)
    assert len(api.volumes[app_name]) == 1
    assert api.machines[app_name] == []

    driver.provision(job)

    assert len(api.apps) == 1
    assert len(api.volumes[app_name]) == 1
    assert len(api.machines[app_name]) == 1


@pytest.mark.integration
def test_fake_fly_api_reconciles_partial_deletion() -> None:
    """A failed teardown remains retryable until every billable resource is gone."""
    api = FakeFlyAPI()
    secrets = FakeSecretManager(set())
    driver = fly_driver(api, secrets)
    job = fly_job("activation-partial-delete")
    allocation = driver.provision(job)
    api.fail_once("DELETE", "/volumes/vol-1")

    with pytest.raises(ProviderError) as raised:
        driver.delete(job, allocation.allocation_id)
    assert raised.value.retryable is True
    app_name = _only_app(api)
    assert api.machines[app_name] == []
    assert len(api.volumes[app_name]) == 1

    driver.delete(job, allocation.allocation_id)

    assert api.apps == {}
    assert not any(api.volumes.values())
    assert not any(api.machines.values())
    assert secrets.revoked == {job.activation_id}


def test_delete_returns_a_provider_confirmed_outcome() -> None:
    """The outcome names provider and classes only after absence is verified."""
    api = FakeFlyAPI()
    secrets = FakeSecretManager(set())
    driver = fly_driver(api, secrets)
    job = fly_job()
    allocation = driver.provision(job)

    outcome = driver.delete(job, allocation.allocation_id)
    replay = driver.delete(job, allocation.allocation_id)

    assert outcome == DeletionOutcome(
        "fly",
        (
            ResourceClass.CREDENTIAL,
            ResourceClass.MACHINE,
            ResourceClass.VOLUME,
            ResourceClass.APP,
        ),
    )
    assert replay == outcome
    assert api.apps == {}
    for canary in (PROVIDER_TOKEN, CONSUMER_TOKEN, TLS_KEY):
        assert canary not in repr(outcome)


def test_list_resources_reads_only_per_app_get_calls_and_maps_states_and_sizes() -> (
    None
):
    """Inventory is three GETs per app, grouped by the app-derived allocation id."""
    api = FakeFlyAPI()
    driver = fly_driver(api)
    allocation = driver.provision(fly_job())
    app_name = _only_app(api)
    api.requests.clear()

    resources = driver.list_resources(["activation-fly-001"])

    pid = allocation.allocation_id
    assert resources == (
        ProviderResource(
            pid, ResourceClass.APP, ResourceState.OTHER, None, None, "app-1"
        ),
        ProviderResource(
            pid,
            ResourceClass.MACHINE,
            ResourceState.STOPPED,
            1,
            "activation-fly-001",
            "machine-1",
        ),
        ProviderResource(
            pid, ResourceClass.VOLUME, ResourceState.OTHER, 5, None, "vol-1"
        ),
    )
    assert all(method == "GET" for method, _ in api.requests)
    assert {path for _, path in api.requests} == {
        f"/v1/apps/{app_name}",
        f"/v1/apps/{app_name}/machines",
        f"/v1/apps/{app_name}/volumes",
    }
    assert len(api.requests) == 3
    assert driver.expected_allocation_id("activation-fly-001") == pid

    machine = api.machines[app_name][0]
    for fly_state, expected in (
        ("started", ResourceState.RUNNING),
        ("starting", ResourceState.RUNNING),
        ("stopping", ResourceState.STOPPED),
        ("suspended", ResourceState.STOPPED),
        ("replacing", ResourceState.OTHER),
        ("destroyed", ResourceState.DESTROYED),
    ):
        machine["state"] = fly_state
        listed = driver.list_resources(["activation-fly-001"])
        assert [
            r.state for r in listed if r.resource_class is ResourceClass.MACHINE
        ] == [expected]
    del machine["config"]["rootfs"]
    api.volumes[app_name][0]["state"] = "destroyed"
    listed = driver.list_resources(["activation-fly-001"])
    assert [r.size_gb for r in listed if r.resource_class is ResourceClass.MACHINE] == [
        None
    ]
    assert [r.state for r in listed if r.resource_class is ResourceClass.VOLUME] == [
        ResourceState.DESTROYED
    ]


def test_list_resources_covers_injected_app_names_and_skips_missing_apps() -> None:
    """Discovery is bounded to derived and injected names under the app prefix."""
    api = FakeFlyAPI()
    driver = fly_driver(api)
    orphan_app = "creek-vault-" + "0" * 24
    api.apps[orphan_app] = {"id": "app-orphan", "name": orphan_app}
    api.volumes[orphan_app].append(
        {"id": "vol-orphan", "name": "left-behind", "size_gb": 3, "state": "created"}
    )
    api.apps["not-ours"] = {"id": "app-foreign", "name": "not-ours"}

    missing_app = "creek-vault-" + "f" * 24
    resources = driver.list_resources(
        [],
        app_names=[orphan_app, "not-ours", missing_app, "creek-vault-missing"],
    )
    nothing = driver.list_resources(["activation-never-provisioned"])

    pid = "fly-" + "0" * 24
    assert resources == (
        ProviderResource(
            pid, ResourceClass.APP, ResourceState.OTHER, None, None, "app-orphan"
        ),
        ProviderResource(
            pid, ResourceClass.VOLUME, ResourceState.OTHER, 3, None, "vol-orphan"
        ),
    )
    assert nothing == ()
    assert not any("not-ours" in path for _, path in api.requests)
    assert [path for _, path in api.requests if missing_app in path] == [
        f"/v1/apps/{missing_app}"
    ]
    assert not any("creek-vault-missing" in path for _, path in api.requests)
    assert ("GET", "/v1/apps") not in api.requests


def test_list_resources_discovers_prefixed_apps_from_the_provider_organization() -> (
    None
):
    """Production inventory finds forgotten apps without a hand-maintained file."""
    api = FakeFlyAPI()
    driver = fly_driver(api, discover_organization_apps=True)
    orphan_app = "creek-vault-" + "0" * 24
    api.apps[orphan_app] = {
        "id": "app-orphan",
        "name": orphan_app,
        "organization": {"slug": "creek-vaults"},
    }
    api.apps["not-ours"] = {
        "id": "app-foreign",
        "name": "not-ours",
        "organization": {"slug": "creek-vaults"},
    }
    api.volumes[orphan_app].append(
        {"id": "vol-orphan", "name": "left-behind", "size_gb": 3}
    )

    resources = driver.list_resources([])

    assert [resource.provider_ref for resource in resources] == [
        "app-orphan",
        "vol-orphan",
    ]
    assert api.requests[0] == ("GET", "/v1/apps")
    assert not any("not-ours" in path for _, path in api.requests)


@pytest.mark.parametrize(
    "body",
    [
        '["inventory-body-canary"]',
        '{"apps": {}, "echo": "inventory-body-canary"}',
        '{"apps": [null], "echo": "inventory-body-canary"}',
        '{"apps": [{"echo": "inventory-body-canary"}]}',
        '{"apps": [{"name": 7, "echo": "inventory-body-canary"}]}',
        '{"total_apps": 2, "apps": [{"name": "inventory-body-canary"}]}',
    ],
)
def test_organization_inventory_shape_fails_closed_without_echo(
    caplog: pytest.LogCaptureFixture,
    body: str,
) -> None:
    """Malformed org discovery is unavailable and never becomes empty inventory."""
    api = FakeFlyAPI()
    driver = fly_driver(api, discover_organization_apps=True)
    api.malformed_once("GET", "/v1/apps", body)

    with caplog.at_level(logging.DEBUG), pytest.raises(ProviderError) as raised:
        driver.list_resources([])

    assert raised.value.reason is FailureReason.PROVIDER_UNAVAILABLE
    assert raised.value.retryable is True
    rendered = caplog.text + str(raised.value) + repr(raised.value) + repr(driver)
    assert "inventory-body-canary" not in rendered
    assert PROVIDER_TOKEN not in rendered


def test_machine_metadata_claiming_another_allocation_stays_under_its_own_app() -> None:
    """Metadata is informational: a rogue Machine cannot be adopted by its claim."""
    api = FakeFlyAPI()
    driver = fly_driver(api)
    first = driver.provision(fly_job("activation-fly-001"))
    second = driver.provision(fly_job("activation-fly-002"))
    app_of_first = next(
        name for name, app in api.apps.items() if app["network"] == first.allocation_id
    )
    api.machines[app_of_first].append(
        {
            "id": "machine-rogue",
            "name": "rogue",
            "region": "iad",
            "state": "stopped",
            "config": {
                "rootfs": {"size_gb": 1},
                "metadata": {
                    "creek_allocation_id": second.allocation_id,
                    "creek_activation_id": "activation-fly-002",
                },
            },
        }
    )

    resources = driver.list_resources(["activation-fly-001", "activation-fly-002"])

    rogue = [r for r in resources if r.provider_ref == "machine-rogue"]
    assert rogue == [
        ProviderResource(
            first.allocation_id,
            ResourceClass.MACHINE,
            ResourceState.STOPPED,
            1,
            "activation-fly-002",
            "machine-rogue",
        )
    ]
    second_machines = [
        r
        for r in resources
        if r.provider_allocation_id == second.allocation_id
        and r.resource_class is ResourceClass.MACHINE
    ]
    assert len(second_machines) == 1


def test_malformed_inventory_bodies_fail_closed_without_leaking(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A non-list inventory body is a retryable provider fault with no echo."""
    api = FakeFlyAPI()
    driver = fly_driver(api)
    driver.provision(fly_job())
    body_canary = "inventory-body-canary-" + PROVIDER_TOKEN
    api.malformed_once("GET", "/machines", f'{{"echo": "{body_canary}"}}')

    with caplog.at_level(logging.DEBUG), pytest.raises(ProviderError) as raised:
        driver.list_resources(["activation-fly-001"])

    assert raised.value.reason is FailureReason.PROVIDER_UNAVAILABLE
    assert raised.value.retryable is True
    rendered = caplog.text + str(raised.value) + repr(raised.value) + repr(driver)
    assert body_canary not in rendered
    assert PROVIDER_TOKEN not in rendered


def test_injected_app_names_outside_the_strict_pattern_never_reach_the_provider() -> (
    None
):
    """Only ``<prefix>-<24 hex>`` names are inventoried; nothing escapes the prefix."""
    api = FakeFlyAPI()
    driver = fly_driver(api)
    valid = "creek-vault-" + "a" * 24
    api.apps[valid] = {"id": "app-valid", "name": valid}

    resources = driver.list_resources(
        [],
        app_names=[
            "creek-vault-x/../other-app",
            "creek-vault-" + "A" * 24,
            "creek-vault-" + "a" * 23,
            "creek-vault-" + "a" * 25,
            "creek-vault-../../v1/apps",
            valid,
        ],
    )

    assert [r.provider_ref for r in resources] == ["app-valid"]
    assert [path for _, path in api.requests] == [
        f"/v1/apps/{valid}",
        f"/v1/apps/{valid}/machines",
        f"/v1/apps/{valid}/volumes",
    ]
    assert all(path.startswith("/v1/apps/creek-vault-") for _, path in api.requests)
    assert not any(".." in path or "other-app" in path for _, path in api.requests)


def test_fly_provision_and_delete_leave_no_secret_in_the_durable_database(
    tmp_path: Path,
) -> None:
    """The SQLite file and the receipt carry no provider, consumer or TLS secret."""
    api = FakeFlyAPI()
    database = tmp_path / "provisioning.sqlite3"
    store = ProvisioningStore(database)
    worker = ProvisioningWorker(store, fly_driver(api), FakeOneTimeHandoff())
    job = store.submit("activation-fly-bytes", "adepthood", now=_NOW)
    assert worker.run_once(now=_NOW) is True
    store.request_delete(job.job_id, "adepthood", now=_NOW)
    assert worker.run_once(now=_NOW) is True

    persisted = database.read_bytes()
    receipt = store.list_deletion_receipts()[0]

    assert receipt.outcome.value == "confirmed"
    assert receipt.provider == "fly"
    for canary in (PROVIDER_TOKEN, CONSUMER_TOKEN, TLS_KEY):
        assert canary.encode() not in persisted
        assert canary not in repr(receipt)
