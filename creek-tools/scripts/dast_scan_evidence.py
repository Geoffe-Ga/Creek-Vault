"""Prove the nightly scan got past the bearer gate before publishing what it found.

The deep-scan job (``.github/workflows/dast-deep.yml``) proves its bearer token
opens an authenticated route before ZAP starts. That proves the *credential*,
which is not the question. The question is whether OWASP ZAP attached the
credential to ZAP's own traffic -- and if it did not, every request in the run
was answered ``401`` before it reached a handler, no active rule attacked
anything, and the report at the end says so in no way at all.

That failure is invisible everywhere else in the job. The report still names the
scanned site. The passive rules still fire, on the 401 responses themselves. The
SARIF still validates and uploads. ``fail_action: false`` keeps ZAP quiet either
way. What comes out is a green nightly run and a Security tab with nothing in
it, which is exactly what a genuinely clean scan looks like.

The one place the difference is written down is the target's own access log
(:mod:`creek_mcp.httpapi.middleware.access_log`), which records the matched
route template, the **authenticated consumer** and the status of every request.
So this module asks one binary question of it -- was the named consumer ever
answered on the probe route with something other than a gate refusal -- and
exits :data:`EXIT_HARNESS_ERROR` when the answer is no. A run that proved
nothing must never be mistaken for a run that found nothing.

Usage (from ``creek-tools/``)::

    python -m scripts.dast_scan_evidence --log api.log \\
        --consumer dast --probe-path /v1/wheel

Stdout carries a Markdown line for the run summary and nothing else -- the
workflow appends it straight to ``$GITHUB_STEP_SUMMARY`` -- so every diagnostic
goes to stderr.

Exit codes:
    0 -- the scanner reached application code behind the credential.
    3 -- it did not, or the log could not be read.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Sequence

EXIT_CLEAN: Final[int] = 0
"""The scanner was answered from behind the credential at least once."""

EXIT_HARNESS_ERROR: Final[int] = 3
"""It never was, or the log could not be read."""

# The app's own access record, as ``middleware/access_log.py`` renders it:
#
#     GET /v1/wheel consumer=dast status=200 1.234ms
#
# preceded by whatever prefix the log handler's format adds. The route is the
# matched *template*, never the concrete path, so an attacked route still reads
# as that route however its parameters were mangled; a request refused before
# routing reads ``-``, and so does an unauthenticated consumer.
_ACCESS_LINE: Final[re.Pattern[str]] = re.compile(
    r"(?P<method>[A-Z]+) (?P<route>\S+) consumer=(?P<consumer>\S+) "
    r"status=(?P<status>\d{3})"
)

DENIAL_STATUSES: Final[frozenset[int]] = frozenset({401, 403})
"""The two answers the bearer gate gives. Anything else means a handler ran."""

_UNMATCHED: Final[str] = "-"
"""How the access log names a route the router never matched."""

Request = tuple[str, str, str, int]
"""One parsed access line: method, route template, consumer, status."""


class UnreadableLogError(Exception):
    """The target's access log is not there, or cannot be read."""


def requests_in(log_text: str) -> list[Request]:
    """Return every request the target logged.

    Args:
        log_text: The contents of the target's log.

    Returns:
        One ``(method, route, consumer, status)`` tuple per access line, in log
        order. Startup banners, tracebacks and anything else in the file are
        skipped rather than guessed at.
    """
    return [
        (match["method"], match["route"], match["consumer"], int(match["status"]))
        for match in _ACCESS_LINE.finditer(log_text)
    ]


def _behind_gate(request: Request, consumer: str) -> bool:
    """Return whether one request was answered for *consumer* past the gate.

    Args:
        request: One parsed access line.
        consumer: The consumer the scan's token authenticates as.

    Returns:
        ``True`` when the request authenticated as *consumer*, matched a route
        and was not refused by the gate.
    """
    _, route, logged_consumer, status = request
    return (
        logged_consumer == consumer
        and route != _UNMATCHED
        and status not in DENIAL_STATUSES
    )


