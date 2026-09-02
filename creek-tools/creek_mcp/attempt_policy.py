"""Failed-attempt budget for the elevated-authorization gate (#914).

The defect this closes: ``creek_mcp.auth.is_elevated`` compared the
caller's token against ``CREEK_MCP_ELEVATED_TOKEN`` in constant time, but
it compared it *as often as it was asked to*. Constant-time comparison
stops an attacker learning the secret one byte at a time; it does nothing
about guessing it whole, at machine speed, against a surface that answers
in microseconds. A partially-trusted caller who could reach the MCP
boundary could therefore brute-force the token that authorizes
irreversible ``creek.purge.*`` vault destruction.

:class:`AttemptBudget` puts a price on failure: after
:data:`MAX_FAILED_ATTEMPTS` consecutive denials the gate stops evaluating
tokens at all for :data:`LOCKOUT_SECONDS`.

**The lockout is non-blocking.** Sleeping is the obvious way to rate
limit and it is the wrong one here. The five purge tools are registered
by ``creek_mcp.server._register_purge_tools``, whose five
``@server.tool(name="creek.purge.*")`` wrappers are each declared ``def``
and not ``async def`` — they are *sync* functions — and the MCP SDK
invokes a sync tool function inline on the asyncio event loop
(``mcp/server/fastmcp/utilities/func_metadata.py:96``). A blocking
backoff in this module would therefore freeze the entire server — every
tool, every consumer, both transports — for the duration, converting a
rate-limit fix into a remotely-triggerable availability kill. There is no
``sleep`` anywhere in this module, and there must never be one: a
throttled caller is refused *immediately*, and pays in refusals rather
than in held connections.

Deliberately stdlib-only. :mod:`creek_mcp.auth` imports this module at
import time, so a dependency pointing back — even at a single constant —
would be an import cycle that surfaces as an ``ImportError`` in whichever
module happened to load first.
"""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Callable

MAX_FAILED_ATTEMPTS: Final[int] = 5
"""Consecutive denials that arm the lockout.

Five *evaluated* guesses per lockout window, against the keyspace of a
``secrets.token_urlsafe(32)`` secret — the 32-character floor
``creek_mcp.token_policy.MIN_TOKEN_LEN`` enforces on any configured
elevated token (#907). Five guesses per minute against that keyspace is
not a search anyone completes; the pairing of the two policies is what
makes the bound worth anything, which is why the floor is named here even
though this module must not import it.
"""

LOCKOUT_SECONDS: Final[float] = 60.0
"""How long the gate stays shut once the budget is spent.

The self-heal guarantee: a lockout is a delay, never a ban. Sixty seconds
after the last allowed failure the gate reopens with its full allowance
restored, with no operator intervention, no restart, and no way for a
hostile caller to hold it shut (see
:meth:`AttemptBudget.attempt` — attempts made inside the window do not
extend it). Bricking the only path an operator has to erase their own
data would be a worse outcome than the brute-force channel #914 closes.
"""

# Monkeypatchable clock alias, mirroring `creek_mcp.remote_auth._now` so both
# time-dependent auth surfaces are pinned the same way in tests. (Named rather
# than cited by line: that module is under active change on another branch.)
# Monotonic rather than `time.time` on purpose, and unlike that seam: wall-clock
# time can be stepped by NTP or by an operator changing the system clock, and a
# lockout a clock change can shorten is a lockout an attacker can shorten.
_now = time.monotonic


class AttemptBudget:
    """A consecutive-failure allowance guarding one verification surface.

    Verification is supplied to :meth:`attempt` as a callback rather than
    performed by the caller, so an unthrottled check is not expressible at
    the type level: there is no way to consult the secret except through
    the budget that meters the consultation.

    Instances are safe to share across threads. Every read-modify-write of
    the counter happens under one lock, so N concurrent callers cannot each
    observe "one attempt left" and each spend it — which would turn an
    N-guess gate into an N-guesses-per-thread gate.
    """

    def __init__(self, *, max_failures: int, lockout_seconds: float) -> None:
        """Configure a budget.

        The policy is taken from these arguments and never re-read from
        :data:`MAX_FAILED_ATTEMPTS` / :data:`LOCKOUT_SECONDS`, so an
        instance means exactly what its construction site says it means.

        Args:
            max_failures: Consecutive failed attempts that arm the lockout.
            lockout_seconds: How long the lockout lasts, in seconds, as
                measured by the monotonic :data:`_now` clock.
        """
        self._max_failures = max_failures
        self._lockout_seconds = lockout_seconds
        self._lock = threading.Lock()
        self._failures = 0
        self._locked_until: float | None = None

    def attempt(self, verify: Callable[[], bool]) -> bool:
        """Consult *verify* if the budget allows it, and account for the answer.

        The only entry point. While a lockout is armed *verify is not
        called at all* — that is what makes the bound a bound, rather than
        a throttle that still runs the comparison and discards the result.

        Attempts made inside the window do not extend it. Without that
        rule one hostile guess per second would keep the window
        permanently re-armed and lock the legitimate operator out of their
        own purge tools forever, with every individual refusal looking
        correct.

        *verify* must not block and must not perform I/O. The purge tools
        are sync MCP tool functions invoked inline on the asyncio event
        loop, so anything slow here stalls the whole server, not just this
        request. For the same reason this method never sleeps, and the
        internal lock is never held across a sleep or any I/O — it covers
        only the counter arithmetic and the *verify* call itself.

        Args:
            verify: Performs the actual check, returning whether the
                caller's credential is correct. Called at most once.

        Returns:
            ``True`` only when the budget was open *and* *verify* said yes.
            A refusal inside the window is indistinguishable from an
            ordinary rejection, so the lockout never becomes an oracle.
        """
        with self._lock:
            now = _now()
            if self._locked_until is not None:
                if now < self._locked_until:
                    return False
                self._locked_until = None
                self._failures = 0

            if verify():
                self._failures = 0
                return True

            self._failures += 1
            if self._failures >= self._max_failures:
                self._locked_until = now + self._lockout_seconds
            return False

    def reset(self) -> None:
        """Clear the failure count and any armed lockout.

        The test-isolation hook: the budget guarding the elevated gate is
        process-global, so failures accrued by one test would otherwise
        leak into the next and make the suite order-dependent. The autouse
        ``_reset_elevated_attempt_budget`` fixture in ``tests/conftest.py``
        calls this before every test.

        Not for production use. Clearing a live lockout on demand would
        hand back precisely the unbounded guessing #914 took away.
        """
        with self._lock:
            self._failures = 0
            self._locked_until = None
