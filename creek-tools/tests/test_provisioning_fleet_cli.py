"""Operator fleet CLI: report, reconcile, and emergency stop (#1769)."""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from creek_mcp.provisioning import fleet_cli
from creek_mcp.provisioning.driver import (
    FakeOneTimeHandoff,
    FakeProviderDriver,
    ProviderError,
)
from creek_mcp.provisioning.fleet_cli import FleetDriverBundle, build_parser, main
from creek_mcp.provisioning.fly import FlyProviderDriver
from creek_mcp.provisioning.inventory import ProviderResource
from creek_mcp.provisioning.models import (
    FailureReason,
    JobState,
    ResourceClass,
    ResourceState,
)
from creek_mcp.provisioning.store import ProvisioningStore
from creek_mcp.provisioning.worker import ProvisioningWorker
from tests.fly_support import (
    IMAGE,
    ORGANIZATION,
    PROVIDER_TOKEN,
    FakeFlyAPI,
    fly_driver,
    fly_job,
)

if TYPE_CHECKING:
    import argparse
    from collections.abc import Callable
    from pathlib import Path

_NOW = datetime(2026, 9, 11, 7, tzinfo=UTC)
_CANARY = "fleet-cli-secret-canary-must-not-appear"
_POLICY_TOML = """
# policy-file-comment-canary
[budget]
currency = "USD"
monthly_budget = 100.00
volume_gb_month_rate = 0.20
stopped_rootfs_gb_month_rate = 0.10
running_hour_rate = 0.01

[policy]
max_continuous_running_seconds = 3600
stuck_deletion_seconds = 1800

[review]
activated_vaults = 10
provisioned_volumes = 20
months_over_budget = 2
"""
_REPORT_KEYS = {
    "observed_at",
    "telemetry",
    "divergences",
    "alerts",
    "estimate",
    "review_triggers",
    "inventory_mode",
}


def _policy_file(
    tmp_path: Path,
    text: str = _POLICY_TOML,
    name: str = "policy.toml",
) -> str:
    """Write one operator policy file and return its path."""
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def _compose(
    driver: Any,
) -> Callable[[argparse.Namespace, argparse.ArgumentParser], FleetDriverBundle]:
    """Return a composition seam that injects *driver* for inventory and stop."""
    return lambda args, parser: FleetDriverBundle(inventory=driver, stopper=driver)


def _activate(store: ProvisioningStore, driver: Any, activation_id: str) -> str:
    """Provision one activation to the ceremony boundary."""
    job = store.submit(activation_id, activation_id, requester_identity="a", now=_NOW)
    assert ProvisioningWorker(store, driver, FakeOneTimeHandoff()).run_once(now=_NOW)
    return job.job_id


def _fly_args(tmp_path: Path, token_mode: int = 0o600) -> list[str]:
    """Return the Fly composition arguments with a token file of *token_mode*."""
    token_file = tmp_path / "fly_token"
    token_file.write_text(f"{PROVIDER_TOKEN}\n", encoding="utf-8")
    token_file.chmod(token_mode)
    return [
        "--fly-token-file",
        str(token_file),
        "--fly-organization",
        ORGANIZATION,
        "--fly-image",
        IMAGE,
        "--fly-api-base-url",
        "https://fly.test",
        "--fly-token-expires-at",
        "2026-12-31T00:00:00+00:00",
    ]


