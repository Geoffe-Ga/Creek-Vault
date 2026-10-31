"""Degrade instead of exiting when the Fly provider refuses the credential (#1866).

Owner decision R2-4 (2026-10-27) reads: *"When the alert channel fails: keep
routing, retry alerts, refuse new activations."*  Issue #1866 extends that
degrade-don't-exit posture to a ``401`` or ``403`` from the Fly Machines API.

Before it, a revoked org token made every scheduled report exit ``1``; the
scheduler raised, the supervisor stopped every peer, and Fly's restart cap
left the control Machine *stopped*, so callers met a ten-second connect
timeout instead of a fast typed ``503`` and nobody was told.

:class:`ProviderAuthState` is the ``provider_auth_failed`` flag the pilot
holds instead.  While it is set:

* the control plane refuses new activations (it is one of the admission
  probes beside the R2-4 alert gate);
* the worker claims nothing, so no create and no deletion runs against an
  unverified provider;
* the scheduler component reports not-ready, so ``/__fly/health`` is ``503``;
* the edge keeps serving: a routed request still meets the provider refusal
  and answers the existing fast ``503 temporarily_unavailable``.

Entering the state collects exactly one ``provider_credential_rejected``
alert; the R2-4 :class:`~creek_mcp.provisioning.alert_relay.AlertRelay`
retains and retries it if the sink is down.  The first provider-authoritative
report that succeeds clears the state.  Nothing here reads, logs, or forwards
token material, provider identifiers, or URLs.
"""

from __future__ import annotations

import logging
from threading import Event
from typing import TYPE_CHECKING

from creek_mcp.provisioning.budget import AlertKind

if TYPE_CHECKING:
    from collections.abc import Callable

_LOGGER = logging.getLogger(__name__)


class ProviderAuthState:
    """A thread-safe ``provider_auth_failed`` flag; clear until a refusal.

    Only the scheduler thread records; the control plane, worker, and health
    probe read it through :meth:`__call__`.
    """

    def __init__(self) -> None:
        """Start clear: only an observed provider refusal sets the flag."""
        self._failed = Event()

    @property
    def provider_auth_failed(self) -> bool:
        """Return whether the provider last refused the pilot's credential."""
        return self._failed.is_set()

    def __call__(self) -> bool:
        """Answer the ``() -> bool`` admission, claim, and readiness probes."""
        return not self._failed.is_set()

    def record_failure(self, collect: Callable[[tuple[str, ...]], None]) -> None:
        """Set the flag; on entry only, log once and hand *collect* one alert."""
        if self._failed.is_set():
            return
        self._failed.set()
        _LOGGER.error(
            "provider refused the pilot credential; provider_auth_failed set, "
            "new activations and provider claims refused, health unavailable"
        )
        collect((AlertKind.PROVIDER_CREDENTIAL_REJECTED.value,))

    def record_success(self) -> None:
        """Clear the flag after a provider-authoritative report succeeded."""
        if not self._failed.is_set():
            return
        self._failed.clear()
        _LOGGER.info("provider authentication recovered; provider_auth_failed clear")
