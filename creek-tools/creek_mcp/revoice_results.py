"""Bounded, process-local revoice results; rewritten text is never persisted.

Durable composition records contain only counts. Detached revoice bodies live
here, readable for ten minutes, with at most 32 retained at once. Expired
bodies are removed lazily on the next cache operation. Only the starting agent
at an adequate declared ceiling may read them. Expiry, eviction and restart all
mean unavailable, rather than an empty successful rewrite.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from collections.abc import Callable

from creek_mcp.tier_ceiling import TierCeiling, routing_tier, tier_allowed

MAX_REVOICE_RESULTS: Final = 32
REVOICE_RESULT_SECONDS: Final = 600.0


@dataclass(frozen=True)
class _Result:
    """One successful detached response and the caller boundary it belongs to."""

    consumer: str
    ceiling: TierCeiling
    response: dict[str, Any]
    expires_at: float


class RevoiceResults:
    """Retain bounded transient bodies across a composition's deadline handoff."""

    def __init__(
        self,
        *,
        limit: int = MAX_REVOICE_RESULTS,
        lifetime: float = REVOICE_RESULT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Create a per-server result cache with count and age bounds.

        Args:
            limit: Maximum successful responses to retain.
            lifetime: Seconds a detached result remains readable.
            clock: Monotonic time source, injectable for hermetic expiry tests.
        """
        self._limit = limit
        self._lifetime = lifetime
        self._clock = clock
        self._entries: OrderedDict[str, _Result] = OrderedDict()
        self._lock = threading.Lock()

    def _expire(self, now: float) -> None:
        """Drop expired bodies while holding the cache lock."""
        expired = [
            key for key, entry in self._entries.items() if entry.expires_at <= now
        ]
        for key in expired:
            del self._entries[key]

    def put(
        self,
        job_id: str,
        *,
        consumer: str,
        ceiling: TierCeiling,
        response: dict[str, Any],
    ) -> None:
        """Retain a detached success without writing it to the durable job record.

        Args:
            job_id: The opaque handle the caller already received.
            consumer: The initiating authenticated agent.
            ceiling: The original declared ceiling.
            response: The successful response returned by the revoice adapter.
        """
        with self._lock:
            now = self._clock()
            self._expire(now)
            self._entries[job_id] = _Result(
                consumer, ceiling, deepcopy(response), now + self._lifetime
            )
            while len(self._entries) > self._limit:
                self._entries.popitem(last=False)

    def get(
        self, job_id: str, *, consumer: str, ceiling: TierCeiling
    ) -> dict[str, Any] | None:
        """Return a copy of an entitled caller's live body, otherwise nothing.

        Args:
            job_id: The durable job's handle, already resolved for this caller.
            consumer: The authenticated agent asking for the body.
            ceiling: The read's declared ceiling, which may not downgrade it.

        Returns:
            A response copy, or ``None`` for expiry, eviction, another agent or
            an inadequate ceiling. A read never renews the retention deadline.
        """
        with self._lock:
            self._expire(self._clock())
            entry = self._entries.get(job_id)
            if (
                entry is None
                or entry.consumer != consumer
                or not tier_allowed(routing_tier(entry.ceiling, None), ceiling)
            ):
                return None
            return deepcopy(entry.response)
