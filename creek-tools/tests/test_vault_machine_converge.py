"""Converging existing vault Machines after an image or runtime change (#1869).

Before #1869 a vault Machine created under an older vault image or runtime
config was refused on every route, forever: ``_verify_machine`` demands an
exact match and nothing ever updated an existing Machine.  The owner's
decision (R2-1, 2026-10-27) is an operator-run batch converge on top of a
strict safety rule: a Machine is converged only when its identity is exact —
name, region, metadata, the single mount on the allocation's own volume, and
secret files its own record reproduces — and only policy-owned fields differ.
Only a stopped Machine is updated; the volume is never touched and the
consumer credential is never rotated.  Every value here is synthetic.
"""

from __future__ import annotations

import base64
import json
import stat
from typing import TYPE_CHECKING, Any, Final

import pytest

from creek.classify.llm.consent import MANAGED_VAULT_ENV
from creek_mcp.provisioning import vault_converge
from creek_mcp.provisioning.driver import ProviderError
from creek_mcp.provisioning.models import FailureReason, RoutableAllocation
from creek_mcp.provisioning.vault_converge import ConvergeJournal, ConvergeOutcome
from tests.fly_support import (
    CONSUMER_TOKEN,
    IMAGE,
    ROUTING_PUBLIC_URL,
    TLS_KEY,
    FakeFlyAPI,
    FakeSecretManager,
    fly_driver,
    fly_job,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from creek_mcp.provisioning.fly import FlyProviderDriver

_OLD_IMAGE: Final[str] = "registry.example/creek@sha256:" + "9" * 64
_CONSUMER: Final[str] = "adepthood-user-001"
_ACTIVATIONS: Final[tuple[str, ...]] = (
    "activation-converge-001",
    "activation-converge-002",
    "activation-converge-003",
)


class _Allocations:
    """The store's operator listing of every routable allocation."""

    def __init__(self, allocations: tuple[RoutableAllocation, ...]) -> None:
        self.allocations = allocations

    def list_routable_allocations(self) -> tuple[RoutableAllocation, ...]:
        """Return the fixed allocation set."""
        return self.allocations


def _routable(driver: FlyProviderDriver, activation_id: str) -> RoutableAllocation:
    """Return the store-owned routing record for *activation_id*."""
    return RoutableAllocation(
        f"job-{activation_id}",
        activation_id,
        "adepthood",
        _CONSUMER,
        driver.expected_allocation_id(activation_id),
    )


def _fleet(
    count: int = 1,
    *,
    replay: bool = True,
) -> tuple[FakeFlyAPI, FakeSecretManager, FlyProviderDriver, _Allocations]:
    """Provision *count* vaults under the old image; return the new driver.

    Returns:
        The fake provider, the shared records, a driver whose policy carries
        the new image, and the store listing of the provisioned allocations.
    """
    api = FakeFlyAPI()
    secrets = FakeSecretManager(set())
    old = fly_driver(api, secrets, fly_replay_enabled=replay, image=_OLD_IMAGE)
    for activation_id in _ACTIVATIONS[:count]:
        old.provision(fly_job(activation_id))
    new = fly_driver(api, secrets, fly_replay_enabled=replay)
    allocations = tuple(_routable(new, a) for a in _ACTIVATIONS[:count])
    return api, secrets, new, _Allocations(allocations)


def _machine(api: FakeFlyAPI, driver: FlyProviderDriver, index: int = 0) -> Any:
    """Return the one Machine of the *index*-th allocation."""
    allocation_id = driver.expected_allocation_id(_ACTIVATIONS[index])
    app_name = "creek-vault-" + allocation_id.removeprefix("fly-")
    (machine,) = api.machines[app_name]
    return machine


def _mutations(api: FakeFlyAPI) -> list[tuple[str, str]]:
    """Return every mutating provider request made so far."""
    return [(method, path) for method, path in api.requests if method != "GET"]


def _converge(
    allocations: _Allocations,
    driver: FlyProviderDriver,
    journal: ConvergeJournal | None,
    *,
    apply: bool = True,
    limit: int = 10,
) -> vault_converge.FleetReport:
    """Run one converge pass."""
    return vault_converge.converge_fleet(
        allocations, driver, journal, apply=apply, limit=limit
    )


def _outcomes(report: vault_converge.FleetReport) -> list[ConvergeOutcome]:
    """Return the per-allocation outcomes in order."""
    return [entry.outcome for entry in report.allocations]


# --------------------------------------------------------------------------- #
# Routing never mutates: it refuses a stale Machine retryably
# --------------------------------------------------------------------------- #


def test_routing_refuses_a_stale_image_retryably_without_touching_it() -> None:
    """A Machine behind the policy image awaits the operator, retryably."""
    api, _secrets, driver, allocations = _fleet()
    before = len(_mutations(api))

    with pytest.raises(ProviderError) as raised:
        driver.prepare_replay(allocations.allocations[0])

    assert raised.value.retryable is True
    assert raised.value.reason is FailureReason.PROVIDER_UNAVAILABLE
    assert len(_mutations(api)) == before
    assert api.updates == []


@pytest.mark.parametrize(
    ("_label", "mutate"),
    [
        ("mount", lambda m: m["config"]["mounts"][0].update(volume="vol-foreign")),
        (
            "metadata",
            lambda m: m["config"]["metadata"].update(
                creek_activation_id="activation-foreign"
            ),
        ),
    ],
)
def test_routing_still_rejects_a_foreign_identity(
    _label: str,
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    """Identity drift stays a permanent rejection, never an upgrade candidate."""
    api, _secrets, driver, allocations = _fleet()
    mutate(_machine(api, driver))

    with pytest.raises(ProviderError) as raised:
        driver.prepare_replay(allocations.allocations[0])

    assert raised.value.reason is FailureReason.PROVIDER_REJECTED
    assert raised.value.retryable is False


# --------------------------------------------------------------------------- #
# A provisioning retry converges the Machine it created under an older policy
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("replay", [True, False])
def test_a_provisioning_retry_converges_its_stopped_stale_machine(
    replay: bool,
) -> None:
    """The worker's own never-handed-off Machine is updated in place."""
    api, secrets, driver, _allocations = _fleet(replay=replay)
    machine = _machine(api, driver)
    machine["config"]["env"].pop(MANAGED_VAULT_ENV)
    issued = dict(secrets.issued)

    driver.provision(fly_job(_ACTIVATIONS[0]))

    (update,) = api.updates
    assert update["skip_launch"] is True
    assert machine["config"]["image"] == IMAGE
    assert machine["config"]["env"][MANAGED_VAULT_ENV] == "1"
    assert machine["state"] == "stopped"
    assert secrets.issued == issued
    assert not any("/volumes" in path for _, path in _mutations(api)[2:])


def test_a_provisioning_retry_refuses_a_running_stale_machine_retryably() -> None:
    """A running Machine is never updated; the claim retries once it stops."""
    api, _secrets, driver, _allocations = _fleet()
    _machine(api, driver)["state"] = "started"

    with pytest.raises(ProviderError) as raised:
        driver.provision(fly_job(_ACTIVATIONS[0]))

    assert raised.value.retryable is True
    assert api.updates == []


def test_a_provisioning_retry_refuses_a_foreign_mount() -> None:
    """A Machine mounted on another volume is still refused outright."""
    api, _secrets, driver, _allocations = _fleet()
    _machine(api, driver)["config"]["mounts"][0]["volume"] = "vol-foreign"

    with pytest.raises(ProviderError) as raised:
        driver.provision(fly_job(_ACTIVATIONS[0]))

    assert raised.value.reason is FailureReason.PROVIDER_REJECTED
    assert api.updates == []


def test_a_failed_provisioning_update_is_retryable_and_resumes() -> None:
    """A provider failure on the update is retryable, and the retry converges."""
    api, _secrets, driver, _allocations = _fleet()
    machine = _machine(api, driver)
    api.fail_once("POST", f"/machines/{machine['id']}")

    with pytest.raises(ProviderError) as raised:
        driver.provision(fly_job(_ACTIVATIONS[0]))

    assert raised.value.retryable is True
    assert machine["config"]["image"] == _OLD_IMAGE
    driver.provision(fly_job(_ACTIVATIONS[0]))
    assert machine["config"]["image"] == IMAGE


# --------------------------------------------------------------------------- #
# The operator batch: dry run
# --------------------------------------------------------------------------- #


def test_a_dry_run_lists_the_planned_diff_and_mutates_nothing(
    tmp_path: Path,
) -> None:
    """The default pass reads only: no update, no start, no journal write."""
    api, _secrets, driver, allocations = _fleet(2)
    _machine(api, driver, 1)["config"]["env"].pop(MANAGED_VAULT_ENV)
    before = len(_mutations(api))
    journal_path = tmp_path / "converge.json"

    report = _converge(allocations, driver, ConvergeJournal(journal_path), apply=False)

    assert _outcomes(report) == [ConvergeOutcome.WOULD_CONVERGE] * 2
    assert report.allocations[0].drift == ("image",)
    assert report.allocations[1].drift == ("env." + MANAGED_VAULT_ENV, "image")
    assert len(_mutations(api)) == before
    assert not journal_path.exists()
    assert report.exit_status == vault_converge.EXIT_PENDING


def test_a_current_fleet_reports_already_current() -> None:
    """A Machine already on policy is left alone and the pass exits clean."""
    api = FakeFlyAPI()
    driver = fly_driver(api, fly_replay_enabled=True)
    driver.provision(fly_job(_ACTIVATIONS[0]))
    allocations = _Allocations((_routable(driver, _ACTIVATIONS[0]),))

    report = _converge(allocations, driver, None, apply=False)

    assert _outcomes(report) == [ConvergeOutcome.ALREADY_CURRENT]
    assert report.exit_status == vault_converge.EXIT_CURRENT


# --------------------------------------------------------------------------- #
# The operator batch: apply
# --------------------------------------------------------------------------- #


def test_apply_converges_a_stale_image_and_verifies_readiness(
    tmp_path: Path,
) -> None:
    """A stale image is updated while stopped, re-verified, started and routed."""
    api, secrets, driver, allocations = _fleet()
    issued = dict(secrets.issued)
    machine = _machine(api, driver)
    volumes = json.dumps(api.volumes, sort_keys=True)
    journal = ConvergeJournal(tmp_path / "converge.json")

    report = _converge(allocations, driver, journal)

    assert _outcomes(report) == [ConvergeOutcome.CONVERGED]
    (update,) = api.updates
    assert update["skip_launch"] is True
    assert machine["config"]["image"] == IMAGE
    assert machine["state"] == "started"
    assert secrets.issued == issued
    assert json.dumps(api.volumes, sort_keys=True) == volumes
    assert not any("/volumes" in path for _, path in _mutations(api)[3:])
    assert report.exit_status == vault_converge.EXIT_CURRENT
    driver.prepare_replay(allocations.allocations[0])


def test_apply_converges_stale_runtime_settings(tmp_path: Path) -> None:
    """A Machine from before #1843/#1864 takes the current runtime config."""
    api, _secrets, driver, allocations = _fleet()
    config = _machine(api, driver)["config"]
    config["env"].pop(MANAGED_VAULT_ENV)
    config.pop("processes")
    config["user"] = "root"

    report = _converge(allocations, driver, ConvergeJournal(tmp_path / "j.json"))

    assert _outcomes(report) == [ConvergeOutcome.CONVERGED]
    current = _machine(api, driver)["config"]
    assert current["env"][MANAGED_VAULT_ENV] == "1"
    assert "user" not in current
    assert current["processes"]


@pytest.mark.parametrize(
    ("_label", "mutate"),
    [
        ("mount", lambda m: m["config"]["mounts"][0].update(volume="vol-foreign")),
        (
            "extra-mount",
            lambda m: m["config"]["mounts"].append(dict(m["config"]["mounts"][0])),
        ),
        (
            "metadata",
            lambda m: m["config"]["metadata"].update(
                creek_allocation_id="fly-" + "0" * 24
            ),
        ),
        ("name", lambda m: m.update(name="another-machine")),
        ("region", lambda m: m.update(region="ord")),
        (
            "foreign-secret",
            lambda m: m["config"]["files"][0].update(raw_value="c3RhbGU="),
        ),
        (
            "mutable-image",
            lambda m: m["config"].update(image="registry.example/creek:latest"),
        ),
        ("unknown-field", lambda m: m["config"].update(unreviewed=True)),
    ],
)
def test_apply_refuses_a_foreign_identity(
    tmp_path: Path,
    _label: str,
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    """Any identity mismatch is refused and nothing is written."""
    api, _secrets, driver, allocations = _fleet()
    mutate(_machine(api, driver))
    journal_path = tmp_path / "converge.json"

    report = _converge(allocations, driver, ConvergeJournal(journal_path))

    assert _outcomes(report) == [ConvergeOutcome.REFUSED_IDENTITY]
    assert api.updates == []
    assert not journal_path.exists()
    assert report.exit_status == vault_converge.EXIT_REFUSED


def test_apply_skips_a_running_machine(tmp_path: Path) -> None:
    """A running Machine keeps serving untouched; the pass asks for a retry."""
    api, _secrets, driver, allocations = _fleet()
    _machine(api, driver)["state"] = "started"

    report = _converge(allocations, driver, ConvergeJournal(tmp_path / "j.json"))

    assert _outcomes(report) == [ConvergeOutcome.SKIPPED_RUNNING]
    assert api.updates == []
    assert report.exit_status == vault_converge.EXIT_RETRY


def test_an_update_failure_halts_the_batch_and_a_rerun_resumes(
    tmp_path: Path,
) -> None:
    """A failed update is retryable, stops the batch, and a re-run finishes."""
    api, _secrets, driver, allocations = _fleet(2)
    first = _machine(api, driver, 0)
    api.fail_once("POST", f"/machines/{first['id']}")
    journal_path = tmp_path / "converge.json"

    halted = _converge(allocations, driver, ConvergeJournal(journal_path))

    assert _outcomes(halted) == [ConvergeOutcome.FAILED_RETRYABLE]
    assert halted.allocations[0].reason == FailureReason.PROVIDER_UNAVAILABLE.value
    assert halted.exit_status == vault_converge.EXIT_RETRY
    assert api.updates == []
    recorded = json.loads(journal_path.read_text(encoding="utf-8"))

    resumed = _converge(allocations, driver, ConvergeJournal(journal_path))

    assert _outcomes(resumed) == [ConvergeOutcome.CONVERGED] * 2
    entries = json.loads(journal_path.read_text(encoding="utf-8"))["entries"]
    first_id = allocations.allocations[0].provider_allocation_id
    assert (
        entries[first_id]["prior_fingerprint"]
        == recorded["entries"][first_id]["prior_fingerprint"]
    )
    assert _converge(
        allocations, driver, ConvergeJournal(journal_path)
    ).allocations == tuple(
        vault_converge.AllocationReport(
            a.provider_allocation_id, ConvergeOutcome.ALREADY_CURRENT
        )
        for a in allocations.allocations
    )


def test_a_readiness_failure_is_resumed_by_the_next_run(tmp_path: Path) -> None:
    """An update whose readiness was not proven is re-verified, not re-applied."""
    api, _secrets, driver, allocations = _fleet()
    machine = _machine(api, driver)
    api.fail_once("GET", f"/machines/{machine['id']}/wait")
    journal_path = tmp_path / "converge.json"

    failed = _converge(allocations, driver, ConvergeJournal(journal_path))

    assert _outcomes(failed) == [ConvergeOutcome.FAILED_RETRYABLE]
    assert len(api.updates) == 1
    resumed = _converge(allocations, driver, ConvergeJournal(journal_path))
    assert _outcomes(resumed) == [ConvergeOutcome.CONVERGED]
    assert len(api.updates) == 1


def test_the_limit_bounds_updates_and_a_rerun_converges_the_rest(
    tmp_path: Path,
) -> None:
    """At most ``limit`` Machines are updated per run; re-runs continue."""
    api, _secrets, driver, allocations = _fleet(3)
    journal_path = tmp_path / "converge.json"

    first = _converge(allocations, driver, ConvergeJournal(journal_path), limit=2)

    assert _outcomes(first) == [ConvergeOutcome.CONVERGED] * 2
    assert first.limit_reached is True
    assert first.exit_status == vault_converge.EXIT_PENDING
    for index in range(2):
        _machine(api, driver, index)["state"] = "stopped"
    second = _converge(allocations, driver, ConvergeJournal(journal_path), limit=2)
    assert _outcomes(second) == [
        ConvergeOutcome.ALREADY_CURRENT,
        ConvergeOutcome.ALREADY_CURRENT,
        ConvergeOutcome.CONVERGED,
    ]
    assert second.limit_reached is False
    assert len(api.updates) == 3


def test_apply_without_a_journal_is_refused() -> None:
    """An update is never applied without a place to record its prior state."""
    _api, _secrets, driver, allocations = _fleet()

    with pytest.raises(ValueError, match="journal"):
        _converge(allocations, driver, None)


def test_a_journal_from_another_upgrade_refuses_the_machine(tmp_path: Path) -> None:
    """An entry whose prior is not this Machine is never overwritten."""
    api, _secrets, driver, allocations = _fleet()
    journal_path = tmp_path / "converge.json"
    _converge(allocations, driver, ConvergeJournal(journal_path))
    machine = _machine(api, driver)
    machine["state"] = "stopped"
    machine["config"]["guest"] = {"cpu_kind": "shared", "cpus": 2, "memory_mb": 1024}

    report = _converge(allocations, driver, ConvergeJournal(journal_path))

    assert _outcomes(report) == [ConvergeOutcome.FAILED]
    assert report.allocations[0].reason == "journal"
    assert len(api.updates) == 1


# --------------------------------------------------------------------------- #
# Rollback
# --------------------------------------------------------------------------- #


def _converged(
    tmp_path: Path,
) -> tuple[FakeFlyAPI, FlyProviderDriver, _Allocations, Path, dict[str, Any]]:
    """Converge one stale vault, stop it, and return the prior config."""
    api, _secrets, driver, allocations = _fleet()
    config = _machine(api, driver)["config"]
    config["env"].pop(MANAGED_VAULT_ENV)
    prior = json.loads(json.dumps(config))
    journal_path = tmp_path / "converge.json"
    _converge(allocations, driver, ConvergeJournal(journal_path))
    _machine(api, driver)["state"] = "stopped"
    return api, driver, allocations, journal_path, prior


def _rollback(
    allocations: _Allocations,
    driver: FlyProviderDriver,
    journal_path: Path,
    *,
    apply: bool = True,
) -> vault_converge.FleetReport:
    """Run one rollback pass."""
    return vault_converge.rollback_fleet(
        allocations, driver, ConvergeJournal(journal_path), apply=apply, limit=10
    )


def test_rollback_restores_the_recorded_prior_config(tmp_path: Path) -> None:
    """A rollback puts back exactly the pre-converge image and runtime config."""
    api, driver, allocations, journal_path, prior = _converged(tmp_path)

    dry = _rollback(allocations, driver, journal_path, apply=False)
    assert _outcomes(dry) == [ConvergeOutcome.WOULD_RESTORE]
    assert len(api.updates) == 1

    restored = _rollback(allocations, driver, journal_path)

    assert _outcomes(restored) == [ConvergeOutcome.RESTORED]
    machine = _machine(api, driver)
    assert machine["config"] == prior
    assert machine["config"]["image"] == _OLD_IMAGE
    assert machine["state"] == "stopped"
    assert _outcomes(_rollback(allocations, driver, journal_path)) == [
        ConvergeOutcome.ALREADY_CURRENT
    ]
    assert len(api.updates) == 2


def test_rollback_refuses_a_machine_changed_since_the_converge(
    tmp_path: Path,
) -> None:
    """Only the exact converged Machine is rolled back."""
    api, driver, allocations, journal_path, _prior = _converged(tmp_path)
    _machine(api, driver)["config"]["env"]["CREEK_EXTRA"] = "drift"

    report = _rollback(allocations, driver, journal_path)

    assert _outcomes(report) == [ConvergeOutcome.REFUSED_IDENTITY]
    assert len(api.updates) == 1


def test_rollback_skips_a_running_machine(tmp_path: Path) -> None:
    """A running Machine is not rolled back until it stops."""
    api, driver, allocations, journal_path, _prior = _converged(tmp_path)
    _machine(api, driver)["state"] = "started"

    report = _rollback(allocations, driver, journal_path)

    assert _outcomes(report) == [ConvergeOutcome.SKIPPED_RUNNING]
    assert len(api.updates) == 1


def test_rollback_ignores_allocations_the_journal_never_recorded(
    tmp_path: Path,
) -> None:
    """With nothing recorded there is nothing to restore."""
    _api, _secrets, driver, allocations = _fleet()

    report = _rollback(allocations, driver, tmp_path / "absent.json")

    assert report.allocations == ()
    assert report.exit_status == vault_converge.EXIT_CURRENT


# --------------------------------------------------------------------------- #
# Content-free records and output
# --------------------------------------------------------------------------- #


def _secret_canaries() -> tuple[str, ...]:
    """Return every secret value, raw and as Fly's base64 file value."""
    raw = (CONSUMER_TOKEN, TLS_KEY, "r" * 64)
    return raw + tuple(base64.b64encode(value.encode()).decode() for value in raw)


def test_the_journal_is_owner_only_and_holds_no_secret(tmp_path: Path) -> None:
    """Prior file values are recorded only as digests."""
    _api, driver, _allocations, journal_path, _prior = _converged(tmp_path)
    del driver

    text = journal_path.read_text(encoding="utf-8")
    assert stat.S_IMODE(journal_path.stat().st_mode) == 0o600
    assert _OLD_IMAGE in text
    for canary in _secret_canaries():
        assert canary not in text
    assert _CONSUMER not in text


def test_an_invalid_journal_is_refused(tmp_path: Path) -> None:
    """A journal that cannot be trusted refuses the run before any request."""
    journal_path = tmp_path / "converge.json"
    journal_path.write_text("{}", encoding="utf-8")
    journal_path.chmod(0o600)

    with pytest.raises(ValueError, match="journal"):
        ConvergeJournal(journal_path)


def test_a_tampered_journal_prior_is_refused(tmp_path: Path) -> None:
    """A prior whose digest no longer matches its record is never restored."""
    _api, _driver, _allocations, journal_path, _prior = _converged(tmp_path)
    document = json.loads(journal_path.read_text(encoding="utf-8"))
    (entry,) = document["entries"].values()
    entry["prior"]["image"] = IMAGE
    journal_path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ValueError, match="journal"):
        ConvergeJournal(journal_path)


def test_the_run_prints_a_content_free_document(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The operator output names allocations, outcomes and field names only."""
    _api, _secrets, driver, allocations = _fleet()

    status = vault_converge.run(
        allocations,
        driver,
        vault_converge.ConvergeRequest(tmp_path / "converge.json", apply=True, limit=5),
    )

    printed = capsys.readouterr().out
    document = json.loads(printed)
    assert status == vault_converge.EXIT_CURRENT
    assert document["mode"] == "converge"
    assert document["applied"] is True
    assert document["counts"] == {"converged": 1}
    (entry,) = document["allocations"]
    assert entry["outcome"] == "converged"
    assert entry["drift"] == ["image"]
    for canary in (*_secret_canaries(), _CONSUMER, ROUTING_PUBLIC_URL, _OLD_IMAGE):
        assert canary not in printed


def test_the_run_refuses_an_unreadable_journal(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A journal that is not owner-only is refused before any provider call."""
    api, _secrets, driver, allocations = _fleet()
    journal_path = tmp_path / "converge.json"
    journal_path.write_text("{}", encoding="utf-8")
    journal_path.chmod(0o644)
    requests = len(api.requests)

    status = vault_converge.run(
        allocations,
        driver,
        vault_converge.ConvergeRequest(journal_path, rollback=True),
    )

    assert status == vault_converge.EXIT_REFUSED
    assert len(api.requests) == requests
    assert "journal" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# Edges: interruptions, unusable journals and unrestorable priors
# --------------------------------------------------------------------------- #


def test_files_that_are_not_a_list_are_a_foreign_identity() -> None:
    """A Machine whose files cannot be inspected is never converged."""
    api, _secrets, driver, allocations = _fleet()
    _machine(api, driver)["config"]["files"] = {"unexpected": "shape"}

    plan = driver.plan_vault_machine(allocations.allocations[0])

    assert plan.identity_exact is False
    assert plan.drift == ()


def test_a_machine_without_files_converges_to_its_record_files(
    tmp_path: Path,
) -> None:
    """Missing secret files are policy-owned: the record supplies them."""
    api, _secrets, driver, allocations = _fleet()
    del _machine(api, driver)["config"]["files"]

    report = _converge(allocations, driver, ConvergeJournal(tmp_path / "j.json"))

    assert _outcomes(report) == [ConvergeOutcome.CONVERGED]
    assert report.allocations[0].drift == ("files", "image")
    assert _machine(api, driver)["config"]["files"]


def test_an_unconventional_env_name_is_reported_without_naming_it() -> None:
    """Only conventional environment names are echoed into the plan."""
    api, _secrets, driver, allocations = _fleet()
    _machine(api, driver)["config"]["env"]["lower-case name"] = "x"

    plan = driver.plan_vault_machine(allocations.allocations[0])

    assert plan.drift == ("env", "image")


def test_a_provider_failure_while_planning_is_reported(tmp_path: Path) -> None:
    """A transient read failure is one allocation's retryable outcome."""
    api, _secrets, driver, allocations = _fleet()
    app_name = "creek-vault-" + allocations.allocations[0].provider_allocation_id[4:]
    api.fail_once("GET", f"/apps/{app_name}/machines")

    report = _converge(allocations, driver, ConvergeJournal(tmp_path / "j.json"))

    assert report.allocations == (
        vault_converge.AllocationReport(
            allocations.allocations[0].provider_allocation_id,
            ConvergeOutcome.FAILED_RETRYABLE,
            FailureReason.PROVIDER_UNAVAILABLE.value,
        ),
    )
    assert report.document()["allocations"] == [
        {
            "provider_allocation_id": allocations.allocations[0].provider_allocation_id,
            "outcome": "failed-retryable",
            "reason": "provider_unavailable",
        }
    ]


def test_an_unverified_converge_is_listed_by_a_dry_run(tmp_path: Path) -> None:
    """A dry run names a converge whose readiness is still unproven."""
    api, _secrets, driver, allocations = _fleet()
    machine = _machine(api, driver)
    api.fail_once("GET", f"/machines/{machine['id']}/wait")
    journal_path = tmp_path / "converge.json"
    _converge(allocations, driver, ConvergeJournal(journal_path))

    report = _converge(allocations, driver, ConvergeJournal(journal_path), apply=False)

    assert _outcomes(report) == [ConvergeOutcome.WOULD_CONVERGE]
    assert report.allocations[0].drift == ("readiness",)


def test_an_unverified_converge_of_a_replaced_machine_is_refused(
    tmp_path: Path,
) -> None:
    """A journal entry never vouches for a different Machine."""
    api, _secrets, driver, allocations = _fleet()
    machine = _machine(api, driver)
    api.fail_once("GET", f"/machines/{machine['id']}/wait")
    journal_path = tmp_path / "converge.json"
    _converge(allocations, driver, ConvergeJournal(journal_path))
    machine["id"] = "machine-replacement"

    report = _converge(allocations, driver, ConvergeJournal(journal_path))

    assert _outcomes(report) == [ConvergeOutcome.FAILED]
    assert report.allocations[0].reason == "journal"


def test_rollback_of_an_unfinished_converge_asks_for_the_converge_first(
    tmp_path: Path,
) -> None:
    """With no converged fingerprint recorded, rollback refuses to guess."""
    api, _secrets, driver, allocations = _fleet()
    machine = _machine(api, driver)
    api.fail_once("POST", f"/machines/{machine['id']}")
    journal_path = tmp_path / "converge.json"
    _converge(allocations, driver, ConvergeJournal(journal_path))
    machine["config"]["image"] = IMAGE

    report = _rollback(allocations, driver, journal_path)

    assert _outcomes(report) == [ConvergeOutcome.FAILED]
    assert report.allocations[0].reason == "journal"


def test_rollback_is_bounded_and_halts_on_a_failed_restore(tmp_path: Path) -> None:
    """The limit and the halt apply to rollback exactly as to converge."""
    api, _secrets, driver, allocations = _fleet(2)
    journal_path = tmp_path / "converge.json"
    _converge(allocations, driver, ConvergeJournal(journal_path), limit=2)
    for index in range(2):
        _machine(api, driver, index)["state"] = "stopped"
    api.fail_once("POST", f"/machines/{_machine(api, driver, 0)['id']}")

    halted = _rollback(allocations, driver, journal_path)
    bounded = vault_converge.rollback_fleet(
        allocations, driver, ConvergeJournal(journal_path), apply=True, limit=1
    )

    assert _outcomes(halted) == [ConvergeOutcome.FAILED_RETRYABLE]
    assert _outcomes(bounded) == [ConvergeOutcome.RESTORED]
    assert bounded.limit_reached is True


@pytest.mark.parametrize(
    ("_label", "files"),
    [
        ("not-a-list", "files"),
        ("extra-key", [{"guest_path": "/x", "raw_value_sha256": "0", "mode": 1}]),
        ("unknown-digest", [{"guest_path": "/x", "raw_value_sha256": "0"}]),
    ],
)
def test_a_prior_whose_files_the_record_cannot_rebuild_is_refused(
    _label: str,
    files: object,
) -> None:
    """A rollback cannot write a secret value the record does not hold."""
    api, _secrets, driver, allocations = _fleet()
    allocation = allocations.allocations[0]
    plan = driver.plan_vault_machine(allocation)
    converged = driver.converge_vault_machine(allocation, plan)
    _machine(api, driver)["state"] = "stopped"
    prior = {**plan.config, "files": files}

    with pytest.raises(ProviderError) as raised:
        driver.restore_vault_machine(
            allocation,
            prior,
            prior_fingerprint=plan.fingerprint,
            converged_fingerprint=converged.fingerprint,
        )

    assert raised.value.reason is FailureReason.PROVIDER_REJECTED
    assert len(api.updates) == 1


def test_a_converge_refuses_a_machine_changed_since_its_plan() -> None:
    """The plan the operator saw is re-checked before the update."""
    api, _secrets, driver, allocations = _fleet()
    allocation = allocations.allocations[0]
    plan = driver.plan_vault_machine(allocation)
    _machine(api, driver)["config"]["guest"]["cpus"] = 2

    with pytest.raises(ProviderError) as raised:
        driver.converge_vault_machine(allocation, plan)

    assert raised.value.retryable is True
    assert api.updates == []


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        json.dumps({"version": 1}),
        json.dumps({"version": 1, "entries": {"fly-x": "entry"}}),
    ],
)
def test_a_malformed_journal_is_refused(tmp_path: Path, content: str) -> None:
    """Only a well-formed journal of this version is trusted."""
    journal_path = tmp_path / "converge.json"
    journal_path.write_text(content, encoding="utf-8")
    journal_path.chmod(0o600)

    with pytest.raises(ValueError, match="journal"):
        ConvergeJournal(journal_path)


def test_a_rollback_without_a_journal_is_refused(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Rollback has nothing to restore from without its journal."""
    _api, _secrets, driver, allocations = _fleet()

    status = vault_converge.run(
        allocations, driver, vault_converge.ConvergeRequest(None, rollback=True)
    )

    assert status == vault_converge.EXIT_REFUSED
    assert "journal" in capsys.readouterr().out
