"""The Fly org token's operator-recorded expiry warning (#1867, R2-2).

The pilot's scheduled report raises ``provider_credential_expiring`` within a
configurable window (72 hours by default) of the recorded expiry, says
"expired" once it has passed, and treats a missing expiry as a configuration
gap rather than as fine.  Only the non-secret token ID and expiry are ever
rendered; the token itself is never read here.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

import httpx
import pytest

from creek_mcp.provisioning import fleet_cli, pilot_cli
from creek_mcp.provisioning.budget import FLEET_SUBJECT, AlertKind
from creek_mcp.provisioning.credential_expiry import (
    PROVIDER_CREDENTIAL_WARN_WITHIN,
    ProviderCredentialStatus,
    check_provider_credential,
    parse_declared_expiry,
    validate_token_id,
)
from creek_mcp.provisioning.driver import FakeProviderDriver
from creek_mcp.provisioning.fleet_cli import FleetDriverBundle, main
from creek_mcp.provisioning.pilot_alerts import HttpFleetAlertSink
from tests.fly_support import IMAGE, ORGANIZATION, PROVIDER_TOKEN

if TYPE_CHECKING:
    import argparse
    from collections.abc import Callable
    from pathlib import Path

_NOW: Final[datetime] = datetime(2026, 10, 31, 12, tzinfo=UTC)
_TOKEN_ID: Final[str] = "creek-pilot-2026-10-31"
_KIND: Final[str] = "provider_credential_expiring"
_TOKEN_CANARY: Final[str] = (
    "FlyV1 fm2_lJPECAAAAAAAAMI9xBDcredential+canary/must,never=appear0123456789"
)
_POLICY_TOML: Final[str] = """
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


def test_the_new_kind_joins_the_closed_vocabulary_with_its_wire_value() -> None:
    """ADR-0013's amended alert list carries exactly this wire string."""
    assert AlertKind.PROVIDER_CREDENTIAL_EXPIRING.value == _KIND
    assert timedelta(hours=72) == PROVIDER_CREDENTIAL_WARN_WITHIN


@pytest.mark.parametrize(
    ("remaining", "status"),
    [
        (timedelta(hours=72, seconds=1), ProviderCredentialStatus.READY),
        (timedelta(hours=72), ProviderCredentialStatus.EXPIRING),
        (timedelta(hours=72, seconds=-1), ProviderCredentialStatus.EXPIRING),
        (timedelta(hours=71), ProviderCredentialStatus.EXPIRING),
        (timedelta(seconds=1), ProviderCredentialStatus.EXPIRING),
        (timedelta(0), ProviderCredentialStatus.EXPIRED),
        (timedelta(seconds=-1), ProviderCredentialStatus.EXPIRED),
    ],
)
def test_the_default_window_is_72_hours_with_inclusive_boundaries(
    remaining: timedelta,
    status: ProviderCredentialStatus,
) -> None:
    """72 h +1 s is quiet, 72 h and 72 h -1 s warn, and zero is expired."""
    check = check_provider_credential(_TOKEN_ID, _NOW + remaining, now=_NOW)

    assert check.status is status
    alert = check.alert()
    if status is ProviderCredentialStatus.READY:
        assert alert is None
    else:
        assert alert is not None
        assert alert.kind is AlertKind.PROVIDER_CREDENTIAL_EXPIRING
        assert alert.subject == FLEET_SUBJECT


def test_the_window_is_configurable() -> None:
    """A wider window warns earlier; a narrower one stays quiet longer."""
    expires_at = _NOW + timedelta(hours=100)

    wide = check_provider_credential(
        _TOKEN_ID, expires_at, now=_NOW, warn_within=timedelta(hours=100)
    )
    narrow = check_provider_credential(
        _TOKEN_ID, expires_at, now=_NOW, warn_within=timedelta(hours=99, seconds=3599)
    )

    assert wide.status is ProviderCredentialStatus.EXPIRING
    assert narrow.status is ProviderCredentialStatus.READY
    alert = wide.alert()
    assert alert is not None
    assert alert.threshold == str(100 * 3600)