def reached_handlers(
    requests: Sequence[Request], consumer: str, probe_path: str
) -> bool:
    """Return whether the scanner was ever answered from behind the credential.

    Args:
        requests: Every request the target logged.
        consumer: The consumer the scan's token authenticates as.
        probe_path: The route template the token was proven on before the scan.

    Returns:
        ``True`` when at least one request to *probe_path* authenticated as
        *consumer* and was answered with anything but a gate refusal. Success
        on some other route proves nothing about the probe, and an anonymous
        success proves nothing about the token.
    """
    return any(
        request[1] == probe_path and _behind_gate(request, consumer)
        for request in requests
    )


def authenticated_routes(
    requests: Sequence[Request], consumer: str
) -> set[tuple[str, str]]:
    """Return the distinct operations the scan reached as *consumer*.

    Args:
        requests: Every request the target logged.
        consumer: The consumer the scan's token authenticates as.

    Returns:
        Each ``(method, route template)`` answered past the gate at least once.
    """
    return {
        (request[0], request[1])
        for request in requests
        if _behind_gate(request, consumer)
    }


def _summarise(requests: Sequence[Request], consumer: str) -> str:
    """Return the Markdown line a reader of the Actions run list gets.

    Args:
        requests: Every request the target logged.
        consumer: The consumer the scan's token authenticates as.

    Returns:
        One line naming how much traffic the target saw and how many distinct
        operations the scan reached from behind the credential.
    """
    reached = authenticated_routes(requests, consumer)
    return (
        f"The target answered {len(requests)} requests; the scan reached "
        f"{len(reached)} authenticated route(s) as `{consumer}`."
    )


def _load(log: Path) -> str:
    """Read the target's access log.

    Args:
        log: Path the workflow told the target to write.

    Returns:
        The log's contents.

    Raises:
        UnreadableLogError: If the file is absent or unreadable. That means the
            target was never running, which is not a clean scan either.
    """
    try:
        return log.read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        raise UnreadableLogError(f"{log} could not be read: {error}") from error


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """Parse the command line.

    No argument has a default: a defaulted probe path or consumer is how a check
    ends up proving that an unauthenticated route answered, which every scan
    achieves.

    Args:
        argv: The argument vector, or ``None`` to read ``sys.argv``.

    Returns:
        The parsed arguments.
    """
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument(
        "--log", required=True, type=Path, help="the target's access log"
    )
    parser.add_argument(
        "--consumer",
        required=True,
        help="the consumer name the scan's bearer token authenticates as",
    )
    parser.add_argument(
        "--probe-path",
        required=True,
        help="a route template the token was proven on, e.g. /v1/wheel",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Read the access log and report whether the scan reached anything.

    Args:
        argv: The argument vector, or ``None`` to read ``sys.argv``.

    Returns:
        :data:`EXIT_CLEAN` when the scanner was answered from behind the
        credential on the probe route at least once; :data:`EXIT_HARNESS_ERROR`
        when it never was, or when the log could not be read.
    """
    args = _parse_args(argv)
    try:
        requests = requests_in(_load(args.log))
    except UnreadableLogError as error:
        sys.stderr.write(
            f"the scan cannot be shown to have reached anything: {error}\n"
        )
        return EXIT_HARNESS_ERROR
    summary = _summarise(requests, args.consumer)
    if not reached_handlers(requests, args.consumer, args.probe_path):
        sys.stderr.write(
            f"the scan never got past the bearer gate: {summary} No request to "
            f"{args.probe_path} was answered for `{args.consumer}` with anything "
            "but a refusal, so no handler ran and no active rule attacked "
            "anything.\n"
        )
        return EXIT_HARNESS_ERROR
    sys.stdout.write(f"{summary}\n")
    return EXIT_CLEAN


if __name__ == "__main__":
    raise SystemExit(main())
