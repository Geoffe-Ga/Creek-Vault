"""Custody of the organization pool admin key (#1930, ADR-0016 Decision 3.4).

The admin key is the one credential that can create, re-cap and delete *every*
activation's vendor key, so its custody is tested rather than trusted:

* it is a **declared secret** whose registration is checked for *exact*
  equality across all four places it must appear — the rendered ``[[files]]``
  entry, the renderer's closed secret-name set, the bootstrap's normalized
  file list and its environment-form refusal — because a secret registered in
  three of four places is a latent production failure and drift protection is
  the whole point of this surface;
* every refusal carries a **content-free closed-vocabulary code** whose exit
  code is its own, distinct from ``--check pool``'s ``pool-unconfigured`` (30);
* while the key is declared in use, a missing, malformed, undated or
  **expired** key **refuses** — including through ``load_pool_admin_key``,
  which is the only path to the value — and never falls through to an
  unauthenticated or default path;
* while it is declared *not* in use, which is the default and today's only
  regime, nothing is required and nothing refuses: declaring the custody path
  must not make a pilot that has no key yet unbootable, because the only way
  to boot it then would be to invent the placeholder credential this module
  exists to refuse;
* the key value reaches no log record, error body, alert row or file under the
  test root. That sweep carries a **positive control**, so a sweep that could
  no longer detect a leak fails instead of passing vacuously.

Every value here is a synthetic canary. Nothing dials a vendor, nothing is
provisioned and nothing bills (ADR-0016 Decision 4, "no spend").
"""

from __future__ import annotations

import dataclasses
import inspect
import json
import logging
import os
import subprocess
import sys
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Final, get_type_hints

import httpx
import pytest

from creek_mcp.container_health import _EXIT_CODES as _PROBE_EXIT_CODES
from creek_mcp.container_health import ProbeStatus
from creek_mcp.container_runtime import ContainerConfigurationError
from creek_mcp.provisioning import pilot_bootstrap, pool_admin_key
from creek_mcp.provisioning.budget import AlertKind
from creek_mcp.provisioning.fly_pilot_config import _SECRET_NAMES, render_fly_toml
from creek_mcp.provisioning.pilot_alerts import HttpFleetAlertSink
from creek_mcp.provisioning.pool_admin_key import (
    POOL_ADMIN_KEY_EXPIRES_AT_ENV,
    POOL_ADMIN_KEY_GUEST_PATH,
    POOL_ADMIN_KEY_IN_USE,
    POOL_ADMIN_KEY_IN_USE_ENV,
    POOL_ADMIN_KEY_NOT_IN_USE,
    POOL_ADMIN_KEY_RENEW_WITHIN,
    POOL_ADMIN_KEY_SECRET_NAME,
    PoolAdminKeyCheck,
    PoolAdminKeyError,
    PoolAdminKeyStatus,
    classify_admin_response,
    load_pool_admin_key,
    pool_admin_key_in_use,
    pool_admin_secret_files,
    verify_pool_admin_key,
)
from tests.test_fly_pilot_deploy import _TEMPLATE, _coordinates

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from creek_mcp.provisioning.fly_pilot_config import FlyPilotCoordinates

# An obvious placeholder. It is well-formed on purpose: a canary that the
# format check rejected would never reach the paths this module sweeps.
_CANARY: Final[str] = "pool-admin-key-do-not-use-secret-canary"

_NOW: Final[datetime] = datetime(2026, 10, 21, 12, tzinfo=UTC)
_DISTANT: Final[str] = (_NOW + timedelta(days=90)).isoformat()
_ALERT_URL: Final[str] = "https://adepthood.example/internal/vault-provisioning/alerts"

_REFUSALS: Final[tuple[PoolAdminKeyStatus, ...]] = tuple(
    status for status in PoolAdminKeyStatus if status.refused
)
_USABLE: Final[tuple[PoolAdminKeyStatus, ...]] = (
    PoolAdminKeyStatus.READY,
    PoolAdminKeyStatus.NEAR_EXPIRY,
)


def _admin_key_file(tmp_path: Path, value: str = _CANARY) -> Path:
    """Write one owner-only admin-key file and return its path.

    Args:
        tmp_path: The test root.
        value: The synthetic key material to store.

    Returns:
        The file's path.
    """
    path = tmp_path / "creek_pool_admin_key"
    path.write_text(value, encoding="utf-8")
    path.chmod(0o400)
    return path


def _environ(
    expires_at: str = _DISTANT,
    *,
    in_use: str | None = POOL_ADMIN_KEY_IN_USE,
) -> dict[str, str]:
    """Return the declared, non-secret admin-key environment.

    Args:
        expires_at: The declared expiry, as the Machine env carries it.
        in_use: The declared regime, or ``None`` to omit the declaration.

    Returns:
        The environment mapping.
    """
    environ = {POOL_ADMIN_KEY_EXPIRES_AT_ENV: expires_at}
    if in_use is not None:
        environ[POOL_ADMIN_KEY_IN_USE_ENV] = in_use
    return environ


def _bearer(tmp_path: Path) -> Path:
    """Write the mounted alert bearer the closed sink reads.

    Args:
        tmp_path: The test root.

    Returns:
        The bearer file's path.
    """
    path = tmp_path / "handoff-bearer"
    path.write_text("synthetic-mounted-bearer", encoding="ascii")
    path.chmod(0o600)
    return path


