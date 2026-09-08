"""Read-only provider fleet enumeration, separate from provisioning (#1769).

ADR-0013 Decision 6 requires that every resource Creek pays for is reconciled
against the durable control plane. Nothing could observe that before this
module: :class:`~creek_mcp.provisioning.driver.ProviderDriver` declares exactly
``provision`` and ``delete``, and every Fly lookup is keyed off an
``activation_id`` the caller must already possess. Fleet reconciliation needs
the opposite direction — "what is the provider billing us for?" — which no
existing method can answer.

Enumeration is declared here as its own Protocol rather than added to
``ProviderDriver``, because the two capabilities must stay narrow in opposite
directions. The durable worker holds the provider credential and must not be
able to enumerate the fleet; the reconciler reads the whole fleet and must not
be able to create or destroy anything. Keeping them apart is also what lets a
future inventory be fed by an offline invoice export rather than a live API.

:class:`ProviderResource` is content-free *by construction*, not by convention.
It carries no activation id and no free-text field, so an implementation cannot
leak one by accident. That matters specifically here: ``FlyProviderDriver``
writes the raw, unhashed ``activation_id`` into Machine metadata as
``creek_activation_id``, and reading it back would put the preimage into every
reconciliation report, alert and audit record. Attribution instead uses the
allocation surrogate the store already holds.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum, unique
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime


@unique
class ProviderResourceClass(StrEnum):
    """The billable resource kinds one Creek allocation can consist of."""

    APP = "app"
    MACHINE = "machine"
    VOLUME = "volume"
    SNAPSHOT = "snapshot"


@unique
class MetricQuality(StrEnum):
    """How far an observed fleet measurement can be trusted.

    The Fly Machines API exposes apps, volumes and Machines — not egress and
    not billing. Any figure Creek reports must therefore say which of these it
    is, so an operator reconciling an invoice knows what to compare. A silent
    zero for an unavailable meter is the failure this enum exists to prevent.
    """

    EXACT = "exact"
    ESTIMATED = "estimated"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class ProviderResource:
    """One content-free observation of a resource the provider is billing for.

    ``provider_allocation_id`` is the surrogate the durable store already holds,
    derived from the provider's own naming, never from inverting a digest and
    never from Machine metadata. ``state_since`` is the instant the provider
    last reported this state, which is what makes "running beyond policy"
    measurable without Creek observing every start and stop itself.
    """

    resource_class: ProviderResourceClass
    provider_id: str
    provider_allocation_id: str | None
    state: str
    region: str | None = None
    size_gb: int | None = None
    size_bytes: int | None = None
    state_since: datetime | None = None


class ProviderInventory(Protocol):
    """A read-only enumeration capability over one provider account."""

    def list_resources(self) -> Sequence[ProviderResource]:
        """Return every resource this account is currently billed for."""
