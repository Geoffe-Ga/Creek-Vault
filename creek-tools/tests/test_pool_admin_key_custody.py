"""Custody of the organization pool admin key (#1930, ADR-0016 Decision 3.4).

The admin key is the one credential that can create, re-cap and delete *every*
activation's vendor key, so its custody is tested rather than trusted:

* it is a **declared secret** alongside the control plane's others — one
  ``[[files]]`` entry, one Fly secret name, one bootstrap-normalized path;
* every refusal carries a **content-free closed-vocabulary code** whose exit
  code is its own, distinct from ``--check pool``'s ``pool-unconfigured`` (30);
* an unconfigured or malformed key **refuses**, never falls through to an
  unauthenticated or default path;
* the key value reaches no log record, error body, alert row or file under the
  test root — on success **and** on a simulated vendor 401 whose response body
  echoes the credential back;
* each failure mode raises its own alert kind, distinguishable from
  ``pool_credential_orphan``.

Every value here is a synthetic canary. Nothing dials a vendor, nothing is
provisioned and nothing bills (ADR-0016 Decision 4, "no spend").
"""

from __future__ import annotations

import dataclasses
import inspect
import json
import logging
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Final, get_type_hints

import httpx
import pytest

from creek_mcp.container_health import _EXIT_CODES as _PROBE_EXIT_CODES
from creek_mcp.container_health import ProbeStatus
from creek_mcp.provisioning import pilot_bootstrap, pool_admin_key
from creek_mcp.provisioning.budget import AlertKind
from creek_mcp.provisioning.fly_pilot_config import render_fly_toml
from creek_mcp.provisioning.pilot_alerts import HttpFleetAlertSink
from creek_mcp.provisioning.pool_admin_key import (
    PoolAdminKeyError,
    PoolAdminKeyStatus,
    classify_admin_response,
    load_pool_admin_key,
    verify_pool_admin_key,
)
from creek_mcp.provisioning.pool_bundle import (
    POOL_ADMIN_KEY_EXPIRES_AT_ENV,
    POOL_ADMIN_KEY_GUEST_PATH,
    POOL_ADMIN_KEY_SECRET_NAME,
)
from tests.test_fly_pilot_deploy import _TEMPLATE, _coordinates

if TYPE_CHECKING:
    from collections.abc import Mapping

# An obvious placeholder. It is well-formed on purpose: a canary that the
# format check rejected would never reach the paths this module sweeps.
_CANARY: Final[str] = "pool-admin-key-do-not-use-secret-canary"

_NOW: Final[datetime] = datetime(2026, 10, 21, 12, tzinfo=UTC)
_DISTANT: Final[str] = (_NOW + timedelta(days=90)).isoformat()
_ALERT_URL: Final[str] = "https://adepthood.example/internal/vault-provisioning/alerts"

