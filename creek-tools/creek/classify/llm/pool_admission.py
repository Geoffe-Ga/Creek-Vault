"""Vault-side admission for pool generations (ADR-0016 Decision 1 item 8, #1896).

Per vault: at most :data:`POOL_MAX_IN_FLIGHT` generations in flight and a
configurable hourly rate, default :data:`DEFAULT_POOL_RATE_PER_HOUR` (three
times adepthood's monthly allowance in a single hour). A managed vault is one
process, so a process-wide limiter *is* the per-vault limiter, and one vault's
traffic can never spend another's budget.

**Why the vault enforces this even though Privatemode has per-key limits.**
Privatemode does carry per-minute limits, including a request cap (owner
comment 2026-10-25 on #1896), so rate *can* be partly delegated. It is not:
the vendor's granularity is not Decision 1 item 8's, and admission is not
delegated. The per-key monthly euro cap bounds total spend; this bounds rate
and concurrency.

A refusal is decided **before** any dial and immediately — never by queueing —
so the caller learns ``rate_limited`` within the 1 s T7 allows rather than
after a wait. It carries no content: the only facts in it are which of the two
limits refused, as a closed label.
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from contextlib import contextmanager
from typing import TYPE_CHECKING, Final

from creek.classify.llm.pool_boundary import PoolBoundaryError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping

POOL_MAX_IN_FLIGHT: Final[int] = 2
"""Most pool generations one vault may have in flight at once."""

DEFAULT_POOL_RATE_PER_HOUR: Final[int] = 60
"""Default pool generations one vault may start in a rolling hour."""

POOL_RATE_ENV: Final[str] = "CREEK_POOL_RATE_PER_HOUR"
"""Machine-environment override for the hourly rate; never vault config."""

_WINDOW_SECONDS: Final[float] = 3600.0

IN_FLIGHT_LIMIT: Final[str] = "in_flight"
"""Closed label: the concurrency limit refused the generation."""

HOURLY_LIMIT: Final[str] = "hourly"
"""Closed label: the hourly rate refused the generation."""


class PoolAdmissionRefusedError(PoolBoundaryError):
    """The vault's own limiter refused a pool generation; nothing was sent.

    Attributes:
        limit: :data:`IN_FLIGHT_LIMIT` or :data:`HOURLY_LIMIT`.
    """

    def __init__(self, limit: str) -> None:
        """Store which limit refused, behind a fixed message.

        Args:
            limit: The closed label of the refusing limit.
        """
        super().__init__(f"the vault's pool admission limit refused: {limit}")
        self.limit = limit


def configured_rate(environ: Mapping[str, str] | None = None) -> int:
    """Return the hourly rate the Machine configures, or the default.

    Args:
        environ: Environment to read; ``os.environ`` when omitted.

    Returns:
        A positive integer. A blank, non-numeric or non-positive value falls
        back to :data:`DEFAULT_POOL_RATE_PER_HOUR` rather than to "unlimited".
    """
    source = os.environ if environ is None else environ
    raw = source.get(POOL_RATE_ENV, "").strip()
    if not raw.isdigit() or int(raw) <= 0:
        return DEFAULT_POOL_RATE_PER_HOUR
    return int(raw)


class PoolAdmission:
    """One vault's in-flight and hourly admission for pool generations."""

    def __init__(
        self,
        *,
        max_in_flight: int = POOL_MAX_IN_FLIGHT,
        rate_per_hour: int = DEFAULT_POOL_RATE_PER_HOUR,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Configure the two limits.

        Args:
            max_in_flight: Concurrency ceiling.
            rate_per_hour: Starts admitted per rolling hour.
            clock: Monotonic clock, injectable for tests.

        Raises:
            ValueError: When either limit is not positive.
        """
        if max_in_flight <= 0 or rate_per_hour <= 0:
            msg = "pool admission limits must be positive"
            raise ValueError(msg)
        self._max_in_flight = max_in_flight
        self._rate = rate_per_hour
        self._clock = clock
        self._lock = threading.Lock()
        self._in_flight = 0
        self._starts: deque[float] = deque()

    @contextmanager
    def admit(self) -> Iterator[None]:
        """Hold one admission slot for the block, or refuse at once.

        Yields:
            Nothing; the slot is released however the block ends.

        Raises:
            PoolAdmissionRefusedError: When either limit is reached.
        """
        self._acquire()
        try:
            yield
        finally:
            with self._lock:
                self._in_flight -= 1

    def _acquire(self) -> None:
        """Take a slot and record a start, or raise without changing state."""
        now = self._clock()
        with self._lock:
            while self._starts and now - self._starts[0] >= _WINDOW_SECONDS:
                self._starts.popleft()
            if self._in_flight >= self._max_in_flight:
                raise PoolAdmissionRefusedError(IN_FLIGHT_LIMIT)
            if len(self._starts) >= self._rate:
                raise PoolAdmissionRefusedError(HOURLY_LIMIT)
            self._in_flight += 1
            self._starts.append(now)


_VAULT_ADMISSION: Final[dict[str, PoolAdmission]] = {}
_VAULT_ADMISSION_LOCK: Final[threading.Lock] = threading.Lock()
_VAULT: Final[str] = "vault"


def vault_admission() -> PoolAdmission:
    """Return this process's limiter, built once from the Machine environment.

    Built under a lock so two first callers racing on worker threads cannot
    each get a limiter of their own, which would double both limits.

    Returns:
        The process-wide :class:`PoolAdmission`.
    """
    with _VAULT_ADMISSION_LOCK:
        if _VAULT not in _VAULT_ADMISSION:
            _VAULT_ADMISSION[_VAULT] = PoolAdmission(rate_per_hour=configured_rate())
        return _VAULT_ADMISSION[_VAULT]
