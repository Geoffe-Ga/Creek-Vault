"""The nightly ZAP scan must prove it got past the bearer gate before it reports.

A deep scan whose credential never reached ZAP's own traffic is answered ``401``
on every request, so no handler runs and no active rule attacks anything -- yet
the report still names the site, the passive rules still fire on the refusals,
and the SARIF still uploads. That run is indistinguishable from a clean one
everywhere except the target's own access log, which records the authenticated
consumer and the matched route template of every request. These tests pin the
reader of that log: it passes only when the named consumer was answered from
behind the gate on the probe route, and fails loudly on everything else.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from scripts.dast_scan_evidence import (
    DENIAL_STATUSES,
    EXIT_CLEAN,
    EXIT_HARNESS_ERROR,
    authenticated_routes,
    main,
    reached_handlers,
    requests_in,
)

if TYPE_CHECKING:
    from pathlib import Path

_CONSUMER = "dast"
_PROBE = "/v1/wheel"


def _line(method: str, route: str, consumer: str, status: int) -> str:
    """Render one access line the way the app's access logger does."""
    return (
        f"INFO creek_mcp.httpapi.access {method} {route} "
        f"consumer={consumer} status={status} 1.234ms"
    )


def _log(tmp_path: Path, *lines: str) -> Path:
    """Write a log file holding *lines* among unrelated startup noise."""
    path = tmp_path / "api.log"
    noise = [
        "INFO:     Started server process [1]",
        "Traceback (most recent call last):",
    ]
    path.write_text("\n".join([*noise, *lines, ""]), encoding="utf-8")
    return path


def _run(log: Path) -> int:
    """Run the command against *log* for the standard consumer and probe."""
    return main(["--log", str(log), "--consumer", _CONSUMER, "--probe-path", _PROBE])


def test_access_lines_are_parsed_and_noise_is_skipped() -> None:
    """Banners and tracebacks are not requests and must not be guessed at."""
    text = "\n".join(
        [
            "INFO:     Uvicorn running on http://127.0.0.1:8823",
            _line("GET", "/v1/wheel", "dast", 200),
            _line("PUT", "/v1/journal-entries/{external_id}", "-", 401),
        ]
    )

    assert requests_in(text) == [
        ("GET", "/v1/wheel", "dast", 200),
        ("PUT", "/v1/journal-entries/{external_id}", "-", 401),
    ]


def test_the_denials_are_exactly_the_bearer_gate_refusals() -> None:
    """Every other status, a 500 included, means a handler ran."""
    assert frozenset({401, 403}) == DENIAL_STATUSES


def test_an_authenticated_answer_on_the_probe_route_is_evidence() -> None:
    """The one shape that proves the credential travelled with ZAP's requests."""
    requests = [("GET", _PROBE, _CONSUMER, 200)]

    assert reached_handlers(requests, _CONSUMER, _PROBE)


def test_a_server_error_behind_the_gate_still_counts_as_reached() -> None:
    """A 500 is a finding, and it can only happen once a handler ran."""
    assert reached_handlers([("GET", _PROBE, _CONSUMER, 500)], _CONSUMER, _PROBE)


def test_an_anonymous_success_is_not_evidence() -> None:
    """``/v1/health`` answers anybody; only the named consumer proves the token."""
    requests = [("GET", _PROBE, "-", 200)]

    assert not reached_handlers(requests, _CONSUMER, _PROBE)


def test_a_denied_request_from_the_consumer_is_not_evidence() -> None:
    """A 403 from a known consumer is still the door, not a handler."""
    requests = [("GET", _PROBE, _CONSUMER, 403)]

    assert not reached_handlers(requests, _CONSUMER, _PROBE)


def test_success_on_another_route_does_not_stand_in_for_the_probe() -> None:
    """The probe is the route the token was proven on; any other is a guess."""
    requests = [("GET", "/v1/capabilities", _CONSUMER, 200)]

    assert not reached_handlers(requests, _CONSUMER, _PROBE)


def test_authenticated_routes_counts_distinct_templates_only() -> None:
    """Coverage is measured in routes reached, not in requests sent."""
    requests = [
        ("GET", "/v1/wheel", _CONSUMER, 200),
        ("GET", "/v1/wheel", _CONSUMER, 422),
        ("PUT", "/v1/voice-drafts/{external_id}", _CONSUMER, 400),
        ("GET", "/v1/export", _CONSUMER, 401),
        ("GET", "/v1/health", "-", 200),
        ("GET", "-", _CONSUMER, 404),
    ]

    assert authenticated_routes(requests, _CONSUMER) == {
        ("GET", "/v1/wheel"),
        ("PUT", "/v1/voice-drafts/{external_id}"),
    }


def test_the_command_passes_and_summarises_when_the_gate_was_crossed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Stdout is the step summary, so the summary is the whole of stdout."""
    log = _log(
        tmp_path,
        _line("GET", _PROBE, _CONSUMER, 200),
        _line("POST", "/v1/links", _CONSUMER, 422),
        _line("GET", _PROBE, "-", 401),
    )

    assert _run(log) == EXIT_CLEAN
    out = capsys.readouterr().out
    assert "3 requests" in out
    assert "2 authenticated route(s)" in out


def test_the_command_fails_when_every_probe_was_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The silent-green this whole step exists to catch."""
    log = _log(tmp_path, _line("GET", _PROBE, "-", 401), _line("GET", _PROBE, "-", 401))

    assert _run(log) == EXIT_HARNESS_ERROR
    captured = capsys.readouterr()
    assert "never got past the bearer gate" in captured.err
    assert captured.out == ""


def test_the_command_fails_on_a_log_with_no_access_lines(tmp_path: Path) -> None:
    """Access logging switched off reads the same as a scan that reached nothing."""
    assert _run(_log(tmp_path)) == EXIT_HARNESS_ERROR


def test_a_missing_log_is_a_harness_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """No log means the target never ran, which is not a clean scan either."""
    absent = tmp_path / "absent.log"

    assert _run(absent) == EXIT_HARNESS_ERROR
    assert "absent.log" in capsys.readouterr().err


@pytest.mark.parametrize("missing", ["--log", "--consumer", "--probe-path"])
def test_every_argument_is_required(missing: str, tmp_path: Path) -> None:
    """A defaulted probe or consumer is how a check proves the wrong thing."""
    argv = {
        "--log": str(tmp_path / "api.log"),
        "--consumer": _CONSUMER,
        "--probe-path": _PROBE,
    }
    del argv[missing]
    flat = [part for pair in argv.items() for part in pair]

    with pytest.raises(SystemExit):
        main(flat)
