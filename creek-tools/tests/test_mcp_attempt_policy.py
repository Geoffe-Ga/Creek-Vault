"""Unit tests for the elevated-auth failed-attempt budget (#914).

``CREEK_MCP_ELEVATED_TOKEN`` guards irreversible ``creek.purge.*`` calls.
Before #914 a wrong guess cost an attacker nothing, so the token was
brute-forceable at machine speed against a surface that answers in
microseconds. :class:`creek_mcp.attempt_policy.AttemptBudget` puts a price on
failure: after :data:`~creek_mcp.attempt_policy.MAX_FAILED_ATTEMPTS` wrong
answers the gate stops evaluating tokens at all for
:data:`~creek_mcp.attempt_policy.LOCKOUT_SECONDS`.

Three properties get most of the attention here, because they are the three
ways this defence goes wrong in practice.

* The refusal must be **non-blocking**. Sleeping is the tempting way to rate
  limit and it is the wrong one: it converts a free brute-force attempt into
  a denial of service against the server the throttle was protecting. No test
  in this file sleeps either -- the clock is a seam
  (``creek_mcp.attempt_policy._now``) that the tests pin.
* The window must **not be extended** by attempts made inside it. A budget
  that re-arms on every throttled knock locks the legitimate operator out
  permanently for as long as anyone keeps knocking, which is a worse outcome
  than the channel #914 closes.
* The token must **not be evaluated** while the window is armed. That is a
  negative property, so it is asserted on a spy's call count rather than on
  a return value.
"""

from __future__ import annotations

import ast
import inspect
import threading
import time
from typing import TYPE_CHECKING

from creek_mcp import attempt_policy, auth
from creek_mcp.attempt_policy import (
    LOCKOUT_SECONDS,
    MAX_FAILED_ATTEMPTS,
    AttemptBudget,
)
from tests.elevated_attempt_support import FakeMonotonicClock, VerifySpy

if TYPE_CHECKING:
    import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fresh_budget() -> AttemptBudget:
    """Build a budget configured exactly the way production configures it.

    Returns:
        An :class:`AttemptBudget` wired to the shipped constants, so a
        behavioural assertion here is an assertion about the real policy.
    """
    return AttemptBudget(
        max_failures=MAX_FAILED_ATTEMPTS,
        lockout_seconds=LOCKOUT_SECONDS,
    )


def _pin_clock(monkeypatch: pytest.MonkeyPatch) -> FakeMonotonicClock:
    """Replace the policy module's clock with one the test drives.

    Args:
        monkeypatch: Restores the real ``_now`` at teardown.

    Returns:
        The installed clock, already reporting its start instant.
    """
    clock = FakeMonotonicClock()
    monkeypatch.setattr(attempt_policy, "_now", clock)
    return clock


def _arm_the_lockout(budget: AttemptBudget) -> VerifySpy:
    """Spend the whole budget on wrong answers, leaving the window armed.

    Args:
        budget: The budget to exhaust.

    Returns:
        The spy that answered every one of those attempts, so a caller can
        keep watching its call count across the window.
    """
    spy = VerifySpy(result=False)
    for _ in range(MAX_FAILED_ATTEMPTS):
        assert budget.attempt(spy) is False
    assert spy.calls == MAX_FAILED_ATTEMPTS
    return spy


def _policy_source() -> str:
    """Return the on-disk source of :mod:`creek_mcp.attempt_policy`.

    Returns:
        The module's text, for the structural invariants below.
    """
    return inspect.getsource(attempt_policy)


def _callee_name(node: ast.Call) -> str | None:
    """Resolve a call's callee to a bare symbol name.

    Args:
        node: The call node to inspect.

    Returns:
        ``func.id`` for a direct call (``sleep(x)``), ``func.attr`` for a
        qualified one (``time.sleep(x)``), or ``None`` when the callee is a
        dynamic expression no static reader can name.
    """
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


# ---------------------------------------------------------------------------
# The seam and the policy numbers
# ---------------------------------------------------------------------------


def test_the_clock_seam_defaults_to_the_real_monotonic_clock() -> None:
    """Production must ship the real clock, not a pinned test double.

    ``_now`` exists so tests can drive time; the hazard of that seam is that
    it ships pinned or, subtler, pointing at :func:`time.time`. Wall-clock
    time is wrong here specifically: an NTP step backwards would silently
    extend a lockout and a step forwards would end one early, so the identity
    of the default is part of the policy rather than an implementation
    detail.
    """
    assert attempt_policy._now is time.monotonic


