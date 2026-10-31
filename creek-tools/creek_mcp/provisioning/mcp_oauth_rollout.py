"""Operator rollout of MCP OAuth settings onto existing allocations (#1974, #2004).

An allocation provisioned before the control plane wrote
``CREEK_MCP_RESOURCE_URL`` and ``CREEK_MCP_AUTHORIZATION_SERVER`` keeps its
exact legacy Machine environment, and keeps routing, until an operator rolls
it; one provisioned on the shared ``<edge>/mcp`` audience keeps that until it
is rolled to its own ``<edge>/a/<slug>/mcp`` (#2004).  This module is that
one-shot operation: resolve the allocation through the same ownership query
routing uses, then let
:meth:`~creek_mcp.provisioning.fly.FlyProviderDriver.roll_mcp_oauth` write the
record and update the stopped Machine.  It is reached through
``--roll-mcp-oauth REQUESTER CONSUMER`` on the worker and the pilot.
:func:`roll_allocation` reports one allocation, for a batch caller (#1869).

:func:`check_shared_audience` is the fleet check, reached through
``--check-mcp-audience``: it counts every allocation still not on its own
resource.  The edge's ``--refuse-shared-mcp-audience`` is flipped only once it
is empty, and the edge itself refuses to start while it is not.

Every outcome is logged content-free — no identity, address, issuer or
provider detail — and mapped to a process exit status an operator script can
branch on.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum, unique
from typing import TYPE_CHECKING, Final, Protocol

from creek_mcp.provisioning.driver import ProviderError

if TYPE_CHECKING:
    from creek_mcp.provisioning.fly import FlyProviderDriver, McpOAuthRecords
    from creek_mcp.provisioning.models import RoutableAllocation

_LOGGER = logging.getLogger(__name__)

EXIT_ROLLED: Final[int] = 0
"""The allocation now carries its settings (rolled now, or already)."""

EXIT_NOT_FOUND: Final[int] = 1
"""No ready, provider-managed allocation is owned by that pair."""

EXIT_REFUSED: Final[int] = 2
"""Drift, a record that cannot take the settings, or a provider rejection."""

EXIT_SHARED_AUDIENCE_REMAINS: Final[int] = 3
"""The fleet check found allocations not yet on their own resource."""

EXIT_RETRY: Final[int] = 75
"""A transient refusal (``EX_TEMPFAIL``), such as a Machine still running."""


@unique
class RollOutcome(StrEnum):
    """What one allocation's roll did."""

    ROLLED = "rolled"
    ALREADY_CURRENT = "already-current"
    REFUSED = "refused"


@dataclass(frozen=True, slots=True)
class RollReport:
    """One allocation's content-free roll outcome.

    Attributes:
        outcome: Rolled, already current, or refused.
        reason: For a refusal, the provider reason value or ``record``.
        retryable: For a refusal, whether a later retry can succeed.
    """

    outcome: RollOutcome
    reason: str | None = None
    retryable: bool = False

    def __str__(self) -> str:
        """Return ``rolled``, ``already-current`` or ``refused(<reason>)``."""
        if self.outcome is not RollOutcome.REFUSED:
            return self.outcome.value
        suffix = ", retryable" if self.retryable else ""
        return f"{self.outcome.value}({self.reason}{suffix})"

    @property
    def exit_status(self) -> int:
        """Return the process exit status this outcome maps to."""
        if self.outcome is not RollOutcome.REFUSED:
            return EXIT_ROLLED
        return EXIT_RETRY if self.retryable else EXIT_REFUSED


class RoutableAllocations(Protocol):
    """The store's ownership-bound allocation lookup."""

    def get_routable_allocation(
        self,
        requester_identity: str,
        consumer_identity: str,
    ) -> RoutableAllocation | None:
        """Resolve one ready allocation through its authenticated ownership."""


class SharedAudienceRecords(Protocol):
    """The encrypted records' fleet check."""

    def shared_audience_activations(self) -> tuple[str, ...]:
        """List every live activation not yet on its own resource."""


def roll_allocation(
    driver: FlyProviderDriver,
    records: McpOAuthRecords,
    allocation: RoutableAllocation,
) -> RollReport:
    """Roll one resolved allocation and report what happened.

    Args:
        driver: The Fly driver, over the activation records *records* writes.
        records: The encrypted record writer configured with the settings.
        allocation: The store-owned allocation to roll.

    Returns:
        The allocation's report; never raises for a refusal.
    """
    try:
        changed = driver.roll_mcp_oauth(allocation, records)
    except ProviderError as exc:
        return RollReport(RollOutcome.REFUSED, exc.reason.value, exc.retryable)
    except ValueError:
        return RollReport(RollOutcome.REFUSED, "record")
    return RollReport(RollOutcome.ROLLED if changed else RollOutcome.ALREADY_CURRENT)


def roll_owned_allocation(
    store: RoutableAllocations,
    driver: FlyProviderDriver,
    records: McpOAuthRecords,
    *,
    requester_identity: str,
    consumer_identity: str,
) -> int:
    """Roll the allocation *requester_identity* provisioned for *consumer_identity*.

    Args:
        store: The provisioning store.
        driver: The Fly driver, over the activation records *records* writes.
        records: The encrypted record writer configured with the settings.
        requester_identity: The provisioning requester, e.g. Adepthood.
        consumer_identity: The person whose vault to roll.

    Returns:
        One of :data:`EXIT_ROLLED`, :data:`EXIT_NOT_FOUND`,
        :data:`EXIT_REFUSED` or :data:`EXIT_RETRY`.
    """
    try:
        allocation = store.get_routable_allocation(
            requester_identity, consumer_identity
        )
    except ValueError:
        _LOGGER.warning("mcp oauth roll: refused(owner)")
        return EXIT_REFUSED
    if allocation is None:
        _LOGGER.warning("mcp oauth roll: no ready allocation for that owner")
        return EXIT_NOT_FOUND
    report = roll_allocation(driver, records, allocation)
    _LOGGER.info("mcp oauth roll: %s", report)
    return report.exit_status


def check_shared_audience(records: SharedAudienceRecords) -> int:
    """Run the fleet check and log only its count.

    Args:
        records: The encrypted records.

    Returns:
        :data:`EXIT_ROLLED` when none remain on the shared audience,
        :data:`EXIT_SHARED_AUDIENCE_REMAINS` when any do, and
        :data:`EXIT_REFUSED` when the records cannot be read or no settings
        are configured.
    """
    try:
        pending = records.shared_audience_activations()
    except ValueError:
        _LOGGER.warning("mcp audience check: refused(record)")
        return EXIT_REFUSED
    _LOGGER.info(
        "mcp audience check: %d allocation(s) remain on the shared audience",
        len(pending),
    )
    return EXIT_SHARED_AUDIENCE_REMAINS if pending else EXIT_ROLLED
