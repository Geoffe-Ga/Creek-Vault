"""Per-claim provenance records for compiled-layer pages (FEAT-003).

A :class:`ProvenanceEntry` records the link between a single claim on a
compiled synthesis page (Thread, Eddy, Frequency index) and the fragment
IDs that produced it. The list of entries lives in the compiled page's
YAML frontmatter so any downstream consumer (``creek draft``, the
CrawDad reflection loop) can verify every claim against its sources.

The schema is the one called out in FEAT-003's "Pre-decided choices"
block; it is stable and should not be re-litigated as part of this
feature.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

CompileMethod = Literal["rules", "llm", "manual"]
"""How the claim was produced — used for audit and downstream filtering."""


class ProvenanceEntry(BaseModel):
    """One claim on a compiled page, traced back to the fragments it came from.

    Attributes:
        claim_id: Stable per-page identifier for the claim
            (e.g. ``claim-001``). Must be unique within a single
            :class:`~creek.models.CompiledPage`.
        claim_excerpt: The first ~80 characters of the claim, kept as a
            human-readable anchor in the audit log so an operator can
            recognise the claim without re-reading the full page.
        fragment_ids: Ordered list of fragment IDs that contributed to
            the claim. Order matches the LLM's reported salience.
        compiled_at: UTC timestamp of the compile run that emitted this
            entry. On idempotent re-runs, the latest timestamp wins.
        compile_method: ``"rules"``, ``"llm"``, or ``"manual"`` —
            determines how downstream consumers should weight the claim.
    """

    model_config = ConfigDict(use_enum_values=True)

    claim_id: str
    claim_excerpt: str
    fragment_ids: list[str] = Field(default_factory=list)
    compiled_at: datetime
    compile_method: CompileMethod


def merge_provenance(
    existing: list[ProvenanceEntry],
    new: list[ProvenanceEntry],
) -> list[ProvenanceEntry]:
    """Merge two provenance lists, keying by ``claim_id``.

    Idempotency contract (FEAT-003): re-running compile against the
    same fragments must produce a deterministic update. Claims with
    matching ``claim_id`` are merged so the latest run's metadata
    (timestamp, compile method, claim excerpt) wins, while
    ``fragment_ids`` accumulate as a deduplicated, order-preserving
    union of the two sources.

    Args:
        existing: Provenance entries already on the compiled page
            (typically loaded from frontmatter).
        new: Provenance entries from the current compile run.

    Returns:
        A merged list ordered by first appearance — existing claims
        come first in their original order, with new ``claim_id`` s
        appended at the end.
    """
    by_claim: dict[str, ProvenanceEntry] = {entry.claim_id: entry for entry in existing}
    order: list[str] = [entry.claim_id for entry in existing]
    for entry in new:
        prior = by_claim.get(entry.claim_id)
        if prior is None:
            by_claim[entry.claim_id] = entry
            order.append(entry.claim_id)
            continue
        merged_ids = list(dict.fromkeys([*prior.fragment_ids, *entry.fragment_ids]))
        by_claim[entry.claim_id] = ProvenanceEntry(
            claim_id=entry.claim_id,
            claim_excerpt=entry.claim_excerpt,
            fragment_ids=merged_ids,
            compiled_at=entry.compiled_at,
            compile_method=entry.compile_method,
        )
    return [by_claim[claim_id] for claim_id in order]