def test_the_documented_constants_are_the_shipped_values() -> None:
    """Five guesses, sixty seconds -- exactly the numbers #914 specifies.

    Exact values rather than a range: these two constants *are* the policy,
    and a silent drift to (say) fifty failures would leave every behavioural
    test in this file green while widening the brute-force window tenfold.
    """
    assert MAX_FAILED_ATTEMPTS == 5
    assert LOCKOUT_SECONDS == 60.0


def test_the_budget_honours_its_arguments_not_the_module_constants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``max_failures`` is a constructor argument, not a re-read of the constant.

    A class that ignored its own arguments and consulted
    :data:`MAX_FAILED_ATTEMPTS` directly would pass every other test in this
    file, because every other test configures it with exactly that value.
    Two failures out of a budget of two must be enough here.
    """
    _pin_clock(monkeypatch)
    budget = AttemptBudget(max_failures=2, lockout_seconds=5.0)
    wrong = VerifySpy(result=False)

    assert budget.attempt(wrong) is False
    assert budget.attempt(wrong) is False

    correct = VerifySpy(result=True)
    assert budget.attempt(correct) is False
    assert correct.calls == 0


# ---------------------------------------------------------------------------
# Where the lockout begins
# ---------------------------------------------------------------------------


def test_one_failure_short_of_the_limit_still_evaluates_the_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The budget spends every attempt it promised before it shuts.

    An off-by-one that locked at ``max_failures - 1`` would cost the operator
    a guess they were told they had, and would do it invisibly -- the refusal
    payload is identical either way.
    """
    _pin_clock(monkeypatch)
    budget = _fresh_budget()
    wrong = VerifySpy(result=False)

    for _ in range(MAX_FAILED_ATTEMPTS - 1):
        assert budget.attempt(wrong) is False

    assert wrong.calls == MAX_FAILED_ATTEMPTS - 1
    correct = VerifySpy(result=True)
    assert budget.attempt(correct) is True
    assert correct.calls == 1


def test_the_last_allowed_failure_arms_the_lockout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The window opens on failure number ``MAX_FAILED_ATTEMPTS``, not later.

    Asserted on the *correct* token, which is the strongest form: if a
    matching token is refused, a mismatched one provably was not evaluated
    either.
    """
    _pin_clock(monkeypatch)
    budget = _fresh_budget()
    _arm_the_lockout(budget)

    correct = VerifySpy(result=True)
    assert budget.attempt(correct) is False
    assert correct.calls == 0


def test_a_locked_budget_never_evaluates_the_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No comparison happens inside the window -- the guess costs nothing to refuse.

    This is the assertion that makes the bound a bound. A throttle that still
    ran the comparison and merely discarded the answer would leave the timing
    side channel wide open and would let a caller keep probing at full speed;
    only the call count can tell the two apart.
    """
    _pin_clock(monkeypatch)
    budget = _fresh_budget()
    wrong = _arm_the_lockout(budget)

    for _ in range(20):
        assert budget.attempt(wrong) is False

    assert wrong.calls == MAX_FAILED_ATTEMPTS


def test_a_success_zeroes_the_failure_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bound is on *consecutive* failures, so an honest typo never accrues.

    Without the reset the budget is a lifetime allowance: four typos today
    plus four next week lock the operator out mid-session with no failed
    attack anywhere in the sequence.
    """
    _pin_clock(monkeypatch)
    budget = _fresh_budget()
    wrong = VerifySpy(result=False)
    correct = VerifySpy(result=True)

    for _ in range(MAX_FAILED_ATTEMPTS - 1):
        assert budget.attempt(wrong) is False
    assert budget.attempt(correct) is True

    for _ in range(MAX_FAILED_ATTEMPTS - 1):
        assert budget.attempt(wrong) is False

    assert wrong.calls == 2 * (MAX_FAILED_ATTEMPTS - 1)
    assert budget.attempt(correct) is True
    assert correct.calls == 2


# ---------------------------------------------------------------------------
# The shape of the window
# ---------------------------------------------------------------------------


def test_attempts_made_inside_the_window_do_not_extend_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Knocking during a lockout must not restart it (the permanent-lockout bug).

    The failure mode is quiet and total: if a throttled attempt re-arms the
    window, anyone who can reach the MCP surface -- including a buggy client
    on a retry loop -- can keep the operator locked out of their own purge
    tools forever, and every individual refusal looks correct. The window is
    armed at t=1000, knocked on once per simulated second for its whole
    length, and must still open at t=1000+``LOCKOUT_SECONDS``.
    """
    clock = _pin_clock(monkeypatch)
    budget = _fresh_budget()
    armed_at = clock.now
    wrong = _arm_the_lockout(budget)

    for second in range(1, int(LOCKOUT_SECONDS)):
        clock.now = armed_at + second
        assert budget.attempt(wrong) is False

    assert wrong.calls == MAX_FAILED_ATTEMPTS
    clock.now = armed_at + LOCKOUT_SECONDS
    correct = VerifySpy(result=True)
    assert budget.attempt(correct) is True
    assert correct.calls == 1


