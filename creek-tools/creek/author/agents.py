"""Stub specialist agents for the Creek Writing Desk (FEAT-041, #455).

Each specialist gathers a slice of structured evidence for the conductor. In
the skeleton they return deterministic mock claims; issues #463/#467 replace
the bodies with real Graph, Retrieval, and Ontology logic while preserving
this interface.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from creek.author.models import EvidenceBundle, EvidenceClaim

if TYPE_CHECKING:
    from pathlib import Path


@runtime_checkable
class Specialist(Protocol):
    """A specialist agent that contributes structured evidence.

    Implementations expose a stable :attr:`name` (used in the conductor's
    plan) and a :meth:`gather` method returning an :class:`EvidenceBundle`.
    """

    name: str

    def gather(self, query: str, vault: Path) -> EvidenceBundle:
        """Return structured evidence for *query* drawn from *vault*."""
        ...


class GraphSpecialist:
    """Stub Graph specialist — would walk the resonance/link graph."""

    name = "graph"

    def gather(self, query: str, vault: Path) -> EvidenceBundle:
        """Return a mock graph-derived claim (skeleton)."""
        return EvidenceBundle(
            claims=[
                EvidenceClaim(
                    claim=f"Graph neighbours relevant to {query!r} (stub).",
                    source_fragments=["frag-graph-0001"],
                )
            ]
        )


class RetrievalSpecialist:
    """Stub Retrieval specialist — would fetch the most relevant fragments."""

    name = "retrieval"

    def gather(self, query: str, vault: Path) -> EvidenceBundle:
        """Return a mock retrieval-derived claim (skeleton)."""
        return EvidenceBundle(
            claims=[
                EvidenceClaim(
                    claim=f"Top retrieved passage for {query!r} (stub).",
                    source_fragments=["frag-retrieval-0001"],
                )
            ]
        )


class OntologySpecialist:
    """Stub Ontology specialist — would ground claims in the APTITUDE model."""

    name = "ontology"

    def gather(self, query: str, vault: Path) -> EvidenceBundle:
        """Return a mock ontology-derived claim (skeleton)."""
        return EvidenceBundle(
            claims=[
                EvidenceClaim(
                    claim=f"Ontology framing for {query!r} (stub).",
                    source_fragments=["frag-ontology-0001"],
                )
            ]
        )


def default_specialists() -> list[Specialist]:
    """Return the ordered default specialist roster (graph, retrieval, ontology)."""
    return [GraphSpecialist(), RetrievalSpecialist(), OntologySpecialist()]
