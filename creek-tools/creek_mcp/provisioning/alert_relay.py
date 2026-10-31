"""Keep routing when the fleet alert channel fails (owner decision R2-4, #1999).

Owner decision R2-4 reads: *"When the alert channel fails: keep routing, retry
alerts, refuse new activations."*  Before it, an :class:`AlertDeliveryError`
escaped the pilot scheduler and tripped the supervisor's shared stop, so one
unreachable alert endpoint ended routing for every vault.

:class:`AlertRelay` sits between ``fleet_cli`` and the alert sink instead:

* it contains :class:`AlertDeliveryError` - and only that error - so the
  scheduler, the edge, and the worker keep running;
* it retains the undelivered closed alert-kind counts and re-sends them with
  capped doubling backoff (one, two, then at most four backoff units);
* while an alert is owed it closes an :class:`AdmissionGate` that the control
  plane consults beside its static policy, so new activations are refused
  with the existing generic admission error, and reopens it on the first
  successful delivery;
* it records the gap through a second channel that does not depend on the
  failed sink: a durable SQLite counter plus gap row, and one content-free
  ``ERROR`` log line per failed delivery.

The gap and the owed counts are durable, so a restarted pilot keeps admission
closed until those alerts are actually delivered.
"""

from __future__ import annotations

import logging
import math
from collections import Counter
from datetime import UTC, datetime
from threading import Event
from time import monotonic
from typing import TYPE_CHECKING, Final

from creek_mcp.provisioning.budget import AlertKind
from creek_mcp.provisioning.pilot_alerts import AlertDeliveryError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from creek_mcp.provisioning.store import ProvisioningStore

_LOGGER = logging.getLogger(__name__)
_KINDS: Final[frozenset[str]] = frozenset(kind.value for kind in AlertKind)
# The retry delay doubles per consecutive failure at most twice: one, two,
# then four backoff units, where the pilot's unit is one fleet interval.
_MAX_BACKOFF_DOUBLINGS: Final[int] = 2


def _utc_now() -> datetime:
    """Return the current timezone-aware UTC timestamp."""
    return datetime.now(tz=UTC)


class AdmissionGate:
    """A thread-safe runtime switch for new activations; open until closed."""

    def __init__(self) -> None:
        """Start open: only a failed alert delivery closes admission."""
        self._open = Event()
        self._open.set()

    @property
    def is_open(self) -> bool:
        """Return whether new activations may currently be admitted."""
        return self._open.is_set()

    def __call__(self) -> bool:
        """Answer the control plane's ``() -> bool`` admission probe."""
        return self.is_open

    def open(self) -> None:
        """Admit new activations again."""
        self._open.set()

    def close(self) -> None:
        """Refuse new activations until :meth:`open` is called."""
        self._open.clear()


class AlertRelay:
    """Deliver fleet alerts, retaining and retrying them while the sink fails.

    Not thread-safe: exactly one scheduler thread calls :meth:`collect` and
    :meth:`flush`.  The :class:`AdmissionGate` it drives is thread-safe.
    """

    def __init__(
        self,
        store: ProvisioningStore,
        gate: AdmissionGate,
        deliver: Callable[[Iterable[str]], None],
        *,
        backoff_seconds: float,
        clock: Callable[[], float] = monotonic,
        utc_clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        """Bind the durable store and reload any gap a previous run left open."""
        if not math.isfinite(backoff_seconds) or backoff_seconds <= 0:
            raise ValueError("alert retry backoff must be finite and positive")
        self._store = store
        self._gate = gate
        self._deliver = deliver
        self._backoff_seconds = backoff_seconds
        self._clock = clock
        self._utc_clock = utc_clock
        self._collected: Counter[str] = Counter()
        self._failures = 0
        self._retry_at: float | None = None
        gap = store.open_alert_delivery_gap()
        self._gap_open = gap is not None
        self._pending: Counter[str] = Counter(None if gap is None else gap.pending)
        if self._gap_open:
            gate.close()

    def collect(self, kinds: tuple[str, ...]) -> None:
        """Accept one report's alert kinds; ``fleet_cli`` calls this as its sink."""
        if not set(kinds) <= _KINDS:
            raise ValueError("fleet alert kind is invalid")
        self._collected |= Counter(kinds)

    def flush(self) -> None:
        """Make at most one delivery attempt for everything owed, when due."""
        batch = self._pending | self._collected
        self._collected = Counter()
        if not batch:
            return
        if self._retry_at is not None and self._clock() < self._retry_at:
            self._pending = batch
            return
        try:
            self._deliver(tuple(sorted(batch.elements())))
        except AlertDeliveryError:
            self._record_failure(batch)
            return
        self._record_recovery()

    def _record_failure(self, batch: Counter[str]) -> None:
        """Keep *batch*, schedule its retry, record the gap, close admission."""
        self._failures += 1
        self._pending = batch
        delay = self._backoff_seconds * 2 ** min(
            self._failures - 1, _MAX_BACKOFF_DOUBLINGS
        )
        self._retry_at = self._clock() + delay
        self._store.record_alert_delivery_failure(batch, now=self._utc_clock())
        self._gap_open = True
        self._gate.close()
        _LOGGER.error(
            "fleet alert delivery failed; new activations refused "
            "consecutive_failures=%d pending_alerts=%d retry_in_seconds=%d",
            self._failures,
            batch.total(),
            math.ceil(delay),
        )

    def _record_recovery(self) -> None:
        """Forget what was delivered, close any gap, and reopen admission."""
        if self._gap_open:
            self._store.close_alert_delivery_gap(now=self._utc_clock())
            _LOGGER.info(
                "fleet alert delivery recovered; new activations admitted "
                "consecutive_failures=%d",
                self._failures,
            )
        self._pending = Counter()
        self._failures = 0
        self._retry_at = None
        self._gap_open = False
        self._gate.open()