def _sink(tmp_path: Path, requests: list[httpx.Request]) -> HttpFleetAlertSink:
    """Return the real closed alert sink over a recording mock transport.

    Args:
        tmp_path: The test root, for the mounted bearer.
        requests: The list each delivered request is appended to.

    Returns:
        The sink.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(204)

    return HttpFleetAlertSink(
        _ALERT_URL,
        _bearer(tmp_path),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def _in_use_coordinates() -> FlyPilotCoordinates:
    """Return coordinates in the admin-key-in-use regime.

    Returns:
        The coordinates, with a declared timezone-aware expiry.
    """
    return dataclasses.replace(
        _coordinates(), pool_admin_key_expires_at="2026-12-28T12:00:00+00:00"
    )


# --------------------------------------------------------------------------
# Storage: exact-equality registration across all four places.
# --------------------------------------------------------------------------


def test_the_environment_form_is_refused_in_every_regime() -> None:
    """A secret value in an env var is always a startup refusal, in use or not.

    Exact equality, not membership: the env-refusal list must be the closed
    secret-name set plus the admin key and nothing else, so a secret added to
    one list and forgotten in the other fails here.
    """
    assert set(pilot_bootstrap._FLY_SECRET_NAMES) == (
        set(_SECRET_NAMES) | {POOL_ADMIN_KEY_SECRET_NAME}
    )
    assert len(pilot_bootstrap._FLY_SECRET_NAMES) == len(
        set(pilot_bootstrap._FLY_SECRET_NAMES)
    )


def test_the_always_present_files_exactly_match_the_closed_secret_names() -> None:
    """The bootstrap's unconditional list is the seven, and only the seven."""
    assert POOL_ADMIN_KEY_SECRET_NAME not in _SECRET_NAMES
    assert Path(POOL_ADMIN_KEY_GUEST_PATH) not in pilot_bootstrap._SECRET_FILES
    assert len(pilot_bootstrap._SECRET_FILES) == len(_SECRET_NAMES)


@pytest.mark.parametrize("declared", [None, POOL_ADMIN_KEY_NOT_IN_USE, "", "   "])
def test_nothing_is_required_while_no_admin_key_is_declared_in_use(
    declared: str | None,
) -> None:
    """The default regime: no file to normalize, and no refusal."""
    environ = {} if declared is None else {POOL_ADMIN_KEY_IN_USE_ENV: declared}

    assert pool_admin_key_in_use(environ) is False
    assert pool_admin_secret_files(environ) == ()


def test_the_admin_key_file_is_normalized_only_while_it_is_in_use() -> None:
    """In use, the bootstrap normalizes exactly one extra declared file."""
    assert pool_admin_secret_files(_environ()) == (Path(POOL_ADMIN_KEY_GUEST_PATH),)


@pytest.mark.parametrize(
    ("coordinates", "expected_names"),
    [
        (_coordinates(), set(_SECRET_NAMES)),
        (_in_use_coordinates(), set(_SECRET_NAMES) | {POOL_ADMIN_KEY_SECRET_NAME}),
    ],
    ids=["not-in-use", "in-use"],
)
def test_the_rendered_files_exactly_match_the_regime(
    coordinates: FlyPilotCoordinates,
    expected_names: set[str],
) -> None:
    """Exact set equality in both regimes, plus guest-path agreement.

    This is the drift gate that matters: the rendered ``[[files]]`` entries,
    the renderer's expected secret-name set and the bootstrap's normalized
    path list must all name the same files, so a secret declared in one and
    forgotten in another cannot ship.
    """
    document = tomllib.loads(render_fly_toml(_TEMPLATE, coordinates))

    rendered = {item["secret_name"] for item in document["files"]}
    assert rendered == expected_names
    assert len(document["files"]) == len(expected_names)
    assert all(set(item) == {"guest_path", "secret_name"} for item in document["files"])

    environ = (
        _environ()
        if coordinates.pool_admin_key_expires_at is not None
        else {POOL_ADMIN_KEY_IN_USE_ENV: POOL_ADMIN_KEY_NOT_IN_USE}
    )
    expected_paths = set(pilot_bootstrap._SECRET_FILES) | set(
        pool_admin_secret_files(environ)
    )
    assert {Path(item["guest_path"]) for item in document["files"]} == expected_paths


def test_the_rendered_config_declares_the_regime_in_its_environment() -> None:
    """The Machine's own config says which regime it is in; nothing is inferred."""
    absent = tomllib.loads(render_fly_toml(_TEMPLATE, _coordinates()))["env"]
    present = tomllib.loads(render_fly_toml(_TEMPLATE, _in_use_coordinates()))["env"]

    assert absent[POOL_ADMIN_KEY_IN_USE_ENV] == POOL_ADMIN_KEY_NOT_IN_USE
    assert absent[POOL_ADMIN_KEY_EXPIRES_AT_ENV] == ""
    assert present[POOL_ADMIN_KEY_IN_USE_ENV] == POOL_ADMIN_KEY_IN_USE
    assert present[POOL_ADMIN_KEY_EXPIRES_AT_ENV] == (
        _in_use_coordinates().pool_admin_key_expires_at
    )


def test_no_key_material_is_rendered_in_either_regime() -> None:
    """The config names secrets; it never carries one."""
    for coordinates in (_coordinates(), _in_use_coordinates()):
        rendered = render_fly_toml(_TEMPLATE, coordinates)
        assert _CANARY not in rendered
        assert "B64=" not in rendered


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(
            lambda text: text.replace("CREEK_FLEET_POLICY_B64", "CREEK_INVENTED_B64"),
            id="renamed-secret",
        ),
        pytest.param(
            lambda text: (
                text
                + '\n[[files]]\n  guest_path = "/run/secrets/creek_extra"\n'
                + '  {{FILE_REFERENCE_KEY}} = "CREEK_EXTRA_B64"\n'
            ),
            id="extra-secret",
        ),
        pytest.param(
            lambda text: (
                text
                + '\n[[files]]\n  guest_path = "/run/secrets/creek_pool_admin_key"\n'
                + '  {{FILE_REFERENCE_KEY}} = "CREEK_POOL_ADMIN_KEY_B64"\n'
            ),
            id="admin-key-while-not-in-use",
        ),
    ],
)
def test_the_renderer_refuses_a_drifted_secret_contract(
    tmp_path: Path,
    mutate: Callable[[str], str],
) -> None:
    """The set-equality check is enforced, not decorative.

    Nothing else in the suite mutates the template, so without this the
    renderer's one real drift defence is never exercised — including the case
    that matters most here: the admin key declared while the config says no
    admin key is in use.
    """
    drifted = tmp_path / "fly.toml.template"
    drifted.write_text(mutate(_TEMPLATE.read_text(encoding="utf-8")), encoding="utf-8")

    with pytest.raises(ValueError, match="drifted from reviewed policy"):
        render_fly_toml(drifted, _coordinates())


