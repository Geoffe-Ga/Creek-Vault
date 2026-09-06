"""The watcher must not throw away the only explanation a held lane gets.

Issue #1270. ``scripts/ralph/watch-pr.sh`` polls ``pr-ready.sh`` as
``token="$(bash "$READY" "$pr" 2>/dev/null || true)"``. The ``|| true`` is what
keeps a tooling failure from killing the watcher; the ``2>/dev/null`` protects
nothing about control flow and destroys everything the lane is told.

``pr-ready.sh`` has two operator-facing diagnostics and both are stderr-ONLY,
because neither is expressible in the single token it prints:

* the skipped-author diagnostic (#1199) — a verdict-shaped comment arrived from
  an account the allowlist does not name;
* the provenance guard (#1181) — the verdict carries the wrong
  ``<!-- creek-review pr=N -->`` marker.

Both refusals print ``awaiting-review``, which is in ``IN_FLIGHT_TOKENS``, so
the watcher sleeps. And ``awaiting-review`` is deliberately excluded from
``LONG_HOLD_TOKENS``, so it is not backed off: at the 30s/1800s defaults that is
60 polls, 60 discarded explanations, and one line of output — ``WATCH <PR>
timeout awaiting-review`` — that says only that nothing happened.

Rotate ``GEOFFE_GA_PAT`` to an account the allowlist does not name and this is
the whole fleet at once, silently. ``pr-ready.sh`` itself records the asymmetry:
the orchestrator path captures stdout only, so its log carries these lines,
while "a rotated PAT is loud in the orchestrator's log and silent in the
watcher's".

**Why not simply drop the redirect.** ``pr-ready.sh`` emits the block on EVERY
poll, so unconditional passthrough is 60 copies of the same paragraph per lane
— roughly 300 lines each, across every lane in the fleet. The signal has to
survive without the flood, so the contract is one emission per TOKEN
TRANSITION.

**Two shell landmines this module exists to catch**, both of which have bitten
this repo:

* ``[[ -s "$diag" ]] && cat "$diag" >&2`` under ``set -euo pipefail`` is an
  errexit trigger when the file is empty. A healthy lane produces no stderr, so
  that spelling kills the watcher on its first poll and converts a cosmetic fix
  into total loss of the wake signal.
  :func:`test_a_silent_pr_ready_does_not_kill_the_watcher` is the guard.
* ``grep -c`` exits 1 on zero matches, so a shell-side count would report a
  false alarm. Counting happens in Python here.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from typing import TYPE_CHECKING

import pytest

from tests.shell_command_support import RALPH_SCRIPTS_DIR, REPO_ROOT

if TYPE_CHECKING:
    from pathlib import Path

WATCH_PR = RALPH_SCRIPTS_DIR / "watch-pr.sh"

#: A stand-in for pr-ready.sh's four-line #1199 diagnostic. The assertions
#: below count the SECOND line, never the first: a timeout summary carrying
#: only ``head -n 1`` of the block must not be able to inflate the count.
_DIAG_LINES = (
    "pr-ready: a verdict-shaped comment from `mallory` was skipped",
    "pr-ready: the accepted authors are Geoffe-Ga, github-actions",
    "pr-ready: rotate the PAT back, or add that account to the allowlist",
)
_DIAG = "\n".join(_DIAG_LINES)

#: The line whose repetition distinguishes per-transition from per-poll.
_DIAG_BODY = _DIAG_LINES[1]

#: Floor on how many polls a case must have made before its count assertion
#: means anything. Deliberately loose: each poll forks bash and the stub `gh`,
#: so the exact number is machine speed, and the distinction being drawn here
#: is "once" versus "every poll" -- a floor of three separates those. It also
#: separates a live loop from the errexit death, which stops at exactly one.
_MIN_POLLS = 3


def _install_harness(tmp_path: Path) -> Path:
    """Copy ``watch-pr.sh`` next to a scripted stub sibling and a stub ``gh``.

    ``watch-pr.sh`` resolves ``pr-ready.sh`` by its own ``dirname``, so placing
    a copy beside a fake is the seam. The stub emits ``$DIAG`` on stderr on
    every call, exactly as the real script emits its refusal block on every
    poll.

    Args:
        tmp_path: pytest temporary directory.

    Returns:
        Path to the copy of ``watch-pr.sh`` under test.
    """
    ralph = tmp_path / "ralph"
    binary = tmp_path / "bin"
    ralph.mkdir()
    binary.mkdir()
    (tmp_path / "state").mkdir()

    watcher = ralph / "watch-pr.sh"
    watcher.write_bytes(WATCH_PR.read_bytes())

    stub_ready = ralph / "pr-ready.sh"
    stub_ready.write_text(
        """#!/usr/bin/env bash