def test_the_window_closes_exactly_at_lockout_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both edges of the window, so its length cannot drift unnoticed.

    A hair before the deadline the gate is still shut; at the deadline it is
    open. Pinning only the "still shut" side would let the window grow without
    bound, and pinning only the "open again" side would let it shrink to
    nothing.
    """
    clock = _pin_clock(monkeypatch)
    budget = _fresh_budget()
    armed_at = clock.now
    _arm_the_lockout(budget)

    clock.now = armed_at + LOCKOUT_SECONDS - 1.0
    still_shut = VerifySpy(result=True)
    assert budget.attempt(still_shut) is False
    assert still_shut.calls == 0

    clock.now = armed_at + LOCKOUT_SECONDS
    reopened = VerifySpy(result=True)
    assert budget.attempt(reopened) is True
    assert reopened.calls == 1


def test_the_budget_is_whole_again_after_the_window_expires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Expiry restores the full allowance, not a single probationary attempt.

    If the counter were left at its high-water mark, the first failure after
    the window would re-lock immediately and the gate would be effectively
    permanently shut for anyone who ever mistyped it once.
    """
    clock = _pin_clock(monkeypatch)
    budget = _fresh_budget()
    armed_at = clock.now
    _arm_the_lockout(budget)

    clock.advance(LOCKOUT_SECONDS)
    assert clock.now == armed_at + LOCKOUT_SECONDS

    second_round = VerifySpy(result=False)
    for _ in range(MAX_FAILED_ATTEMPTS - 1):
        assert budget.attempt(second_round) is False
    assert second_round.calls == MAX_FAILED_ATTEMPTS - 1

    correct = VerifySpy(result=True)
    assert budget.attempt(correct) is True
    assert correct.calls == 1