# --------------------------------------------------------------------------
# Verification: fail closed with a content-free code.
# --------------------------------------------------------------------------


def test_a_well_formed_key_with_a_distant_expiry_verifies_ready(
    tmp_path: Path,
) -> None:
    """The one passing shape: in use, present, owner-only, well-formed, in date."""
    check = verify_pool_admin_key(
        _admin_key_file(tmp_path), environ=_environ(), now=_NOW
    )

    assert check.status is PoolAdminKeyStatus.READY
    assert check.exit_code == 0
    assert check.refused is False
    assert check.alert() is None


def test_a_key_declared_not_in_use_verifies_without_touching_the_file(
    tmp_path: Path,
) -> None:
    """Today's regime: nothing to custody, so nothing refuses and nothing alerts."""
    check = verify_pool_admin_key(
        tmp_path / "absent",
        environ={POOL_ADMIN_KEY_IN_USE_ENV: POOL_ADMIN_KEY_NOT_IN_USE},
        now=_NOW,
    )

    assert check.status is PoolAdminKeyStatus.NOT_IN_USE
    assert check.exit_code == 0
    assert check.refused is False
    assert check.alert() is None


@pytest.mark.parametrize("declared", ["2", "true", "yes", "01", "0x1", "no"])
def test_a_garbled_in_use_declaration_refuses(
    tmp_path: Path,
    declared: str,
) -> None:
    """The regime flag is a closed two-value vocabulary, not a truthiness test."""
    assert pool_admin_key_in_use({POOL_ADMIN_KEY_IN_USE_ENV: declared}) is None

    check = verify_pool_admin_key(
        _admin_key_file(tmp_path),
        environ=_environ(in_use=declared),
        now=_NOW,
    )

    assert check.status is PoolAdminKeyStatus.UNDECLARED
    assert check.refused is True


def test_a_missing_key_refuses_with_a_code_that_is_not_pool_unconfigured(
    tmp_path: Path,
) -> None:
    """An absent in-use credential refuses; it never reads as "no pool"."""
    check = verify_pool_admin_key(tmp_path / "absent", environ=_environ(), now=_NOW)

    assert check.status is PoolAdminKeyStatus.MISSING
    assert check.refused is True
    assert check.exit_code != _PROBE_EXIT_CODES[ProbeStatus.POOL_UNCONFIGURED]


@pytest.mark.parametrize(
    "value",
    [
        "   \n",
        "short",
        "pool-admin-key-secret-canary\npool-admin-key-second-line-canary",
        # A pasted PEM-shaped blob. The banner is deliberately not a real one:
        # a literal key banner here would trip detect-private-key, and
        # suppressing that hook to test a format check is not a trade worth
        # making. The rule under test is the charset and the single line.
        "-----BEGIN NOT-A-CREDENTIAL-----\nnot-a-bearer-canary\n",
        '{"key": "pool-admin-key-secret-canary"}',
        "pool admin key secret canary with spaces",
        "'pool-admin-key-secret-canary'",
        "pool-admin-key-secret-canary\x00",
    ],
)
def test_a_malformed_key_refuses_rather_than_being_presented(
    tmp_path: Path,
    value: str,
) -> None:
    """A pasted blob, a shell-quoted value or a second line is not a credential."""
    check = verify_pool_admin_key(
        _admin_key_file(tmp_path, value), environ=_environ(), now=_NOW
    )

    assert check.status is PoolAdminKeyStatus.MALFORMED
    assert check.refused is True


def test_a_group_readable_key_file_refuses(tmp_path: Path) -> None:
    """Custody includes the file mode, exactly as the other secrets do."""
    path = _admin_key_file(tmp_path)
    path.chmod(0o440)

    check = verify_pool_admin_key(path, environ=_environ(), now=_NOW)

    assert check.status is PoolAdminKeyStatus.MALFORMED
    assert check.refused is True


@pytest.mark.parametrize("declared", ["", "   ", "not-a-date", "2026-10-21T12:00:00"])
def test_an_undeclared_or_naive_expiry_refuses(
    tmp_path: Path,
    declared: str,
) -> None:
    """A key whose expiry nobody declared would expire unnoticed: refuse."""
    check = verify_pool_admin_key(
        _admin_key_file(tmp_path), environ=_environ(declared), now=_NOW
    )

    assert check.status is PoolAdminKeyStatus.UNDECLARED
    assert check.refused is True


def test_an_absent_expiry_declaration_refuses_while_in_use(tmp_path: Path) -> None:
    """No declaration at all is the same refusal as an unusable one."""
    check = verify_pool_admin_key(
        _admin_key_file(tmp_path),
        environ={POOL_ADMIN_KEY_IN_USE_ENV: POOL_ADMIN_KEY_IN_USE},
        now=_NOW,
    )

    assert check.status is PoolAdminKeyStatus.UNDECLARED


