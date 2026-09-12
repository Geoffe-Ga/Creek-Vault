"""Operator fleet CLI: reconciliation report, repair, and emergency stop (#1769).

``creek-provisioning-fleet`` is a separate process from the API and worker.
It holds a Fly driver whose secret manager refuses to issue or revoke, so it
can inventory and stop Machines but can never provision or delete.  Every
rate, budget, duration and review threshold comes from ``--policy-file``;
org-wide discovery is an injected ``--inventory-file`` because the driver
uses no org listing endpoint.

Exit codes: 0 clean, 3 alerts present, 1 provider unavailable (or any
emergency-stop failure), 2 usage error.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final

import httpx

from creek_mcp.provisioning.budget import estimate_monthly_cost, load_policy_file
from creek_mcp.provisioning.driver import ProviderError
from creek_mcp.provisioning.fleet_schema import month_range
from creek_mcp.provisioning.fly import (
    FlyCredential,
    FlyCredentialScope,
    FlyProviderDriver,
    FlyProviderPolicy,
    RefusingSecretManager,
)
from creek_mcp.provisioning.reconcile import FleetReconciler, ReconcileUnavailableError
from creek_mcp.provisioning.store import ProvisioningStore

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from creek_mcp.provisioning.budget import PolicyFile
    from creek_mcp.provisioning.inventory import FleetInventorySource, FleetStopper
    from creek_mcp.provisioning.models import FleetJob, FleetTelemetry

_LOGGER = logging.getLogger(__name__)
_DEFAULT_API_BASE_URL: Final[str] = "https://api.machines.dev"
_EXIT_CLEAN: Final[int] = 0
_EXIT_UNAVAILABLE: Final[int] = 1
_EXIT_ALERTS: Final[int] = 3
_REPORT_COMMANDS: Final[frozenset[str]] = frozenset({"report", "reconcile"})
_INVENTORY_ERROR: Final[str] = (
    "--inventory-file must be a JSON list of app names ([a-z0-9-]) "
    "or objects with such a name"
)
_APP_NAME_RE: Final[re.Pattern[str]] = re.compile(r"[a-z0-9-]+")
_MONTH_RE: Final[re.Pattern[str]] = re.compile(r"\d{4}-(0[1-9]|1[0-2])")


@dataclass(frozen=True, slots=True)
class FleetDriverBundle:
    """The two narrow provider seams a fleet command may hold."""

    inventory: FleetInventorySource
    stopper: FleetStopper


def build_parser() -> argparse.ArgumentParser:
    """Return the fleet CLI's non-secret command-line contract."""
    parser = argparse.ArgumentParser(
        prog="creek-provisioning-fleet",
        description=(
            "Reconcile the provisioning fleet against the provider, report "
            "telemetry and budget alerts, or stop every live Machine."
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name, description in (
        ("report", "Observe and report; never repairs provider state."),
        ("reconcile", "Report and perform the two bounded repairs."),
        ("emergency-stop", "Stop every live Machine; destroys nothing."),
    ):
        command = commands.add_parser(name, help=description)
        _add_common_arguments(command)
        if name in _REPORT_COMMANDS:
            _add_report_arguments(command)
    return parser


def _add_common_arguments(command: argparse.ArgumentParser) -> None:
    """Add the store and provider arguments every command needs."""
    command.add_argument("--database", type=Path, required=True)
    command.add_argument("--fly-token-file", type=Path)
    command.add_argument("--fly-organization")
    command.add_argument("--fly-image")
    command.add_argument("--fly-api-base-url", default=_DEFAULT_API_BASE_URL)
    command.add_argument("--fly-token-expires-at")


def _add_report_arguments(command: argparse.ArgumentParser) -> None:
    """Add the policy, inventory and recording arguments of report/reconcile."""
    command.add_argument("--policy-file", type=Path, required=True)
    command.add_argument("--inventory-file", type=Path)
    command.add_argument("--record-month", metavar="YYYY-MM")
    command.add_argument("--confidential-compute-changed", action="store_true")


def _utc_now() -> datetime:
    """Return the wall clock the CLI observes with unless a test injects one."""
    return datetime.now(tz=UTC)


def compose_fly(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
    *,
    transport: httpx.BaseTransport | None = None,
) -> FleetDriverBundle:
    """Build the Fly driver from mounted, non-secret arguments plus a token file."""
    required = (
        ("--fly-token-file", args.fly_token_file),
        ("--fly-organization", args.fly_organization),
        ("--fly-image", args.fly_image),
        ("--fly-token-expires-at", args.fly_token_expires_at),
    )
    missing = [flag for flag, value in required if value is None]
    if missing:
        parser.error(f"{', '.join(missing)} required for the Fly provider")
    try:
        expires_at = datetime.fromisoformat(args.fly_token_expires_at)
    except ValueError:
        parser.error("--fly-token-expires-at must be an ISO-8601 timestamp")
    if expires_at.tzinfo is None:
        parser.error("--fly-token-expires-at must carry a timezone")
    try:
        credential = FlyCredential.from_file(
            args.fly_token_file,
            organization=args.fly_organization,
            scope=FlyCredentialScope.ORG_DEPLOY,
            expires_at=expires_at,
        )
        policy = FlyProviderPolicy(
            organization=args.fly_organization,
            image=args.fly_image,
            api_base_url=args.fly_api_base_url,
        )
    except ValueError as exc:
        parser.error(f"--fly-token-file or Fly policy is invalid: {exc}")
    client = (
        None
        if transport is None
        else httpx.Client(base_url=policy.api_base_url, transport=transport)
    )
    driver = FlyProviderDriver(policy, credential, RefusingSecretManager(), client)
    return FleetDriverBundle(inventory=driver, stopper=driver)


def _load_policy(path: Path, parser: argparse.ArgumentParser) -> PolicyFile:
    """Load the operator policy or exit 2 naming the offending key only."""
    try:
        policy_file = load_policy_file(path)
    except ValueError as exc:
        parser.error(f"--policy-file is invalid: {exc}")
    return policy_file


def _load_inventory_names(
    path: Path | None,
    parser: argparse.ArgumentParser,
) -> tuple[str, ...]:
    """Read the injected app-name list (``fly apps list --json`` or plain strings)."""
    if path is None:
        return ()
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        parser.error("--inventory-file is unreadable or not JSON")
    if not isinstance(document, list):
        parser.error(_INVENTORY_ERROR)
    names: list[str] = []
    for item in document:
        name = item.get("name", item.get("Name")) if isinstance(item, dict) else item
        if not isinstance(name, str) or _APP_NAME_RE.fullmatch(name) is None:
            parser.error(_INVENTORY_ERROR)
        names.append(name)
    return tuple(names)


def _record_month_target(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
    now: datetime,
) -> str | None:
    """Validate ``--record-month``: a ``YYYY-MM`` month that has already ended."""
    month: str | None = args.record_month
    if month is None:
        return None
    if _MONTH_RE.fullmatch(month) is None:
        parser.error("--record-month must be a calendar month written as YYYY-MM")
    _, month_end = month_range(month)
    if month_end > now:
        parser.error("--record-month must name a month that has already ended")
    return month


def main(
    argv: Sequence[str] | None = None,
    *,
    compose: Callable[
        [argparse.Namespace, argparse.ArgumentParser], FleetDriverBundle
    ] = compose_fly,
    clock: Callable[[], datetime] = _utc_now,
) -> int:
    """Run one fleet command and return its exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    store = ProvisioningStore(args.database)
    if args.command == "emergency-stop":
        return _run_emergency_stop(store, compose(args, parser).stopper)
    now = clock()
    policy_file = _load_policy(args.policy_file, parser)
    app_names = _load_inventory_names(args.inventory_file, parser)
    month = _record_month_target(args, parser, now)
    bundle = compose(args, parser)
    return _run_report(
        args, store, bundle, policy_file, app_names, now=now, month=month
    )


def _run_report(
    args: argparse.Namespace,
    store: ProvisioningStore,
    bundle: FleetDriverBundle,
    policy_file: PolicyFile,
    app_names: tuple[str, ...],
    *,
    now: datetime,
    month: str | None,
) -> int:
    """Run one pass, print the JSON report, and log each alert content-free."""
    reconciler = FleetReconciler(
        store,
        bundle.inventory,
        bundle.stopper,
        policy_file.policy,
        extra_app_names=app_names,
        usage=policy_file.usage,
    )
    try:
        report = reconciler.run_once(
            now=now,
            repair=args.command == "reconcile",
            confidential_compute_changed=args.confidential_compute_changed,
        )
    except ReconcileUnavailableError:
        print(json.dumps({"error": "provider_unavailable"}), file=sys.stderr)
        return _EXIT_UNAVAILABLE
    if month is not None:
        _record_closed_month(store, report.telemetry, policy_file, month, now)
    for alert in report.alerts:
        _LOGGER.warning(
            "fleet alert kind=%s subject=%s", alert.kind.value, alert.subject
        )
    print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    return _EXIT_ALERTS if report.alerts else _EXIT_CLEAN


def _record_closed_month(
    store: ProvisioningStore,
    telemetry: FleetTelemetry,
    policy_file: PolicyFile,
    month: str,
    now: datetime,
) -> None:
    """Record *month* from its own stored running buckets plus injected usage.

    Storage components come from the current inventory; compute comes from the
    month's sampled seconds unless ``[usage]`` injected the invoice figure.
    """
    running = store.running_seconds_by_allocation(month)
    closed = dataclasses.replace(
        telemetry,
        running_machine_seconds_by_allocation=running,
        running_machine_seconds_fleet=sum(running.values()),
    )
    estimate = estimate_monthly_cost(closed, policy_file.policy, now=now, month=month)
    store.record_budget_month(
        month,
        estimate.estimated_month,
        policy_file.policy.monthly_budget,
        policy_file.policy.currency,
        now=now,
    )


def _has_live_allocation(entry: FleetJob) -> bool:
    """Return whether *entry* still owns a provider allocation."""
    return (
        entry.provider_allocation_id is not None and entry.allocation_deleted_at is None
    )


def _run_emergency_stop(store: ProvisioningStore, stopper: FleetStopper) -> int:
    """Stop every allocation's Machine, continue past failures, destroy nothing."""
    outcomes: list[dict[str, str | None]] = []
    failed = 0
    for entry in filter(_has_live_allocation, store.list_fleet_jobs()):
        try:
            stopper.stop(entry.job.activation_id)
            outcome = "ok"
        except ProviderError as exc:
            failed += 1
            outcome = f"failed:{exc.reason.value}"
        outcomes.append(
            {
                "job_id": entry.job.job_id,
                "provider_allocation_id": entry.provider_allocation_id,
                "outcome": outcome,
            }
        )
    document = {
        "stopped": len(outcomes) - failed,
        "failed": failed,
        "outcomes": outcomes,
    }
    print(json.dumps(document, indent=2, sort_keys=True))
    return _EXIT_UNAVAILABLE if failed else _EXIT_CLEAN


if __name__ == "__main__":
    sys.exit(main())
