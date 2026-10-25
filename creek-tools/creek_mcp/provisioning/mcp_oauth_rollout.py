"""Operator rollout of MCP OAuth settings onto one existing allocation (#1974).

An allocation provisioned before the control plane wrote
``CREEK_MCP_RESOURCE_URL`` and ``CREEK_MCP_AUTHORIZATION_SERVER`` keeps its
exact legacy Machine environment, and keeps routing, until an operator rolls
it.  This module is that one-shot operation: resolve the allocation through
the same ownership query routing uses, then let
:meth:`~creek_mcp.provisioning.fly.FlyProviderDriver.roll_mcp_oauth` write the
record and update the stopped Machine.  It is reached through
``--roll-mcp-oauth REQUESTER CONSUMER`` on the worker and the pilot.

Every outcome is logged content-free — no identity, address, issuer or
provider detail — and mapped to a process exit status an operator script can
branch on.
"""

from __future__ import annotations

import logging
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

EXIT_RETRY: Final[int] = 75
"""A transient refusal (``EX_TEMPFAIL``), such as a Machine still running."""


class RoutableAllocations(Protocol):
    """The store's ownership-bound allocation lookup."""

    def get_routable_allocation(
        self,
        requester_identity: str,
        consumer_identity: str,
    ) -> RoutableAllocation | None:
        """Resolve one ready allocation through its authenticated ownership."""


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
        if allocation is None:
            _LOGGER.warning("mcp oauth roll: no ready allocation for that owner")
            return EXIT_NOT_FOUND
        changed = driver.roll_mcp_oauth(allocation, records)
    except ProviderError as exc:
        _LOGGER.warning("mcp oauth roll refused: %s", exc.reason.value)
        return EXIT_RETRY if exc.retryable else EXIT_REFUSED
    except ValueError:
        _LOGGER.warning("mcp oauth roll refused: the record cannot take the settings")
        return EXIT_REFUSED
    _LOGGER.info("mcp oauth roll: %s", "rolled" if changed else "already current")
    return EXIT_ROLLED