set -uo pipefail
count_file="$STATE_DIR/ready-calls"
n="$(cat "$count_file" 2>/dev/null || echo 0)"
n=$((n + 1))
echo "$n" > "$count_file"
# The refusal block is printed on EVERY poll, like the real script's.
[[ -z "${DIAG:-}" ]] || printf '%s\\n' "$DIAG" >&2
IFS=',' read -ra toks <<< "${TOKENS:-pending}"
idx=$((n - 1))
[[ "$idx" -lt "${#toks[@]}" ]] || idx=$(( ${#toks[@]} - 1 ))
# `ERR` plays pr-ready.sh's tooling-error contract: exit 2, NOTHING on stdout,
# the reason on stderr. That is the poll whose explanation matters most and the
# one a token-gated emission cannot report.
[[ "${toks[$idx]}" != "ERR" ]] || exit 2
printf '%s\\n' "${toks[$idx]}"
""",
        encoding="utf-8",
    )

    stub_gh = binary / "gh"
    stub_gh.write_text(
        """#!/usr/bin/env bash
case "$*" in
  *"pr view"*"--json state"*) printf 'OPEN\\n' ;;
  *) printf '' ;;
esac
""",
        encoding="utf-8",
    )

    for script in (watcher, stub_ready, stub_gh):
        script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
    return watcher


def _run_watch(
    tmp_path: Path,
    *,
    pr: str = "1707",
    tokens: str = "awaiting-review",
    diag: str | None = _DIAG,
    interval: str = "0.1",
    timeout: str = "2",
) -> subprocess.CompletedProcess[str]:
    """Run the watcher to completion against the scripted stubs.

    Run from the repository root: a gate exercised from the wrong working
    directory has reported a real symbol as absent in this repo before, and the
    watcher derives its pidfile slug from the cwd's git remote.

    Args:
        tmp_path: pytest temporary directory holding the harness.
        pr: PR number to watch.
        tokens: Comma-separated script of ``pr-ready.sh`` answers; the last
            repeats forever.
        diag: Stderr the stub prints on every call, or ``None`` for a silent
            (healthy) ``pr-ready.sh``.
        interval: Poll interval in seconds.
        timeout: Watch timeout in seconds.

    Returns:
        The completed watcher process, stdout and stderr captured separately.
    """
    watcher = _install_harness(tmp_path)
    environment = dict(os.environ)
    environment["PATH"] = f"{tmp_path / 'bin'}{os.pathsep}{environment['PATH']}"
    environment["STATE_DIR"] = str(tmp_path / "state")
    environment["RALPH_WATCH_PIDDIR"] = str(tmp_path)
    environment["TOKENS"] = tokens
    if diag is None:
        environment.pop("DIAG", None)
    else:
        environment["DIAG"] = diag
    return subprocess.run(
        [str(watcher), pr, interval, timeout],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
        cwd=str(REPO_ROOT),
    )


def _polls(tmp_path: Path) -> int:
    """Return how many times the stub ``pr-ready.sh`` was actually called.

    Args:
        tmp_path: pytest temporary directory holding the harness.

    Returns:
        The poll count, or 0 if the watcher never polled.
    """
    counter = tmp_path / "state" / "ready-calls"
    if not counter.is_file():
        return 0
    return int(counter.read_text(encoding="utf-8").strip() or 0)


@pytest.fixture(autouse=True)
def _require_bash() -> None:
    """Skip loudly if ``bash`` is absent rather than passing silently."""
    if shutil.which("bash") is None:
        pytest.skip("bash is not installed; watch-pr.sh is a bash script")


def test_the_refusal_reason_reaches_the_watchers_stderr(tmp_path: Path) -> None:
    """The defect, executable: the only explanation is currently discarded.

    A lane held on ``awaiting-review`` because the reviewer's account is not on
    the allowlist looks byte-identical to a lane waiting for an ordinary review
    that has not been posted yet. The difference exists only on the stream the
    watcher throws away.
    """
    result = _run_watch(tmp_path)
    assert _polls(tmp_path) >= _MIN_POLLS, (
        f"the watcher only polled {_polls(tmp_path)} time(s); the harness is "
        "not exercising the poll loop, so the assertions below prove nothing"
    )
    assert _DIAG_LINES[0] in result.stderr, (
        "pr-ready.sh's refusal diagnostic never reached the watcher's stderr. "
        "watch-pr.sh discards it with `2>/dev/null`, so a rotated PAT holds "
        "every lane in the fleet on `awaiting-review` for the full 30-minute "
        f"timeout with nothing saying why (#1270). stderr: {result.stderr!r}"
    )


def test_the_reason_is_emitted_once_per_hold_not_once_per_poll(
    tmp_path: Path,
) -> None:
    """Unconditional passthrough is the other half of the bug, not the fix.

    ``pr-ready.sh`` prints the block on every poll. At 60 polls a lane that is
    ~300 lines of the same paragraph, times every lane in the fleet — which
    buries the signal it exists to surface.

    The count is taken over the diagnostic's SECOND line, so a one-line timeout
    summary carrying ``head -n 1`` of the block cannot inflate it.
    """
    result = _run_watch(tmp_path)
    polls = _polls(tmp_path)
    assert polls >= _MIN_POLLS, f"only {polls} poll(s); the loop was not exercised"
    emitted = result.stderr.count(_DIAG_BODY)
    assert emitted == 1, (
        f"the diagnostic body appears {emitted} time(s) across {polls} polls; "
        "it must be emitted once per token transition. A monitor that "
        "re-reports the same condition every interval buries the signal it "
        f"exists to surface (#1270). stderr: {result.stderr!r}"
    )


def test_re_entering_a_state_emits_the_reason_again(tmp_path: Path) -> None:
    """The key is the transition, not a fire-once latch.

    A lane that flaps ``awaiting-review`` → ``pending`` → ``awaiting-review``
    has entered a held state twice, and the second hold is new information. A
    latch would report the first and silently swallow every later one — which
    is the same silence, deferred.
    """
    result = _run_watch(tmp_path, tokens="awaiting-review,pending,awaiting-review")
    polls = _polls(tmp_path)
    assert polls >= _MIN_POLLS, f"only {polls} poll(s); the loop was not exercised"
    emitted = result.stderr.count(_DIAG_BODY)
    assert emitted == 3, (
        f"the diagnostic body appears {emitted} time(s) across three token "
        "transitions (unknown→awaiting-review→pending→awaiting-review); it "
        "must be emitted on entry into each state, so a flap away and back "
        f"re-reports (#1270). stderr: {result.stderr!r}"
    )


def test_the_timeout_line_on_stdout_is_byte_unchanged(tmp_path: Path) -> None:
    """Acceptance criterion 2: stdout is a parsed contract, not a report.

    ``scripts/ralph/test_watch_pr.sh`` asserts byte equality on this line in
    sixty places and the orchestrator greps it. The reason belongs on stderr;
    nothing about it may reach stdout.
    """
    result = _run_watch(tmp_path, pr="1708")
    assert result.stdout == "WATCH 1708 timeout awaiting-review\n", (
        "the watcher's stdout is no longer exactly the timeout token line. "
        "Sixty assertions and the orchestrator's routing both parse it, so a "
        f"diagnostic leaking onto stdout breaks them all: {result.stdout!r}"
    )
    assert result.returncode == 0, (
        f"a wait outcome exited {result.returncode}; only usage errors are non-zero"
    )


def test_a_settled_token_still_reports_only_the_token(tmp_path: Path) -> None:
    """The wake itself must stay parseable while a diagnostic is in flight.

    ``ralph-tick.md`` captures this stdout into a shell variable; a stray line
    there is a mis-routed lane, not a cosmetic problem.
    """
    result = _run_watch(tmp_path, pr="1709", tokens="ready", timeout="5")
    assert result.stdout == "WATCH 1709 ready\n", (
        f"the settle line carried extra stdout: {result.stdout!r}"
    )


def test_a_silent_pr_ready_does_not_kill_the_watcher(tmp_path: Path) -> None:
    """The errexit landmine, guarded.

    ``watch-pr.sh`` runs under ``set -euo pipefail``. Emitting the captured
    stderr as ``[[ -s "$diag" ]] && cat "$diag" >&2`` makes the whole AND-list
    exit 1 whenever the file is empty — which is every poll of every healthy
    lane. The watcher would die on its first poll and the wake would never
    arrive, turning a diagnostics improvement into total loss of the signal.
    Use ``if [[ -s … ]]; then … fi``.
    """
    result = _run_watch(tmp_path, pr="1710", diag=None)
    polls = _polls(tmp_path)
    assert polls >= _MIN_POLLS, (
        f"the watcher stopped after {polls} poll(s) against a pr-ready.sh that "
        "printed nothing on stderr. An empty diagnostic must never end the "
        "poll loop — that is every healthy lane in the fleet (#1270)"
    )
    assert result.stdout == "WATCH 1710 timeout awaiting-review\n", (
        f"the healthy lane did not reach its timeout line: {result.stdout!r}"
    )
    assert result.returncode == 0, (
        f"the watcher exited {result.returncode} on a healthy lane"
    )


def test_an_unclassifiable_poll_still_reports_its_reason(tmp_path: Path) -> None:
    """The half a token-gated emission cannot cover (#1270, #1685).

    ``pr-ready.sh`` exits 2 with EMPTY STDOUT on a tooling error — a rate-limit
    blip, a 5xx, an expired token, or (until #1685) an unresolvable repository
    on the DEFAULT invocation — and puts the reason on stderr. ``watch-pr.sh``
    swallows that non-zero by design, so ``$token`` is empty and an emission
    gated on ``[[ -n "$token" ]]`` prints nothing at all.

    MEASURED before the fix: a stub exiting 2 with a stderr reason produced
    ``WATCH 1707 timeout unknown`` on stdout and an EMPTY stderr — exactly the
    silence this module exists to close, reached by the path most likely to
    produce it.
    """
    result = _run_watch(tmp_path, pr="1712", tokens="ERR", timeout="2")
    assert result.stdout == "WATCH 1712 timeout unknown\n", (
        "an unclassifiable lane must still reach the byte-frozen timeout line: "
        f"{result.stdout!r}"
    )
    assert _DIAG_BODY in result.stderr, (
        "a poll that produced NO token discarded its stderr. That is the poll "
        "whose explanation matters most, and gating the emission on a token is "
        f"what silences it (#1270). stderr: {result.stderr!r}"
    )
    assert result.returncode == 0, (
        f"the watcher exited {result.returncode} on a transient failure"
    )


def test_the_unclassifiable_reason_is_also_emitted_once(tmp_path: Path) -> None:
    """De-duplication must survive being hoisted out of the token test.

    The whole point of emitting on TRANSITION is that pr-ready.sh reprints its
    refusal block on every poll. Moving the emission above ``[[ -n "$token" ]]``
    with no key for the token-less poll would restore the ~300-lines-per-lane
    flood this fix exists to prevent.
    """
    result = _run_watch(tmp_path, pr="1713", tokens="ERR", timeout="2", interval="0.05")
    polls = _polls(tmp_path)
    assert polls >= _MIN_POLLS, (
        f"only {polls} poll(s) ran; the de-duplication claim would be vacuous"
    )
    assert result.stderr.count(_DIAG_BODY) == 1, (
        f"the reason was emitted {result.stderr.count(_DIAG_BODY)} time(s) "
        f"across {polls} identical unclassifiable polls; it must be emitted "
        "once per state transition"
    )