@pytest.mark.parametrize(
    ("offset", "status", "remaining"),
    [
        pytest.param(timedelta(0), PoolAdminKeyStatus.EXPIRED, None, id="exactly-now"),
        pytest.param(
            timedelta(seconds=-1), PoolAdminKeyStatus.EXPIRED, None, id="one-second-ago"
        ),
        pytest.param(
            timedelta(seconds=1),
            PoolAdminKeyStatus.NEAR_EXPIRY,
            1,
            id="one-second-left",
        ),
        pytest.param(
            POOL_ADMIN_KEY_RENEW_WITHIN,
            PoolAdminKeyStatus.NEAR_EXPIRY,
            int(POOL_ADMIN_KEY_RENEW_WITHIN.total_seconds()),
            id="exactly-the-window",
        ),
        pytest.param(
            POOL_ADMIN_KEY_RENEW_WITHIN + timedelta(seconds=1),
            PoolAdminKeyStatus.READY,
            None,
            id="one-second-outside-the-window",
        ),
    ],
)
def test_the_expiry_window_is_inclusive_at_both_of_its_edges(
    tmp_path: Path,
    offset: timedelta,
    status: PoolAdminKeyStatus,
    remaining: int | None,
) -> None:
    """The boundaries, not just the middle: ``<= 0`` expired, ``<= window`` near."""
    check = verify_pool_admin_key(
        _admin_key_file(tmp_path),
        environ=_environ((_NOW + offset).isoformat()),
        now=_NOW,
    )

    assert check.status is status
    assert check.remaining_seconds == remaining


def test_a_near_expiry_alert_carries_only_counts(tmp_path: Path) -> None:
    """The measured and threshold cells are second counts, not key material."""
    soon = (_NOW + timedelta(days=3)).isoformat()

    check = verify_pool_admin_key(
        _admin_key_file(tmp_path), environ=_environ(soon), now=_NOW
    )
    alert = check.alert()

    assert check.status is PoolAdminKeyStatus.NEAR_EXPIRY
    assert check.refused is False
    assert alert is not None
    assert alert.kind is AlertKind.POOL_ADMIN_KEY_NEAR_EXPIRY
    assert alert.subject == "fleet"
    assert alert.age_seconds is None
    assert alert.measured == str(int(timedelta(days=3).total_seconds()))
    assert alert.threshold == str(int(POOL_ADMIN_KEY_RENEW_WITHIN.total_seconds()))


def test_the_documented_renewal_window_is_the_constant_the_code_uses() -> None:
    """The 15 days the runbook quotes is this constant, not a second copy."""
    assert timedelta(days=15) == POOL_ADMIN_KEY_RENEW_WITHIN


def test_every_refusal_has_its_own_code_and_none_collides_with_a_probe() -> None:
    """Codes are a closed vocabulary the operator can act on unambiguously."""
    refusing = {status: status.exit_code for status in _REFUSALS}
    proceeding = {
        status.exit_code for status in PoolAdminKeyStatus if not status.refused
    }

    assert len(set(refusing.values())) == len(refusing)
    assert proceeding == {0}
    assert 0 not in set(refusing.values())
    assert set(refusing.values()) & set(_PROBE_EXIT_CODES.values()) == set()


# --------------------------------------------------------------------------
# The loader fails closed; it never returns a default, and never an expired key.
# --------------------------------------------------------------------------


def test_the_loader_returns_the_value_only_for_a_custodied_in_date_key(
    tmp_path: Path,
) -> None:
    """The one way to the value, and the only shapes it accepts."""
    path = _admin_key_file(tmp_path)

    assert load_pool_admin_key(path, environ=_environ(), now=_NOW) == _CANARY
    # Near expiry the key still works, so the loader must still hand it over:
    # refusing here would brick the control plane 15 days early.
    soon = (_NOW + timedelta(days=3)).isoformat()
    assert load_pool_admin_key(path, environ=_environ(soon), now=_NOW) == _CANARY


@pytest.mark.parametrize(
    ("factory", "environ", "status"),
    [
        pytest.param(
            lambda root: root / "absent",
            _environ(),
            PoolAdminKeyStatus.MISSING,
            id="missing",
        ),
        pytest.param(
            lambda root: _admin_key_file(root, "short"),
            _environ(),
            PoolAdminKeyStatus.MALFORMED,
            id="malformed",
        ),
        pytest.param(
            _admin_key_file,
            _environ((_NOW - timedelta(days=1)).isoformat()),
            PoolAdminKeyStatus.EXPIRED,
            id="expired",
        ),
        pytest.param(
            _admin_key_file,
            _environ("not-a-date"),
            PoolAdminKeyStatus.UNDECLARED,
            id="undeclared-expiry",
        ),
        pytest.param(
            _admin_key_file,
            {POOL_ADMIN_KEY_IN_USE_ENV: POOL_ADMIN_KEY_NOT_IN_USE},
            PoolAdminKeyStatus.NOT_IN_USE,
            id="not-in-use",
        ),
    ],
)
def test_the_loader_refuses_rather_than_falling_through_to_a_default(
    tmp_path: Path,
    factory: Callable[[Path], Path],
    environ: Mapping[str, str],
    status: PoolAdminKeyStatus,
) -> None:
    """No path yields an empty, placeholder, ambient or **expired** credential.

    The expired and undeclared rows are the point: a loader that enforced only
    presence and format would hand a #1896 caller a credential the vendor has
    already stopped honouring, which is a fail-open wearing a check's clothes.
    """
    with pytest.raises(PoolAdminKeyError) as caught:
        load_pool_admin_key(factory(tmp_path), environ=environ, now=_NOW)

    assert caught.value.status is status
    assert str(caught.value) == status.value
    assert caught.value.exit_code == status.exit_code


def test_the_loader_accepts_exactly_the_statuses_that_may_be_presented() -> None:
    """Enumerated, so a new status cannot default into being presentable."""
    assert set(pool_admin_key._USABLE) == set(_USABLE)
    assert set(_USABLE) & set(_REFUSALS) == set()
    assert set(_USABLE) | set(_REFUSALS) | {PoolAdminKeyStatus.NOT_IN_USE} == set(
        PoolAdminKeyStatus
    )


# --------------------------------------------------------------------------
# The vendor 401: classified from the status line, never from the body.
# --------------------------------------------------------------------------