def test_the_alert_body_names_only_the_token_id_and_expiry() -> None:
    """Expiring, expired and unrecorded each carry their own wording."""
    expires_at = _NOW + timedelta(hours=10)
    expiring = check_provider_credential(_TOKEN_ID, expires_at, now=_NOW).alert()
    expired = check_provider_credential(
        _TOKEN_ID, _NOW - timedelta(hours=1), now=_NOW
    ).alert()

    assert expiring is not None
    assert expiring.measured == f"token {_TOKEN_ID} expires at {expires_at.isoformat()}"
    assert expiring.age_seconds is None
    assert expiring.threshold == str(72 * 3600)
    assert expired is not None
    assert expired.kind is AlertKind.PROVIDER_CREDENTIAL_EXPIRING
    assert expired.measured is not None
    assert "expired at" in expired.measured
    assert _TOKEN_ID in expired.measured


def test_a_missing_expiry_is_a_configuration_gap_never_fine() -> None:
    """No recorded expiry refuses and alerts; it is never READY."""
    check = check_provider_credential(_TOKEN_ID, None, now=_NOW)

    assert check.status is ProviderCredentialStatus.UNRECORDED
    assert check.refused
    alert = check.alert()
    assert alert is not None
    assert alert.kind is AlertKind.PROVIDER_CREDENTIAL_EXPIRING
    assert alert.measured == f"token {_TOKEN_ID} has no recorded expiry"


def test_an_unrecorded_token_id_is_named_as_such() -> None:
    """The ID is optional configuration; its absence is said, not invented."""
    alert = check_provider_credential(None, _NOW + timedelta(hours=1), now=_NOW).alert()

    assert alert is not None
    assert alert.measured is not None
    assert alert.measured.startswith("token (ID unrecorded) expires at ")


def test_only_expired_and_unrecorded_refuse() -> None:
    """A token that still works never stops the report."""
    assert not check_provider_credential(
        _TOKEN_ID, _NOW + timedelta(days=30), now=_NOW
    ).refused
    assert not check_provider_credential(
        _TOKEN_ID, _NOW + timedelta(hours=1), now=_NOW
    ).refused
    assert check_provider_credential(
        _TOKEN_ID, _NOW - timedelta(seconds=1), now=_NOW
    ).refused


@pytest.mark.parametrize(
    "value",
    [
        _TOKEN_CANARY,
        "fm2_" + "A" * 200,
        "has space",
        "",
        "a" * 65,
        "-leading-dash",
        "with\nnewline",
    ],
)
def test_a_token_shaped_or_malformed_id_is_refused_without_echo(value: str) -> None:
    """A pasted token in the ID field is refused and never repeated back."""
    with pytest.raises(ValueError, match="token ID") as raised:
        validate_token_id(value)

    assert value not in str(raised.value) or not value


def test_declared_expiry_parsing_refuses_naive_and_garbled_values() -> None:
    """Absent is ``None``; naive or garbled is refused, never guessed."""
    assert parse_declared_expiry(None) is None
    assert parse_declared_expiry("") is None
    assert parse_declared_expiry("2026-11-03T00:00:00+00:00") == datetime(
        2026, 11, 3, tzinfo=UTC
    )
    for garbled in ("2026-11-03T00:00:00", "next tuesday"):
        with pytest.raises(ValueError, match="expiry"):
            parse_declared_expiry(garbled)


def _compose(
    driver: Any,
) -> Callable[[argparse.Namespace, argparse.ArgumentParser], FleetDriverBundle]:
    """Inject *driver* for inventory and stop."""
    return lambda args, parser: FleetDriverBundle(inventory=driver, stopper=driver)


def _report_args(
    tmp_path: Path,
    expires_at: datetime | None,
    *extra: str,
) -> list[str]:
    """Return report arguments with a token file whose content is a canary."""
    token_file = tmp_path / "fly_token"
    token_file.write_text(f"{_TOKEN_CANARY}\n", encoding="utf-8")
    token_file.chmod(0o600)
    policy = tmp_path / "policy.toml"
    policy.write_text(_POLICY_TOML, encoding="utf-8")
    arguments = [
        "report",
        "--database",
        str(tmp_path / "jobs.sqlite3"),
        "--policy-file",
        str(policy),
        "--fly-token-file",
        str(token_file),
        "--fly-token-id",
        _TOKEN_ID,
        *extra,
    ]
    if expires_at is not None:
        arguments += ["--fly-token-expires-at", expires_at.isoformat()]
    return arguments


