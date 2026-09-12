"""Narrow provider inventory and stop boundaries for fleet reconciliation (#1769).

The reconciler is typed against these Protocols instead of ``ProviderDriver``:
neither exposes ``delete``, so under mypy strict a repair cannot destroy a
provider resource.  Nothing here imports the store or a provider adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence

    from creek_mcp.provisioning.models import ResourceClass, ResourceState


@dataclass(frozen=True, slots=True)
class ProviderResource:
    """One provider resource as inventory sees it: identifiers and sizes only.

    ``provider_allocation_id`` is always derived from where the resource lives
    (the app), never from metadata the resource claims about itself.
    """

    provider_allocation_id: str
    resource_class: ResourceClass
    state: ResourceState
    size_gb: int | None
    activation_id: str | None
    provider_ref: str


class FleetInventorySource(Protocol):
    """Read-only provider inventory over a bounded, caller-supplied known set."""

    def list_resources(
        self,
        activation_ids: Sequence[str],
        *,
        app_names: Sequence[str] = (),
    ) -> tuple[ProviderResource, ...]:
        """Return every resource under the derived and injected app names."""

    def expected_allocation_id(self, activation_id: str) -> str:
        """Return the allocation id a provisioned *activation_id* would carry."""


class FleetStopper(Protocol):
    """The single mutating repair the reconciler may perform."""

    def stop(self, activation_id: str) -> None:
        """Idempotently stop one allocation's Machine without detaching storage."""