def test_a_vendor_401_classifies_from_the_status_code_alone() -> None:
    """Signature-enforced, not swept: there is no parameter a body fits in.

    This is a structural claim, deliberately not a leak sweep. A sweep over a
    response object this function was never given would hold trivially; what
    actually excludes the body is that ``classify_admin_response`` accepts one
    ``int`` and nothing else, which is asserted here against the resolved
    annotations rather than the source text.
    """
    signature = inspect.signature(classify_admin_response)

    assert list(signature.parameters) == ["status_code"]
    assert get_type_hints(classify_admin_response)["status_code"] is int

    rejected = classify_admin_response(401)
    assert rejected is not None
    assert rejected.status is PoolAdminKeyStatus.REJECTED
    assert rejected.refused is True
    assert classify_admin_response(403) is not None
    assert classify_admin_response(200) is None
    assert classify_admin_response(500) is None
    assert classify_admin_response(429) is None


# --------------------------------------------------------------------------
# The canary sweep, with a positive control.
# --------------------------------------------------------------------------


def _sweep_files(root: Path, *, allowed: Path | None) -> list[str]:
    """Return the contents of every file under *root* except *allowed*.

    Args:
        root: The test root to sweep.
        allowed: The one file permitted to hold the canary, or ``None`` to
            sweep every file including the custodied secret.

    Returns:
        The decoded contents of each swept file.
    """
    return [
        path.read_bytes().decode("utf-8", "replace")
        for path in root.rglob("*")
        if path.is_file() and path != allowed
    ]


def test_the_sweep_can_detect_a_leak_at_all(tmp_path: Path) -> None:
    """Positive control for the sweep below.

    Without this, a sweep that silently stopped looking at anything would pass
    forever. Pointed at the custodied secret itself, the same helper must find
    the canary.
    """
    secret_file = _admin_key_file(tmp_path)

    assert any(_CANARY in text for text in _sweep_files(tmp_path, allowed=None))
    assert all(
        _CANARY not in text for text in _sweep_files(tmp_path, allowed=secret_file)
    )