_REFUSALS: Final[tuple[PoolAdminKeyStatus, ...]] = (
    PoolAdminKeyStatus.MISSING,
    PoolAdminKeyStatus.MALFORMED,
    PoolAdminKeyStatus.REJECTED,
    PoolAdminKeyStatus.EXPIRY_UNDECLARED,
    PoolAdminKeyStatus.EXPIRED,
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


def _environ(expires_at: str = _DISTANT) -> dict[str, str]:
    """Return the declared, non-secret admin-key environment.

    Args:
        expires_at: The declared expiry, as the Machine env carries it.

    Returns:
        The environment mapping.
    """
    return {POOL_ADMIN_KEY_EXPIRES_AT_ENV: expires_at}


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


# --------------------------------------------------------------------------
# The storage half: a declared secret alongside the control plane's others.
# --------------------------------------------------------------------------


def test_the_admin_key_is_declared_everywhere_the_other_secrets_are() -> None:
    """One Fly secret name, one guest path, one bootstrap-normalized file."""
    from creek_mcp.provisioning.fly_pilot_config import _SECRET_NAMES

    assert POOL_ADMIN_KEY_SECRET_NAME in _SECRET_NAMES
    assert POOL_ADMIN_KEY_SECRET_NAME in pilot_bootstrap._FLY_SECRET_NAMES
    assert Path(POOL_ADMIN_KEY_GUEST_PATH) in pilot_bootstrap._SECRET_FILES


def test_the_rendered_config_mounts_the_admin_key_and_declares_its_expiry() -> None:
    """The renderer gives the credential a declared home, not an ad hoc one."""
    document = tomllib.loads(render_fly_toml(_TEMPLATE, _coordinates()))

    files = {item["guest_path"]: item["secret_name"] for item in document["files"]}
    assert files[POOL_ADMIN_KEY_GUEST_PATH] == POOL_ADMIN_KEY_SECRET_NAME
    assert document["env"][POOL_ADMIN_KEY_EXPIRES_AT_ENV]
    assert _CANARY not in render_fly_toml(_TEMPLATE, _coordinates())


# --------------------------------------------------------------------------
# The verification half: fail closed with a content-free code.
# --------------------------------------------------------------------------


def test_a_well_formed_key_with_a_distant_expiry_verifies_ready(
    tmp_path: Path,
) -> None:
    """The one passing shape: present, owner-only, well-formed, in date."""
    check = verify_pool_admin_key(
        _admin_key_file(tmp_path), environ=_environ(), now=_NOW
    )

    assert check.status is PoolAdminKeyStatus.READY
    assert check.exit_code == 0
    assert check.refused is False
    assert check.alert() is None


def test_a_missing_key_refuses_with_a_code_that_is_not_pool_unconfigured(
    tmp_path: Path,
) -> None:
    """An absent credential refuses; it never reads as "no pool configured"."""
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

    assert check.status is PoolAdminKeyStatus.EXPIRY_UNDECLARED
    assert check.refused is True


def test_an_absent_expiry_declaration_refuses(tmp_path: Path) -> None:
    """No declaration at all is the same refusal as an unusable one."""
    check = verify_pool_admin_key(_admin_key_file(tmp_path), environ={}, now=_NOW)

    assert check.status is PoolAdminKeyStatus.EXPIRY_UNDECLARED


def test_an_expired_declaration_refuses(tmp_path: Path) -> None:
    """An expired admin key cannot reset a cap; refusing is the fail-closed act."""
    expired = (_NOW - timedelta(seconds=1)).isoformat()

    check = verify_pool_admin_key(
        _admin_key_file(tmp_path), environ=_environ(expired), now=_NOW
    )

    assert check.status is PoolAdminKeyStatus.EXPIRED
    assert check.refused is True


def test_a_key_inside_the_renewal_window_alerts_without_refusing(
    tmp_path: Path,
) -> None:
    """Near expiry the key still works, so it alerts rather than refusing."""
    soon = (_NOW + timedelta(days=3)).isoformat()

    check = verify_pool_admin_key(
        _admin_key_file(tmp_path), environ=_environ(soon), now=_NOW
    )

    assert check.status is PoolAdminKeyStatus.NEAR_EXPIRY
    assert check.refused is False
    alert = check.alert()
    assert alert is not None
    assert alert.kind is AlertKind.POOL_ADMIN_KEY_NEAR_EXPIRY


def test_every_status_carries_its_own_exit_code_and_none_collides_with_a_probe(
    tmp_path: Path,
) -> None:
    """Codes are a closed vocabulary the operator can act on unambiguously."""
    del tmp_path
    codes = {status: status.exit_code for status in PoolAdminKeyStatus}

    assert len(set(codes.values())) == len(codes)
    assert codes[PoolAdminKeyStatus.READY] == 0
    assert set(codes.values()) & set(_PROBE_EXIT_CODES.values()) == {0}


# --------------------------------------------------------------------------
# The loader fails closed; it never returns a default.
# --------------------------------------------------------------------------


def test_the_loader_returns_the_value_only_for_a_custodied_file(
    tmp_path: Path,
) -> None:
    """The one way to the value, and the only shape it accepts."""
    assert load_pool_admin_key(_admin_key_file(tmp_path)) == _CANARY


@pytest.mark.parametrize(
    ("factory", "status"),
    [
        (lambda root: root / "absent", PoolAdminKeyStatus.MISSING),
        (lambda root: _admin_key_file(root, "short"), PoolAdminKeyStatus.MALFORMED),
    ],
)
def test_the_loader_refuses_rather_than_falling_through_to_a_default(
    tmp_path: Path,
    factory: object,
    status: PoolAdminKeyStatus,
) -> None:
    """No unconfigured path yields an empty, placeholder or ambient credential."""
    assert callable(factory)
    path = factory(tmp_path)

    with pytest.raises(PoolAdminKeyError) as caught:
        load_pool_admin_key(path)

    assert caught.value.status is status
    assert str(caught.value) == status.value
    assert caught.value.exit_code == status.exit_code


# --------------------------------------------------------------------------
# The vendor 401: classified from the status line, never from the body.
# --------------------------------------------------------------------------


def test_a_vendor_401_classifies_from_the_status_code_alone() -> None:
    """``_pool_status_error``'s "no body is read" discipline, for the admin key."""
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


# --------------------------------------------------------------------------
# The canary sweep: success and the hostile 401.
# --------------------------------------------------------------------------


def _sweep_files(root: Path, *, secret_file: Path) -> list[str]:
    """Return every file under *root* except the custodied secret itself.

    Args:
        root: The test root to sweep.
        secret_file: The one file allowed to hold the canary.

    Returns:
        The decoded contents of every other file.
    """
    return [
        path.read_bytes().decode("utf-8", "replace")
        for path in root.rglob("*")
        if path.is_file() and path != secret_file
    ]


def test_the_canary_reaches_no_log_error_alert_row_or_file(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC1: on success and on a vendor 401 that echoes the key back."""
    caplog.set_level(logging.DEBUG)
    secret_file = _admin_key_file(tmp_path)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(204)

    sink = HttpFleetAlertSink(
        _ALERT_URL,
        _bearer(tmp_path),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    ready = verify_pool_admin_key(secret_file, environ=_environ(), now=_NOW)
    # A hostile vendor echoing the credential into its own 401 body. The
    # classifier is handed the status line only, so the body has nowhere to go.
    hostile = httpx.Response(401, text=f'{{"error":"invalid key {_CANARY}"}}')
    rejected = classify_admin_response(hostile.status_code)
    assert rejected is not None
    alert = rejected.alert()
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
        repr(rejected),
        repr(alert),
        json.dumps(dataclasses.asdict(alert), sort_keys=True, default=str),
        *(request.content.decode() for request in requests),
        *_sweep_files(tmp_path, secret_file=secret_file),
    ]
    for haystack in haystacks:
        assert _CANARY not in haystack

    assert requests
    assert json.loads(requests[-1].content) == {
        "schema": "creek_fleet_alert_v1",
        "counts": {AlertKind.POOL_ADMIN_KEY_REJECTED.value: 1},
    }


def test_a_refusal_error_carries_only_its_closed_vocabulary_code(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A malformed key's traceback must not become the leak the file was not."""
    caplog.set_level(logging.DEBUG)
    bad = _CANARY + "\nsecond-line-pool-admin-key-canary"
    path = _admin_key_file(tmp_path, bad)

    with pytest.raises(PoolAdminKeyError) as caught:
        load_pool_admin_key(path)

    for haystack in (str(caught.value), repr(caught.value), caplog.text):
        assert _CANARY not in haystack


# --------------------------------------------------------------------------
# The alert half: one kind per failure mode, all distinct from the orphan.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("status", _REFUSALS)
def test_every_refusal_fires_its_own_closed_alert_kind(
    tmp_path: Path,
    status: PoolAdminKeyStatus,
) -> None:
    """AC5: hermetic, no vendor call, one delivered count per failure mode."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(204)

    sink = HttpFleetAlertSink(
        _ALERT_URL,
        _bearer(tmp_path),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    alert = pool_admin_key.PoolAdminKeyCheck(status).alert()
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
    """No failure mode is silently unalerted, and READY raises nothing."""
    for status in PoolAdminKeyStatus:
        alert = pool_admin_key.PoolAdminKeyCheck(status).alert()
        if status is PoolAdminKeyStatus.READY:
            assert alert is None
        else:
            assert alert is not None
            assert alert.kind in set(AlertKind)


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
# Startup: the control plane refuses to come up without a custodied key.
# --------------------------------------------------------------------------


def test_bootstrap_refuses_the_admin_key_as_an_environment_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The admin key is a file, exactly like the control plane's other secrets."""
    monkeypatch.setenv(POOL_ADMIN_KEY_SECRET_NAME, "synthetic-secret")
    monkeypatch.setattr(pilot_bootstrap, "prepare_runtime_paths", lambda: None)

    with pytest.raises(SystemExit, match="secret values"):
        pilot_bootstrap.main()


def test_bootstrap_refuses_startup_with_a_content_free_code(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An unusable admin key stops the control plane before it serves."""
    started: list[object] = []
    monkeypatch.setattr(pilot_bootstrap.sys, "argv", ["pilot"])
    monkeypatch.setattr(pilot_bootstrap, "prepare_runtime_paths", lambda: None)
    monkeypatch.setattr(
        pilot_bootstrap,
        "drop_privileges_and_run",
        lambda argv: started.append(argv),
    )
    monkeypatch.setattr(
        pilot_bootstrap,
        "verify_pool_admin_key",
        lambda *_args, **_kwargs: pool_admin_key.PoolAdminKeyCheck(
            PoolAdminKeyStatus.MISSING
        ),
    )

    with pytest.raises(SystemExit) as caught:
        pilot_bootstrap.main()

    assert caught.value.code == PoolAdminKeyStatus.MISSING.exit_code
    assert started == []
    assert capsys.readouterr().err == (
        f"creek-fly-pilot: pool admin key: {PoolAdminKeyStatus.MISSING.value}\n"
    )


def test_bootstrap_starts_and_reports_the_ready_code(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The outcome is legible either way: one closed code on stderr."""
    started: list[object] = []
    monkeypatch.setattr(pilot_bootstrap.sys, "argv", ["pilot", "--flag"])
    monkeypatch.setattr(pilot_bootstrap, "prepare_runtime_paths", lambda: None)
    monkeypatch.setattr(
        pilot_bootstrap,
        "drop_privileges_and_run",
        lambda argv: started.append(argv),
    )
    monkeypatch.setattr(
        pilot_bootstrap,
        "verify_pool_admin_key",
        lambda *_args, **_kwargs: pool_admin_key.PoolAdminKeyCheck(
            PoolAdminKeyStatus.READY
        ),
    )

    pilot_bootstrap.main()

    assert started == [["--flag"]]
    assert capsys.readouterr().err == (
        f"creek-fly-pilot: pool admin key: {PoolAdminKeyStatus.READY.value}\n"
    )


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