def test_the_scheduled_report_raises_the_alert_on_every_run_inside_the_window(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Each pass re-raises until the recorded expiry changes, then goes quiet."""
    delivered: list[tuple[str, ...]] = []
    driver = FakeProviderDriver()
    soon = _NOW + timedelta(hours=71)

    with caplog.at_level(logging.INFO):
        codes = [
            main(
                _report_args(tmp_path, soon),
                compose=_compose(driver),
                clock=lambda: _NOW,
                emit_output=False,
                alert_sink=delivered.append,
            )
            for _ in range(2)
        ]
        rotated = main(
            _report_args(tmp_path, _NOW + timedelta(days=90)),
            compose=_compose(driver),
            clock=lambda: _NOW,
            emit_output=False,
            alert_sink=delivered.append,
        )

    assert codes == [3, 3]
    assert rotated == 0
    assert delivered == [(_KIND,), (_KIND,)]
    captured = capsys.readouterr()
    observed = captured.out + captured.err + caplog.text
    assert _TOKEN_ID in caplog.text
    assert soon.isoformat() in caplog.text
    assert _TOKEN_CANARY not in observed
    assert "fm2_" not in observed


def test_the_operator_report_lists_the_alert_beside_the_reconciler_s(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The private JSON report carries the token ID and expiry, nothing more."""
    soon = _NOW + timedelta(hours=1)

    code = main(
        _report_args(tmp_path, soon, "--fly-token-warning-hours", "2"),
        compose=_compose(FakeProviderDriver()),
        clock=lambda: _NOW,
    )

    out = capsys.readouterr().out
    document = json.loads(out)
    assert code == 3
    assert document["alerts"] == [
        {
            "kind": _KIND,
            "subject": FLEET_SUBJECT,
            "age_seconds": None,
            "measured": f"token {_TOKEN_ID} expires at {soon.isoformat()}",
            "threshold": str(2 * 3600),
        }
    ]
    assert _TOKEN_CANARY not in out


@pytest.mark.parametrize(
    "expires_at",
    [_NOW - timedelta(seconds=1), _NOW, None],
)
def test_an_expired_or_unrecorded_expiry_alerts_then_refuses(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    expires_at: datetime | None,
) -> None:
    """The operator hears about it before the report refuses to run on it."""
    delivered: list[tuple[str, ...]] = []
    composed: list[bool] = []

    def compose(
        args: argparse.Namespace, parser: argparse.ArgumentParser
    ) -> FleetDriverBundle:
        composed.append(True)
        driver = FakeProviderDriver()
        return FleetDriverBundle(inventory=driver, stopper=driver)

    with caplog.at_level(logging.INFO), pytest.raises(SystemExit) as raised:
        main(
            _report_args(tmp_path, expires_at),
            compose=compose,
            clock=lambda: _NOW,
            emit_output=False,
            alert_sink=delivered.append,
        )

    assert raised.value.code == 2
    assert delivered == [(_KIND,)]
    assert composed == []
    wording = "has no recorded expiry" if expires_at is None else "expired at"
    assert wording in caplog.text
    captured = capsys.readouterr()
    assert "--fly-token-expires-at" in captured.err
    assert _TOKEN_CANARY not in captured.out + captured.err + caplog.text


def test_no_fly_token_means_no_provider_credential_check(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An injected non-Fly inventory has no provider credential to date."""
    policy = tmp_path / "policy.toml"
    policy.write_text(_POLICY_TOML, encoding="utf-8")

    code = main(
        [
            "report",
            "--database",
            str(tmp_path / "jobs.sqlite3"),
            "--policy-file",
            str(policy),
        ],
        compose=_compose(FakeProviderDriver()),
        clock=lambda: _NOW,
    )

    assert code == 0
    assert json.loads(capsys.readouterr().out)["alerts"] == []


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--fly-token-id", _TOKEN_CANARY),
        ("--fly-token-expires-at", "2026-11-03T00:00:00"),
        ("--fly-token-warning-hours", "0"),
    ],
)
def test_bad_credential_configuration_is_a_usage_error_without_echo(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    flag: str,
    value: str,
) -> None:
    """A malformed ID, a naive expiry, or a zero window exits 2."""
    arguments = _report_args(tmp_path, _NOW + timedelta(days=30))
    arguments += [flag, value]

    with pytest.raises(SystemExit) as raised:
        main(arguments, compose=_compose(FakeProviderDriver()), clock=lambda: _NOW)

    assert raised.value.code == 2
    err = capsys.readouterr().err
    assert flag in err
    assert _TOKEN_CANARY not in err


def test_the_delivered_payload_is_counts_only(tmp_path: Path) -> None:
    """The alert sink accepts the new kind and sends no ID, expiry or token."""
    bearer = tmp_path / "handoff"
    bearer.write_text("h" * 48 + "\n", encoding="utf-8")
    bearer.chmod(0o600)
    bodies: list[bytes] = []

    def handle(request: httpx.Request) -> httpx.Response:
        bodies.append(request.read())
        return httpx.Response(204)

    sink = HttpFleetAlertSink(
        "https://adepthood.test/internal/vault-provisioning/alerts",
        bearer,
        client=httpx.Client(transport=httpx.MockTransport(handle)),
    )
    try:
        sink.deliver((_KIND,))
    finally:
        sink.close()

    assert [json.loads(body) for body in bodies] == [
        {"schema": "creek_fleet_alert_v1", "counts": {_KIND: 1}}
    ]
    assert _TOKEN_ID.encode() not in bodies[0]


def _pilot_arguments(tmp_path: Path) -> list[str]:
    """Return the minimal pilot arguments the fleet pass is built from."""
    return [
        "--database",
        str(tmp_path / "jobs.sqlite3"),
        "--fly-token-file",
        str(tmp_path / "fly-token"),
        "--fly-organization",
        "creek-vaults",
        "--fly-image",
        "registry.example/creek@sha256:" + "a" * 64,
        "--fly-token-expires-at",
        "2026-11-03T00:00:00+00:00",
        "--fleet-policy-file",
        str(tmp_path / "fleet.toml"),
    ]


def test_the_pilot_passes_the_token_id_and_window_to_every_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The recorded ID and a configured window reach the scheduled report."""
    monkeypatch.setenv("CREEK_PILOT_FLY_TOKEN_ID", _TOKEN_ID)
    monkeypatch.setenv("CREEK_PILOT_FLY_TOKEN_WARNING_HOURS", "96")
    args = pilot_cli.build_parser().parse_args(_pilot_arguments(tmp_path))

    fleet = fleet_cli.build_parser().parse_args(pilot_cli._fleet_arguments(args))

    assert fleet.fly_token_id == _TOKEN_ID
    assert fleet.fly_token_warning_hours == 96
    assert fleet.fly_token_expires_at == "2026-11-03T00:00:00+00:00"


def test_the_pilot_defaults_to_72_hours_and_an_unrecorded_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without configuration the window is 72 h and no ID flag is passed."""
    monkeypatch.delenv("CREEK_PILOT_FLY_TOKEN_ID", raising=False)
    monkeypatch.delenv("CREEK_PILOT_FLY_TOKEN_WARNING_HOURS", raising=False)
    args = pilot_cli.build_parser().parse_args(_pilot_arguments(tmp_path))

    arguments = pilot_cli._fleet_arguments(args)
    fleet = fleet_cli.build_parser().parse_args(arguments)

    assert "--fly-token-id" not in arguments
    assert fleet.fly_token_id is None
    assert fleet.fly_token_warning_hours == 72


@pytest.mark.parametrize(
    ("lifetime", "admitted"),
    [(timedelta(days=90), True), (timedelta(days=90, seconds=1), False)],
)
def test_the_fleet_admits_a_90_day_org_token_and_no_longer(
    tmp_path: Path,
    lifetime: timedelta,
    *,
    admitted: bool,
) -> None:
    """Owner decision R2-2: future Fly org tokens live at most 90 days."""
    token_file = tmp_path / "fly_token"
    token_file.write_text(f"{PROVIDER_TOKEN}\n", encoding="utf-8")
    token_file.chmod(0o600)
    parser = fleet_cli.build_parser()
    args = parser.parse_args(
        [
            "report",
            "--database",
            str(tmp_path / "jobs.sqlite3"),
            "--policy-file",
            "p",
            "--fly-token-file",
            str(token_file),
            "--fly-organization",
            ORGANIZATION,
            "--fly-image",
            IMAGE,
            "--fly-token-expires-at",
            (_NOW + lifetime).isoformat(),
        ]
    )

    if admitted:
        fleet_cli.compose_fly(args, parser, clock=lambda: _NOW).close()
    else:
        with pytest.raises(SystemExit) as raised:
            fleet_cli.compose_fly(args, parser, clock=lambda: _NOW)
        assert raised.value.code == 2