def test_the_canary_reaches_no_log_error_alert_row_or_file(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC1, over every path that actually handles the value.

    The sweep covers the functions that touch the credential —
    ``verify_pool_admin_key``, which reads and format-checks it;
    ``load_pool_admin_key``, which *returns* it; the refusal path's error; and
    the whole startup sequence through ``pilot_bootstrap.main`` with the real
    verification against a real canary file — rather than objects built after
    the value was already dropped.
    """
    caplog.set_level(logging.DEBUG)
    secret_file = _admin_key_file(tmp_path)
    monkeypatch.setenv(POOL_ADMIN_KEY_IN_USE_ENV, POOL_ADMIN_KEY_IN_USE)
    monkeypatch.setenv(POOL_ADMIN_KEY_EXPIRES_AT_ENV, _DISTANT)
    monkeypatch.setattr(pilot_bootstrap, "POOL_ADMIN_KEY_GUEST_PATH", str(secret_file))
    monkeypatch.setattr(pilot_bootstrap.sys, "argv", ["pilot"])
    monkeypatch.setattr(pilot_bootstrap, "prepare_runtime_paths", lambda **_kw: None)
    monkeypatch.setattr(pilot_bootstrap, "drop_privileges_and_run", lambda _argv: None)

    ready = verify_pool_admin_key(secret_file, environ=_environ(), now=_NOW)
    loaded = load_pool_admin_key(secret_file, environ=_environ(), now=_NOW)
    expired_environ = _environ((_NOW - timedelta(days=1)).isoformat())
    with pytest.raises(PoolAdminKeyError) as refusal:
        load_pool_admin_key(secret_file, environ=expired_environ, now=_NOW)
    # The real startup sequence, with the real verification, over the real file.
    pilot_bootstrap.main()

    requests: list[httpx.Request] = []
    sink = _sink(tmp_path, requests)
    alert = PoolAdminKeyCheck(PoolAdminKeyStatus.REJECTED).alert()
    assert alert is not None
    sink.deliver([alert.kind.value])
    sink.close()

    captured = capsys.readouterr()
    haystacks = [
        caplog.text,
        captured.out,
        captured.err,
        repr(ready),
        str(ready),
        repr(refusal.value),
        str(refusal.value),
        repr(alert),
        json.dumps(dataclasses.asdict(alert), sort_keys=True, default=str),
        *(request.content.decode() for request in requests),
        *_sweep_files(tmp_path, allowed=secret_file),
    ]
    for haystack in haystacks:
        assert _CANARY not in haystack

    # The sweep is only meaningful because the value really was in play.
    assert loaded == _CANARY
    assert ready.status is PoolAdminKeyStatus.READY
    assert captured.err == (
        f"creek-fly-pilot: pool admin key: {PoolAdminKeyStatus.READY.value}\n"
    )
    assert json.loads(requests[-1].content)["counts"] == {alert.kind.value: 1}


# --------------------------------------------------------------------------
# The alert half: registered, mapped, and not yet emitted.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("status", _REFUSALS)
def test_every_refusal_maps_to_a_closed_alert_kind_the_sink_accepts(
    tmp_path: Path,
    status: PoolAdminKeyStatus,
) -> None:
    """AC5: hermetic, no vendor call, one delivered count per failure mode."""
    requests: list[httpx.Request] = []
    sink = _sink(tmp_path, requests)
    alert = PoolAdminKeyCheck(status).alert()
    assert alert is not None

    sink.deliver([alert.kind.value])
    sink.close()

    assert json.loads(requests[-1].content)["counts"] == {alert.kind.value: 1}


def test_the_admin_key_kinds_are_distinct_from_the_pool_credential_orphan() -> None:
    """AC2: the admin key is not a per-activation key; its kinds say so."""
    kinds = {
        AlertKind.POOL_ADMIN_KEY_UNUSABLE,
        AlertKind.POOL_ADMIN_KEY_REJECTED,
        AlertKind.POOL_ADMIN_KEY_NEAR_EXPIRY,
    }

    assert kinds <= set(AlertKind)
    assert AlertKind.POOL_CREDENTIAL_ORPHAN not in kinds
    assert all(kind.value.startswith("pool_admin_key_") for kind in kinds)


def test_every_status_maps_into_the_closed_alert_vocabulary() -> None:
    """No failure mode is silently unalerted, and the usable ones raise nothing."""
    for status in PoolAdminKeyStatus:
        alert = PoolAdminKeyCheck(status).alert()
        if status in {PoolAdminKeyStatus.READY, PoolAdminKeyStatus.NOT_IN_USE}:
            assert alert is None
        else:
            assert alert is not None
            assert alert.kind in set(AlertKind)


def test_no_production_path_emits_an_admin_key_alert_yet() -> None:
    """Pin the honest state of the alerting half, so the docs cannot drift.

    The kinds are registered because the custody vocabulary needs them, but
    emission belongs to #1896's fleet-report pass and additionally needs
    adepthood's receiver to admit them. The startup path deliberately prints a
    code and builds no ``Alert``; if that changes, this fails and the "recorded,
    not delivered" wording in the runbook and the module docstring must change
    with it.
    """
    source = Path(pilot_bootstrap.__file__).read_text(encoding="utf-8")

    assert ".alert()" not in source
    assert "HttpFleetAlertSink" not in source
    assert "recorded" in (pool_admin_key.__doc__ or "")
    assert "not yet emitted" in (pool_admin_key.__doc__ or "")


# --------------------------------------------------------------------------
# AC4: the docstring states why the full value is required.
# --------------------------------------------------------------------------


def test_the_module_docstring_states_why_a_hash_or_handle_cannot_serve() -> None:
    """The non-obvious part is recorded where the custody code lives."""
    docstring = pool_admin_key.__doc__
    assert docstring is not None
    lowered = docstring.lower()
    for phrase in ("update", "delete", "full key value", "hash", "truncated"):
        assert phrase in lowered, f"module docstring does not explain: {phrase}"


# --------------------------------------------------------------------------
# Startup: ordering, and the real file.
# --------------------------------------------------------------------------


def test_startup_normalizes_the_files_before_it_verifies_custody(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The order is load-bearing, so it is recorded rather than assumed.

    ``read_owner_only_file`` refuses anything not already mode 0400, so
    verifying before ``prepare_runtime_paths`` would read every Fly-injected
    file as ``MALFORMED``. Without this test, swapping the two calls passes.
    """
    calls: list[str] = []
    monkeypatch.setattr(pilot_bootstrap.sys, "argv", ["pilot"])
    monkeypatch.setattr(
        pilot_bootstrap,
        "prepare_runtime_paths",
        lambda **_kw: calls.append("prepare"),
    )
    monkeypatch.setattr(
        pilot_bootstrap,
        "verify_pool_admin_key",
        lambda *_a, **_kw: (
            calls.append("verify") or PoolAdminKeyCheck(PoolAdminKeyStatus.NOT_IN_USE)
        ),
    )
    monkeypatch.setattr(
        pilot_bootstrap, "drop_privileges_and_run", lambda _argv: calls.append("drop")
    )

    pilot_bootstrap.main()

    assert calls == ["prepare", "verify", "drop"]


def test_startup_lists_the_admin_key_for_normalization_only_when_in_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The conditional file list reaches ``prepare_runtime_paths`` as declared."""
    listed: list[tuple[Path, ...]] = []
    monkeypatch.setattr(pilot_bootstrap.sys, "argv", ["pilot"])
    monkeypatch.setattr(
        pilot_bootstrap,
        "prepare_runtime_paths",
        lambda *, secret_files: listed.append(secret_files),
    )
    monkeypatch.setattr(
        pilot_bootstrap,
        "verify_pool_admin_key",
        lambda *_a, **_kw: PoolAdminKeyCheck(PoolAdminKeyStatus.NOT_IN_USE),
    )
    monkeypatch.setattr(pilot_bootstrap, "drop_privileges_and_run", lambda _argv: None)

    monkeypatch.delenv(POOL_ADMIN_KEY_IN_USE_ENV, raising=False)
    pilot_bootstrap.main()
    monkeypatch.setenv(POOL_ADMIN_KEY_IN_USE_ENV, POOL_ADMIN_KEY_IN_USE)
    pilot_bootstrap.main()

    assert listed[0] == pilot_bootstrap._SECRET_FILES
    assert listed[1] == (
        *pilot_bootstrap._SECRET_FILES,
        Path(POOL_ADMIN_KEY_GUEST_PATH),
    )


def test_an_absent_declared_admin_key_is_refused_by_the_real_normalizer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reachable startup refusal for an absent in-use key is exit 2.

    ``prepare_runtime_paths`` runs first and ``_normalize_secret`` raises on
    any listed file it cannot open, so an absent *declared* admin key is
    refused there with the generic content-free code rather than reaching
    ``MISSING`` (40). Both are refusals; the runbook says which is which.
    """
    state = tmp_path / "state"
    state.mkdir()
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        f"41 32 0:35 / {state} rw,nosuid - ext4 /dev/vault rw\n", encoding="utf-8"
    )
    monkeypatch.setattr(pilot_bootstrap.os, "geteuid", lambda: 0)
    monkeypatch.setattr(pilot_bootstrap.os, "chown", lambda *_a, **_kw: None)

    with pytest.raises(ContainerConfigurationError, match="unavailable"):
        pilot_bootstrap.prepare_runtime_paths(
            state_root=state,
            mountinfo=mountinfo,
            secret_files=(tmp_path / "absent-admin-key",),
            uid=10001,
            gid=10001,
        )


