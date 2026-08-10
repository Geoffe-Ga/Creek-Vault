"""Hand-driven clock and verify-callback doubles for the #914 attempt budget.

``creek_mcp.attempt_policy.AttemptBudget`` refuses a caller for
``LOCKOUT_SECONDS`` after too many failures. Asserting that against a real
clock would mean sleeping a minute per assertion, so every #914 test drives
time by hand through :class:`FakeMonotonicClock`, pinned over the policy
module's ``_now`` seam. Issue #914 forbids ``time.sleep`` in the production
path; this module is what lets the *tests* obey the same rule.

Deliberately imports nothing from :mod:`creek_mcp`. Three test modules import
this one at module scope, and two of them (``tests/test_mcp_auth.py`` and
``tests/test_mcp_purge.py``) must keep *collecting* while
``creek_mcp.attempt_policy`` does not exist yet, so their behavioural tests
fail on their own assertions rather than on an import-time
``ModuleNotFoundError``.

Named as a flat ``tests/*_support.py`` module to match
``tests/v1_api_support.py`` and ``tests/adapter_parity.py``. It is not
collected: ``python_files = ["test_*.py"]`` in ``pyproject.toml`` does not
match it.
"""

from __future__ import annotations

import threading


class FakeMonotonicClock:
    """A stand-in for :func:`time.monotonic` that only a test advances.

    Monotonic semantics are the point of the seam it replaces: an NTP step
    must not be able to shorten a lockout, so the production clock cannot be
    wall-clock time and this double cannot offer a way to run backwards.

    Attributes:
        now: The instant the clock currently reports, in seconds. Assign to
            it directly when a test needs an exact absolute instant (the
            window boundary), or call :meth:`advance` for a relative step.
    """

    def __init__(self, start: float = 1000.0) -> None:
        """Start the clock at *start*.

        Args:
            start: The first instant the clock reports, in seconds. The
                default is a round, obviously synthetic value, so a real
                timestamp leaking into a failure message is easy to spot.
        """
        self.now = start

    def __call__(self) -> float:
        """Report the current instant, as :func:`time.monotonic` would.

        Returns:
            The pinned instant, in seconds.
        """
        return self.now

    def advance(self, seconds: float) -> None:
        """Move the clock forward by *seconds*.

        Args:
            seconds: How far forward to move, in seconds.

        Raises:
            ValueError: If *seconds* is negative. A monotonic clock that
                runs backwards is not a monotonic clock, and a test that
                relied on one would be pinning behaviour the production
                seam can never produce.
        """
        if seconds < 0:
            msg = f"a monotonic clock cannot run backwards: {seconds!r}"
            raise ValueError(msg)
        self.now += seconds


class VerifySpy:
    """A verify callback that records whether the budget consulted it at all.

    The budget's central security property is a negative one — while a
    lockout is armed the supplied token is *not evaluated* — and no return
    value can express that. A call count can.

    The counter is guarded by its own lock so the spy stays exact when
    several threads race the same budget; the budget's own lock would
    already serialise the increments if it works, which is precisely the
    thing under test and therefore not something to rely on here.

    Attributes:
        result: The fixed answer this spy gives the budget.
        calls: How many times the budget invoked this callback.
    """

    def __init__(self, *, result: bool) -> None:
        """Create a spy that always answers *result*.

        Args:
            result: What the callback reports: ``True`` for a token that
                matches, ``False`` for one that does not.
        """
        self.result = result
        self.calls = 0
        self._lock = threading.Lock()

    def __call__(self) -> bool:
        """Record the consultation and answer.

        Returns:
            The fixed :attr:`result` this spy was built with.
        """
        with self._lock:
            self.calls += 1
        return self.result