def test_a_sustained_attacker_is_held_to_the_documented_guess_rate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An hour of hammering buys 285 evaluated guesses, not 3600 (#914).

    The per-window allowance only matters because it composes into a *rate*,
    and the rate is the number an attacker actually plans against: they do
    not get five guesses, they get five guesses per window for as long as
    they care to keep knocking. Driven at one wrong guess per simulated
    second for an hour, the yield is 285 evaluated guesses -- 4.75 a minute,
    against a rate that was previously bounded only by the network. The
    shape is a consequence of the no-extension rule two tests above: each
    time a window expires the attacker spends the fresh allowance in four
    seconds and immediately arms the next one.

    The accepted cost, stated plainly. That same re-arming keeps
    ``creek.purge.*`` shut to *everyone*, the operator included, for 59 of
    every 64 seconds -- 3,315 of the hour's 3,600 seconds, about 92% of the
    attack. (Five seconds of each cycle are open, not four: the fifth guess
    is evaluated and *then* arms the window, so the arming second is an open
    one. ``docs/mcp.md`` quotes the same 92%.) Under sustained attack MCP
    purge is not merely delayed by a
    minute; it is largely unavailable, and this test is the record that the
    trade was made deliberately. It is the right trade: keying the budget
    per consumer instead would multiply the attacker's guess rate by the
    number of identities they hold, and it is the *secret* that needs rate
    limiting. The operator's recovery path is not to wait the attacker out
    but to run ``creek purge`` on the CLI, which drives
    :class:`creek.purge.engine.PurgeEngine` directly and never reaches this
    gate.

    The upper bound is the regression this test exists to catch. Delete the
    re-arm -- lock once, then never again -- and every assertion about a
    single window still passes, while the attacker collects 3,541 guesses
    out of this same hour: an unbounded gate wearing a throttle's clothes.
    """
    clock = _pin_clock(monkeypatch)
    budget = _fresh_budget()
    wrong = VerifySpy(result=False)
    started_at = clock.now
    attack_seconds = 3600

    for second in range(attack_seconds):
        clock.now = started_at + second
        assert budget.attempt(wrong) is False

    # One cycle is the time it takes to spend the whole allowance at 1 Hz
    # (MAX_FAILED_ATTEMPTS - 1 seconds, the first guess costing none) plus the
    # window that spending it arms. The hour's tail is long enough to hold one
    # more whole allowance, which is what the +1 counts.
    cycle_seconds = MAX_FAILED_ATTEMPTS - 1 + int(LOCKOUT_SECONDS)
    whole_cycles, tail_seconds = divmod(attack_seconds, cycle_seconds)
    assert tail_seconds >= MAX_FAILED_ATTEMPTS
    expected_evaluations = (whole_cycles + 1) * MAX_FAILED_ATTEMPTS

    seconds_per_minute = 60
    documented_guesses_per_minute = 4.75
    assert wrong.calls == expected_evaluations
    assert (
        wrong.calls * seconds_per_minute / attack_seconds
        == documented_guesses_per_minute
    )

    # An order of magnitude above the documented rate: far clear of 285, and
    # nowhere near the 3,541 a gate that locks only once would have handed
    # over. The bound is deliberately loose so it fails for one reason only.
    unbounded_alarm = attack_seconds // 10
    assert wrong.calls < unbounded_alarm, (
        "the lockout must re-arm on every window; a gate that locks once is "
        "unbounded for the rest of the attack"
    )


# ---------------------------------------------------------------------------
# Total behaviour: never raises, and reset() really resets
# ---------------------------------------------------------------------------


def test_attempt_returns_a_bool_in_every_state_and_never_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every state answers with a plain ``bool``; none of them raises.

    ``creek_mcp.tools.purge._gate`` does not catch, so anything raised out of
    the budget escapes into the FastMCP tool surface and skips the audit entry
    every purge call is required to write. The lockout adds a whole new family
    of states to get that wrong in, so the walk covers all of them: fresh,
    part-spent, just-armed, deep inside the window, at the boundary, and after
    expiry.
    """
    clock = _pin_clock(monkeypatch)
    budget = _fresh_budget()
    armed_at = clock.now
    wrong = VerifySpy(result=False)
    correct = VerifySpy(result=True)

    observed: list[bool] = []
    for _ in range(MAX_FAILED_ATTEMPTS - 1):
        observed.append(budget.attempt(wrong))
    observed.append(budget.attempt(wrong))
    observed.append(budget.attempt(correct))
    clock.now = armed_at + LOCKOUT_SECONDS / 2
    observed.append(budget.attempt(wrong))
    clock.now = armed_at + LOCKOUT_SECONDS - 1.0
    observed.append(budget.attempt(correct))
    clock.now = armed_at + LOCKOUT_SECONDS
    observed.append(budget.attempt(correct))

    assert observed == [False] * (MAX_FAILED_ATTEMPTS + 3) + [True]
    assert all(type(value) is bool for value in observed)


def test_reset_clears_an_armed_lockout(monkeypatch: pytest.MonkeyPatch) -> None:
    """``reset`` reopens the gate without waiting out the window.

    This is the hook ``tests/conftest.py`` uses for isolation, so its whole
    value is that it works *immediately* -- a reset that only cleared the
    counter would leave the next test inheriting the window.
    """
    _pin_clock(monkeypatch)
    budget = _fresh_budget()
    _arm_the_lockout(budget)

    budget.reset()

    correct = VerifySpy(result=True)
    assert budget.attempt(correct) is True
    assert correct.calls == 1


def test_reset_clears_the_failure_counter_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``reset`` restores the full allowance, not just the open gate.

    Distinguishable from the test above only by what happens *next*: if the
    counter survived the reset, the very first failure afterwards would push
    it back over the limit and re-arm the window.
    """
    _pin_clock(monkeypatch)
    budget = _fresh_budget()
    _arm_the_lockout(budget)

    budget.reset()

    wrong = VerifySpy(result=False)
    for _ in range(MAX_FAILED_ATTEMPTS - 1):
        assert budget.attempt(wrong) is False
    assert wrong.calls == MAX_FAILED_ATTEMPTS - 1

    correct = VerifySpy(result=True)
    assert budget.attempt(correct) is True


# ---------------------------------------------------------------------------
# Structural invariants: no sleeping, no import cycle, and a budget in auth
# ---------------------------------------------------------------------------


def test_a_throttled_refusal_never_sleeps() -> None:
    """The gate refuses immediately; it does not stall the caller.

    Sleeping is the obvious way to rate limit and the wrong one here. The MCP
    server would hold the request open, so an attacker who could previously
    fail a guess for free could instead pin a worker for a minute per guess --
    trading a brute-force channel for a denial of service. The absence of a
    call is only expressible structurally, so it is asserted on the AST, and
    the ``from time import sleep as ...`` escape route is closed alongside it.
    """
    tree = ast.parse(_policy_source())

    slept = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _callee_name(node) == "sleep"
    ]
    assert slept == [], "attempt_policy must refuse without blocking the caller"

    aliased = [
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "time"
        for alias in node.names
        if alias.name == "sleep"
    ]
    assert aliased == [], f"time.sleep must not be imported under any name: {aliased}"


def test_the_policy_module_depends_on_nothing_in_creek() -> None:
    """The budget is stdlib-only, so nothing can import-cycle through it.

    :mod:`creek_mcp.auth` imports this module at import time. If the
    dependency ever pointed back -- even at a constant -- the cycle would
    surface as an ``ImportError`` in whichever of the two happened to load
    first, a failure that depends on collection order rather than on code and
    that a green suite can hide for a long time.
    """
    tree = ast.parse(_policy_source())
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append("." * node.level + (node.module or ""))

    first_party = [name for name in imported if name.startswith((".", "creek"))]
    assert first_party == [], f"attempt_policy must stay stdlib-only: {first_party}"


def test_the_auth_module_owns_a_budget_the_conftest_hook_can_reset() -> None:
    """``tests/conftest.py``'s autouse reset must have something real to reset.

    That fixture looks ``_ELEVATED_BUDGET`` up tolerantly, because it has to
    survive the RED window in which the attribute does not exist yet.
    Tolerance nothing checks decays into a silent no-op, and a silent no-op
    there means process-global auth state leaks between tests and the suite
    becomes order-dependent -- the exact failure the fixture exists to
    prevent. This assertion is what keeps the tolerance honest.
    """
    assert isinstance(auth._ELEVATED_BUDGET, AttemptBudget)


# ---------------------------------------------------------------------------
# Concurrency: the budget is process-global state under a lock
# ---------------------------------------------------------------------------


def test_concurrent_attempts_cannot_overspend_the_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Racing callers still get ``MAX_FAILED_ATTEMPTS`` evaluations between them.

    The budget is process-global state that every MCP request reads and
    writes. An unsynchronised read-modify-write of the counter would let N
    concurrent callers each observe "four failures so far" and each spend a
    fifth, turning a five-guess gate into a five-guesses-per-thread gate --
    the brute-force channel #914 closes, reopened under another name.

    Honest about what this proves: a passing run is not a proof, because
    interleaving is the scheduler's business and a serialised run passes
    trivially. A *failing* run is proof of a real defect, and the barrier
    maximises the overlap that makes the difference detectable. The join
    timeout doubles as the non-blocking assertion -- ``attempt`` must never
    park a caller.
    """
    _pin_clock(monkeypatch)
    budget = _fresh_budget()
    wrong = VerifySpy(result=False)
    thread_count = 8
    attempts_each = 3
    barrier = threading.Barrier(thread_count)
    outcomes: list[bool] = []
    outcomes_lock = threading.Lock()

    def _hammer() -> None:
        """Line up with the other threads, then spend attempts flat out."""
        barrier.wait()
        mine = [budget.attempt(wrong) for _ in range(attempts_each)]
        with outcomes_lock:
            outcomes.extend(mine)

    workers = [threading.Thread(target=_hammer) for _ in range(thread_count)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10.0)

    assert not any(worker.is_alive() for worker in workers), (
        "attempt() must refuse without blocking; a live thread means it parked"
    )
    assert outcomes == [False] * (thread_count * attempts_each)
    assert wrong.calls == MAX_FAILED_ATTEMPTS