def test_startup_refuses_a_real_malformed_admin_key_with_its_own_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``main()`` against a real file, with the real verification, not a stub.

    Only ``prepare_runtime_paths`` is stubbed — it needs root and the real
    ``/data`` mount — so the verdict here is produced by the production
    classifier reading the production path.
    """
    malformed = _admin_key_file(tmp_path, "short")
    monkeypatch.setenv(POOL_ADMIN_KEY_IN_USE_ENV, POOL_ADMIN_KEY_IN_USE)
    monkeypatch.setenv(POOL_ADMIN_KEY_EXPIRES_AT_ENV, _DISTANT)
    monkeypatch.setattr(pilot_bootstrap, "POOL_ADMIN_KEY_GUEST_PATH", str(malformed))
    monkeypatch.setattr(pilot_bootstrap.sys, "argv", ["pilot"])
    monkeypatch.setattr(pilot_bootstrap, "prepare_runtime_paths", lambda **_kw: None)
    started: list[object] = []
    monkeypatch.setattr(
        pilot_bootstrap, "drop_privileges_and_run", lambda argv: started.append(argv)
    )

    with pytest.raises(SystemExit) as caught:
        pilot_bootstrap.main()

    assert caught.value.code == PoolAdminKeyStatus.MALFORMED.exit_code
    assert started == []
    assert capsys.readouterr().err == (
        f"creek-fly-pilot: pool admin key: {PoolAdminKeyStatus.MALFORMED.value}\n"
    )


def test_startup_refuses_a_real_missing_admin_key_with_its_own_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``MISSING`` (40) is reachable through ``main()`` when the file vanishes.

    This is the window ``_normalize_secret`` does not cover: the declaration
    says in use and the file is gone by the time custody is verified.
    """
    monkeypatch.setenv(POOL_ADMIN_KEY_IN_USE_ENV, POOL_ADMIN_KEY_IN_USE)
    monkeypatch.setenv(POOL_ADMIN_KEY_EXPIRES_AT_ENV, _DISTANT)
    monkeypatch.setattr(
        pilot_bootstrap, "POOL_ADMIN_KEY_GUEST_PATH", str(tmp_path / "vanished")
    )
    monkeypatch.setattr(pilot_bootstrap.sys, "argv", ["pilot"])
    monkeypatch.setattr(pilot_bootstrap, "prepare_runtime_paths", lambda **_kw: None)
    monkeypatch.setattr(pilot_bootstrap, "drop_privileges_and_run", lambda _argv: None)

    with pytest.raises(SystemExit) as caught:
        pilot_bootstrap.main()

    assert caught.value.code == PoolAdminKeyStatus.MISSING.exit_code
    assert capsys.readouterr().err == (
        f"creek-fly-pilot: pool admin key: {PoolAdminKeyStatus.MISSING.value}\n"
    )


def test_bootstrap_refuses_the_admin_key_as_an_environment_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The admin key is a file, exactly like the control plane's other secrets."""
    monkeypatch.setenv(POOL_ADMIN_KEY_SECRET_NAME, "synthetic-secret")
    monkeypatch.setattr(pilot_bootstrap, "prepare_runtime_paths", lambda **_kw: None)

    with pytest.raises(SystemExit, match="secret values"):
        pilot_bootstrap.main()


def test_the_verification_environment_is_injected_not_ambient(
    tmp_path: Path,
) -> None:
    """The check reads a passed mapping, so a test never mutates the process."""
    signature = inspect.signature(verify_pool_admin_key)

    assert set(signature.parameters) == {"path", "environ", "now"}
    environ: Mapping[str, str] = _environ()
    assert (
        verify_pool_admin_key(
            _admin_key_file(tmp_path), environ=environ, now=_NOW
        ).status
        is PoolAdminKeyStatus.READY
    )


# --------------------------------------------------------------------------
# The root import surface, pinned per regime (#1930 review).
# --------------------------------------------------------------------------

_IMPORT_PROBE: Final[str] = """
import json, os, sys
from datetime import UTC, datetime
from pathlib import Path

import creek_mcp.container_runtime  # noqa: F401  the bootstrap's own baseline

before = set(sys.modules)
from creek_mcp.provisioning import pilot_bootstrap

pilot_bootstrap.verify_pool_admin_key(
    Path(os.environ["PROBE_KEY_PATH"]), environ=os.environ, now=datetime.now(tz=UTC)
)
print(json.dumps(sorted(set(sys.modules) - before)))
"""
"""Measure what reaching the custody check adds *on top of* the bootstrap's
own module-scope baseline, in a fresh interpreter.

A subprocess, because ``sys.modules`` in this one is already saturated by the
test suite's own imports, which would make any in-process measurement read
zero and the gate vacuous.
"""


def _import_delta(
    tmp_path: Path,
    *,
    in_use: str,
    monkeypatch: pytest.MonkeyPatch,
) -> set[str]:
    """Return the modules the custody check adds in one regime.

    Args:
        tmp_path: The test root, for the probe's key path.
        in_use: The regime declaration to run under.
        monkeypatch: Used only to build the child environment.

    Returns:
        The module names imported after ``container_runtime``.
    """
    del monkeypatch
    probe = tmp_path / "import_probe.py"
    probe.write_text(_IMPORT_PROBE, encoding="utf-8")
    environment = {
        **os.environ,
        POOL_ADMIN_KEY_IN_USE_ENV: in_use,
        POOL_ADMIN_KEY_EXPIRES_AT_ENV: _DISTANT,
        "PROBE_KEY_PATH": str(tmp_path / "absent-admin-key"),
    }
    completed = subprocess.run(
        [sys.executable, str(probe)],
        capture_output=True,
        check=True,
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        text=True,
    )
    return set(json.loads(completed.stdout))