def test_report_prints_json_exits_three_on_alerts_and_never_repairs(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """report observes and exits 3 on alerts; only reconcile repairs."""

    class CanaryDriver(FakeProviderDriver):
        """Carry a secret-looking attribute the CLI must never render."""

        canary = _CANARY

    database = tmp_path / "jobs.sqlite3"
    store = ProvisioningStore(database)
    driver = CanaryDriver()
    _activate(store, driver, "activation-cli-live")
    doomed = _activate(store, driver, "activation-cli-doomed")
    store.request_delete(doomed, "a", now=_NOW)
    claim = store.claim_next(now=_NOW)
    assert claim is not None
    store.record_failure(
        doomed,
        claim.lease_token,
        FailureReason.PROVIDER_UNAVAILABLE,
        retryable=True,
        now=_NOW,
    )
    driver.seed_resource(
        ProviderResource(
            "fake-orphan-000",
            ResourceClass.VOLUME,
            ResourceState.OTHER,
            5,
            None,
            "vol-x",
        )
    )
    driver.seed_resource(
        ProviderResource(
            "fake-orphan-000",
            ResourceClass.MACHINE,
            ResourceState.STOPPED,
            None,
            None,
            "machine-nosize",
        )
    )
    store.submit(
        "activation-cli-live-alias",
        "activation-cli-live",
        requester_identity="a",
        now=_NOW,
    )
    common = ["--database", str(database), "--policy-file", _policy_file(tmp_path)]

    with caplog.at_level(logging.INFO):
        report_code = main(["report", *common], compose=_compose(driver))
    report_out = capsys.readouterr()
    still_failed = store.get(doomed, "a")
    reconcile_code = main(
        [
            "reconcile",
            *common,
            "--record-month",
            "2026-08",
            "--confidential-compute-changed",
        ],
        compose=_compose(driver),
        clock=lambda: datetime(2026, 9, 1, 2, tzinfo=UTC),
    )
    reconcile_out = capsys.readouterr()
    requeued = store.get(doomed, "a")

    assert report_code == 3
    document = json.loads(report_out.out)
    assert set(document) == _REPORT_KEYS
    # The CLI observes on the wall clock, so the delete requested at _NOW is
    # also older than the injected stuck threshold by the time it runs.
    assert [alert["kind"] for alert in document["alerts"]] == [
        "orphan_resource",
        "orphan_resource",
        "stuck_deletion",
    ]
    assert document["telemetry"]["snapshot_bytes"] is None
    assert document["telemetry"]["machines_without_rootfs_size"] == 1
    assert document["telemetry"]["duplicate_allocation_attempts"] == 1
    assert document["estimate"]["unpriced"] == ["egress", "snapshot", "stopped_rootfs"]
    assert document["inventory_mode"] == "derived"
    assert still_failed is not None
    assert still_failed.state is JobState.FAILED
    assert "fleet alert kind=orphan_resource subject=fake-orphan-000" in caplog.text
    assert reconcile_code == 3
    reconciled = json.loads(reconcile_out.out)
    assert requeued is not None
    assert requeued.state is JobState.DELETING
    assert "confidential_compute_change" in reconciled["review_triggers"]
    assert store.months_over_budget(1) == (False,)
    assert driver.stop_count == 0
    assert driver.delete_count == 0
    rendered = report_out.out + report_out.err + reconcile_out.out + caplog.text
    assert _CANARY not in rendered
    assert "consumer_credential" not in rendered

    clean_store = tmp_path / "clean.sqlite3"
    ProvisioningStore(clean_store)
    clean_code = main(
        [
            "report",
            "--database",
            str(clean_store),
            "--policy-file",
            _policy_file(tmp_path),
        ],
        compose=_compose(FakeProviderDriver()),
    )
    clean = json.loads(capsys.readouterr().out)
    assert clean_code == 0
    assert clean["alerts"] == []
    assert clean["divergences"] == []


def test_report_exits_one_when_the_provider_inventory_is_unavailable(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A provider outage is exit 1 and never a clean report."""

    class Outage(FakeProviderDriver):
        """Fail every inventory read with a private detail."""

        def list_resources(
            self,
            activation_ids: Any,
            *,
            app_names: Any = (),
        ) -> tuple[ProviderResource, ...]:
            del activation_ids, app_names
            raise ProviderError(
                FailureReason.PROVIDER_UNAVAILABLE,
                retryable=True,
                private_detail=_CANARY,
            )

    database = tmp_path / "jobs.sqlite3"
    ProvisioningStore(database)

    code = main(
        [
            "report",
            "--database",
            str(database),
            "--policy-file",
            _policy_file(tmp_path),
        ],
        compose=_compose(Outage()),
    )
    captured = capsys.readouterr()

    assert code == 1
    assert captured.out == ""
    assert "provider_unavailable" in captured.err
    assert _CANARY not in captured.err


def test_emergency_stop_stops_live_machines_never_deletes_and_continues_past_failures(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Every live Machine is stopped, nothing is destroyed, failures do not abort."""
    api = FakeFlyAPI()
    driver = fly_driver(api)
    database = tmp_path / "jobs.sqlite3"
    store = ProvisioningStore(database)
    earlier = _NOW - timedelta(seconds=1)
    doomed = store.submit(
        "activation-stop-2", "activation-stop-2", requester_identity="a", now=earlier
    )
    assert ProvisioningWorker(store, driver, FakeOneTimeHandoff()).run_once(now=earlier)
    job_ids = [
        doomed.job_id,
        *(_activate(store, driver, f"activation-stop-{number}") for number in range(2)),
    ]
    for activation in ("activation-stop-0", "activation-stop-1"):
        driver.start(activation)
    lost_app = next(
        name
        for name, app in api.apps.items()
        if app["network"] == driver.expected_allocation_id("activation-stop-2")
    )
    del api.apps[lost_app]
    del api.machines[lost_app]
    del api.volumes[lost_app]
    volumes_before = sum(len(volumes) for volumes in api.volumes.values())
    api.requests.clear()

    code = main(
        ["emergency-stop", "--database", str(database)],
        compose=_compose(driver),
    )
    captured = capsys.readouterr()

    assert code == 1
    document = json.loads(captured.out)
    assert document["stopped"] == 2
    assert document["failed"] == 1
    assert [outcome["outcome"] for outcome in document["outcomes"]] == [
        "failed:provider_unavailable",
        "ok",
        "ok",
    ]
    ordered = [outcome["job_id"] for outcome in document["outcomes"]]
    assert ordered[0] == job_ids[0]
    assert set(ordered[1:]) == set(job_ids[1:])
    assert all(
        machine["state"] == "stopped"
        for machines in api.machines.values()
        for machine in machines
    )
    assert not any(method == "DELETE" for method, _ in api.requests)
    assert not any(
        method == "POST" and path.endswith("/start") for method, path in api.requests
    )
    assert sum(len(volumes) for volumes in api.volumes.values()) == volumes_before
    assert len(api.apps) == 2
    assert PROVIDER_TOKEN not in captured.out + captured.err


def test_cli_takes_the_fly_token_only_from_an_owner_only_file_and_rejects_bad_inputs(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Usage errors exit 2, name the offending key, and echo no file content."""
    database = str(tmp_path / "jobs.sqlite3")
    policy = _policy_file(tmp_path)

    def run(argv: list[str], **kwargs: Any) -> tuple[int, str]:
        with caplog.at_level(logging.DEBUG), pytest.raises(SystemExit) as caught:
            main(argv, **kwargs)
        assert isinstance(caught.value.code, int)
        return caught.value.code, capsys.readouterr().err + caplog.text

    code, err = run(
        [
            "report",
            "--database",
            database,
            "--policy-file",
            policy,
            *_fly_args(tmp_path, 0o644),
        ]
    )
    assert code == 2
    assert "--fly-token-file" in err
    assert PROVIDER_TOKEN not in err

    code, err = run(["report", "--database", database, "--policy-file", policy])
    assert code == 2
    assert "--fly-token-file" in err

    naive = _fly_args(tmp_path)
    naive[-1] = "2026-12-31T00:00:00"
    code, err = run(["report", "--database", database, "--policy-file", policy, *naive])
    assert code == 2
    assert "--fly-token-expires-at" in err
    garbled = _fly_args(tmp_path)
    garbled[-1] = "not-a-timestamp"
    code, err = run(
        ["report", "--database", database, "--policy-file", policy, *garbled]
    )
    assert code == 2

    missing = _policy_file(
        tmp_path,
        _POLICY_TOML.replace("monthly_budget = 100.00\n", ""),
        "missing.toml",
    )
    code, err = run(
        ["report", "--database", database, "--policy-file", missing],
        compose=_compose(FakeProviderDriver()),
    )
    assert code == 2
    assert "monthly_budget" in err
    assert "policy-file-comment-canary" not in err
    negative = _policy_file(
        tmp_path, _POLICY_TOML.replace("0.20", "-0.20"), "negative.toml"
    )
    code, err = run(
        ["report", "--database", database, "--policy-file", negative],
        compose=_compose(FakeProviderDriver()),
    )
    assert code == 2
    assert "volume_gb_month_rate" in err
    broken = _policy_file(tmp_path, "[budget\ncurrency = = ", "broken.toml")
    code, err = run(
        ["report", "--database", database, "--policy-file", broken],
        compose=_compose(FakeProviderDriver()),
    )
    assert code == 2
    assert "TOML" in err

    inventory = tmp_path / "apps.json"
    inventory.write_text(
        json.dumps({"apps": ["inventory-object-canary"]}), encoding="utf-8"
    )
    code, err = run(
        [
            "report",
            "--database",
            database,
            "--policy-file",
            policy,
            "--inventory-file",
            str(inventory),
        ],
        compose=_compose(FakeProviderDriver()),
    )
    assert code == 2
    assert "--inventory-file" in err
    assert "inventory-object-canary" not in err
    inventory.write_text("not json", encoding="utf-8")
    code, err = run(
        [
            "report",
            "--database",
            database,
            "--policy-file",
            policy,
            "--inventory-file",
            str(inventory),
        ],
        compose=_compose(FakeProviderDriver()),
    )
    assert code == 2
    assert "--inventory-file" in err
    inventory.write_text(json.dumps(["creek-vault-x/../other-app"]), encoding="utf-8")
    code, err = run(
        [
            "report",
            "--database",
            database,
            "--policy-file",
            policy,
            "--inventory-file",
            str(inventory),
        ],
        compose=_compose(FakeProviderDriver()),
    )
    assert code == 2
    assert "--inventory-file" in err
    assert "other-app" not in err

    for literal in ("nan", "inf", "-inf"):
        not_finite = _policy_file(
            tmp_path,
            _POLICY_TOML.replace(
                "monthly_budget = 100.00", f"monthly_budget = {literal}"
            ),
            f"{literal.strip('-')}.toml",
        )
        code, err = run(
            ["report", "--database", database, "--policy-file", not_finite],
            compose=_compose(FakeProviderDriver()),
        )
        assert code == 2
        assert "monthly_budget" in err

    code, err = run(
        ["report", "--database", database], compose=_compose(FakeProviderDriver())
    )
    assert code == 2
    assert "--policy-file" in err


def test_inventory_file_names_reach_the_driver_and_mark_the_report_mode(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Strings and objects with name/Name are accepted and handed to inventory."""

    class Spy(FakeProviderDriver):
        """Record the app names the CLI injects."""

        seen: tuple[str, ...] = ()

        def list_resources(
            self,
            activation_ids: Any,
            *,
            app_names: Any = (),
        ) -> tuple[ProviderResource, ...]:
            self.seen = tuple(app_names)
            return super().list_resources(activation_ids, app_names=app_names)

    database = tmp_path / "jobs.sqlite3"
    ProvisioningStore(database)
    inventory = tmp_path / "apps.json"
    inventory.write_text(
        json.dumps(["creek-vault-a", {"name": "creek-vault-b"}, {"Name": "other"}]),
        encoding="utf-8",
    )
    spy = Spy()

    code = main(
        [
            "report",
            "--database",
            str(database),
            "--policy-file",
            _policy_file(tmp_path),
            "--inventory-file",
            str(inventory),
        ],
        compose=_compose(spy),
    )
    document = json.loads(capsys.readouterr().out)

    assert code == 0
    assert spy.seen == ("creek-vault-a", "creek-vault-b", "other")
    assert document["inventory_mode"] == "derived+injected"


def test_fly_composition_uses_a_refusing_secret_manager_and_fails_closed_on_provision(
    tmp_path: Path,
) -> None:
    """The production composition can inventory and stop but never provision."""
    api = FakeFlyAPI()
    parser = build_parser()
    args = parser.parse_args(
        [
            "report",
            "--database",
            str(tmp_path / "j.sqlite3"),
            "--policy-file",
            "p",
            *_fly_args(tmp_path),
        ]
    )

    bundle = fleet_cli.compose_fly(
        args, parser, transport=httpx.MockTransport(api.handle)
    )

    assert isinstance(bundle.inventory, FlyProviderDriver)
    assert bundle.stopper is bundle.inventory
    with pytest.raises(ProviderError) as raised:
        bundle.inventory.provision(fly_job())
    assert raised.value.reason is FailureReason.PROVIDER_REJECTED
    assert raised.value.retryable is False
    assert not any(method == "POST" for method, _ in api.requests)
    assert bundle.inventory.list_resources([]) == ()


def test_record_month_names_a_closed_month_and_refuses_an_open_one(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The closed month is recorded from its own buckets and injected usage."""
    database = tmp_path / "jobs.sqlite3"
    store = ProvisioningStore(database)
    driver = FakeProviderDriver()
    _activate(store, driver, "activation-month")
    pid = driver.expected_allocation_id("activation-month")
    august = datetime(2026, 8, 20, tzinfo=UTC)
    store.record_machine_state(pid, running=True, now=august)
    store.record_machine_state(pid, running=True, now=august + timedelta(hours=10))
    store.record_machine_state(pid, running=False, now=august + timedelta(hours=11))
    clock = datetime(2026, 9, 1, 2, tzinfo=UTC)
    common = ["--database", str(database), "--policy-file", _policy_file(tmp_path)]

    code = main(
        ["report", *common, "--record-month", "2026-08"],
        compose=_compose(driver),
        clock=lambda: clock,
    )
    document = json.loads(capsys.readouterr().out)
    with closing(sqlite3.connect(database)) as connection:
        rows = connection.execute(
            "SELECT month, estimated, over FROM provisioning_budget_months"
        ).fetchall()

    assert code == 0
    assert rows == [("2026-08", "1.20", 0)]
    assert document["estimate"]["running_basis"] == "month_to_date"
    assert document["observed_at"] == clock.isoformat()
    for month in ("2026-09", "2026-13", "202608", "2026-9"):
        with pytest.raises(SystemExit) as caught:
            main(
                ["report", *common, "--record-month", month],
                compose=_compose(driver),
                clock=lambda: clock,
            )
        assert caught.value.code == 2
        assert "--record-month" in capsys.readouterr().err
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM provisioning_budget_months"
        ).fetchone() == (1,)


def test_emergency_stop_needs_neither_a_policy_nor_an_inventory_file(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An operator in a hurry is never blocked by an unrelated file."""
    database = tmp_path / "jobs.sqlite3"
    store = ProvisioningStore(database)
    driver = FakeProviderDriver()
    _activate(store, driver, "activation-hurry")
    pid = driver.expected_allocation_id("activation-hurry")
    driver.set_machine_state(pid, ResourceState.RUNNING)

    code = main(
        ["emergency-stop", "--database", str(database)], compose=_compose(driver)
    )
    document = json.loads(capsys.readouterr().out)

    assert code == 0
    assert document["stopped"] == 1
    assert document["failed"] == 0
    assert [o["provider_allocation_id"] for o in document["outcomes"]] == [pid]
    assert driver.stop_count == 1
    with pytest.raises(SystemExit) as caught:
        main(["emergency-stop", "--database", str(database), "--policy-file", "x"])
    assert caught.value.code == 2