def test_the_not_in_use_regime_adds_nothing_to_the_root_import_surface(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default regime pays for the two new modules and nothing else.

    ``pool_admin_key`` imports nothing first-party at module scope precisely
    so that a control plane with no admin key does not drag the x509/AEAD
    surface in as root. This is that claim, measured.
    """
    added = _import_delta(
        tmp_path, in_use=POOL_ADMIN_KEY_NOT_IN_USE, monkeypatch=monkeypatch
    )

    assert added == {
        "creek_mcp.provisioning.pilot_bootstrap",
        "creek_mcp.provisioning.pool_admin_key",
    }


def test_the_in_use_regime_does_pay_for_the_secret_reader_as_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The honest other half: in use, the deferral does not cancel the cost.

    ``_stored_status`` imports ``production_secrets`` lazily, but it runs
    before the privilege drop, so with a key declared in use the bootstrap
    loads the x509/AEAD surface as root after all. The module docstring and
    the PR say exactly this; pinning it stops the claim drifting back into
    "adds nothing", and stops the cost growing unnoticed.
    """
    added = _import_delta(
        tmp_path, in_use=POOL_ADMIN_KEY_IN_USE, monkeypatch=monkeypatch
    )

    assert "creek_mcp.provisioning.production_secrets" in added
    assert any(name.startswith("cryptography.x509") for name in added)
    assert "creek_mcp.provisioning.pool_admin_key" in added
    # The claim is "bounded in the default regime only", so the docstring has
    # to keep saying so.
    assert "not-in-use regime" in (pool_admin_key.__doc__ or "")


def test_the_unrendered_template_is_not_a_deployable_config() -> None:
    """Why ``secret_name`` is assembled rather than written literally.

    With ``{{FILE_REFERENCE_KEY}}`` as a bare key the template is not valid
    TOML, so it cannot be mistaken for a config or consumed un-rendered. The
    appended admin-key block keeps the same idiom, and this pins the property
    that makes the idiom worth keeping.
    """
    with pytest.raises(tomllib.TOMLDecodeError):
        tomllib.loads(_TEMPLATE.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# Startup, end to end, for the paths that had no main()-level coverage.
# --------------------------------------------------------------------------


def _stub_startup(
    monkeypatch: pytest.MonkeyPatch,
    *,
    key_path: Path,
) -> list[object]:
    """Stub only what needs root, and return the privilege-drop recorder.

    Args:
        monkeypatch: The patcher.
        key_path: The admin-key path startup should verify.

    Returns:
        The list the privilege drop appends to, so a refusal can be shown to
        have happened before it.
    """
    started: list[object] = []
    monkeypatch.setattr(pilot_bootstrap, "POOL_ADMIN_KEY_GUEST_PATH", str(key_path))
    monkeypatch.setattr(pilot_bootstrap.sys, "argv", ["pilot"])
    monkeypatch.setattr(pilot_bootstrap, "prepare_runtime_paths", lambda **_kw: None)
    monkeypatch.setattr(
        pilot_bootstrap, "drop_privileges_and_run", lambda argv: started.append(argv)
    )
    return started


def test_startup_refuses_a_garbled_regime_declaration_end_to_end(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The garbled flag refuses through ``main()``, and normalizes no file.

    The one refusal that had no end-to-end cover. It also must not be mistaken
    for the in-use regime by the file list: a garbled declaration normalizes
    nothing, so the refusal comes from the custody code's own code rather than
    from ``_normalize_secret``'s generic exit 2.
    """
    started = _stub_startup(monkeypatch, key_path=_admin_key_file(tmp_path))
    monkeypatch.setenv(POOL_ADMIN_KEY_IN_USE_ENV, "yes")
    monkeypatch.setenv(POOL_ADMIN_KEY_EXPIRES_AT_ENV, _DISTANT)

    assert pool_admin_secret_files({POOL_ADMIN_KEY_IN_USE_ENV: "yes"}) == ()

    with pytest.raises(SystemExit) as caught:
        pilot_bootstrap.main()

    assert caught.value.code == PoolAdminKeyStatus.UNDECLARED.exit_code
    assert started == []
    assert capsys.readouterr().err == (
        f"creek-fly-pilot: pool admin key: {PoolAdminKeyStatus.UNDECLARED.value}\n"
    )


def test_startup_refuses_an_expired_admin_key_end_to_end(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An expired declared key stops the control plane before it serves."""
    started = _stub_startup(monkeypatch, key_path=_admin_key_file(tmp_path))
    monkeypatch.setenv(POOL_ADMIN_KEY_IN_USE_ENV, POOL_ADMIN_KEY_IN_USE)
    monkeypatch.setenv(
        POOL_ADMIN_KEY_EXPIRES_AT_ENV, (datetime.now(tz=UTC) - timedelta(1)).isoformat()
    )

    with pytest.raises(SystemExit) as caught:
        pilot_bootstrap.main()

    assert caught.value.code == PoolAdminKeyStatus.EXPIRED.exit_code
    assert started == []
    assert capsys.readouterr().err == (
        f"creek-fly-pilot: pool admin key: {PoolAdminKeyStatus.EXPIRED.value}\n"
    )


@pytest.mark.parametrize(
    "in_use", [POOL_ADMIN_KEY_NOT_IN_USE, POOL_ADMIN_KEY_IN_USE], ids=["off", "on"]
)
def test_the_environment_form_is_refused_behaviourally_in_both_regimes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    in_use: str,
) -> None:
    """Not just a name-list comparison: ``main()`` really refuses, either way.

    The env-form check sits ahead of every regime decision, so a key smuggled
    in as an environment value must be refused whether or not the config says
    an admin key is in use.
    """
    started = _stub_startup(monkeypatch, key_path=_admin_key_file(tmp_path))
    monkeypatch.setenv(POOL_ADMIN_KEY_IN_USE_ENV, in_use)
    monkeypatch.setenv(POOL_ADMIN_KEY_EXPIRES_AT_ENV, _DISTANT)
    monkeypatch.setenv(POOL_ADMIN_KEY_SECRET_NAME, "synthetic-secret")

    with pytest.raises(SystemExit, match="secret values"):
        pilot_bootstrap.main()

    assert started == []
