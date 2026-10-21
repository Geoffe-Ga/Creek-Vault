"""Blinding: opaque ids, a seeded shuffle, and no identity downstream.

The judge must not know which model it is scoring, or the comparison measures
reputation rather than prose. Two things enforce that here, and neither is a
convention a future caller can forget:

- :class:`BlindedCandidate` has **no** model field. A record with nowhere to
  put an identity cannot leak one, so the assertion is structural
  (``tests/bench/eval/test_blinding.py``) rather than a review habit.
- The opaque id is derived from the run's seed and the candidate's
  presentation slot — never from the model tag — so the label itself carries
  no hint.

The shuffle is seeded, so a run is reproducible: the same seed presents the
same candidates in the same order. A different seed changes that order and the
labels, and nothing else: every scorer reads the candidate's text, so the score
of a fixed output does not move.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from creek_mcp.bench.eval.rubric import RefusalProbe

ID_HEX: Final[int] = 16
"""Hex characters in an opaque candidate id."""

_EMPTY: Final[str] = "blinding needs at least one candidate"


@dataclass(frozen=True, slots=True)
class Candidate:
    """One candidate model's output under test, with its identity attached.

    This is the harness-side record. It never reaches the judge; :func:`blind`
    reduces it to a :class:`BlindedCandidate` first.

    Attributes:
        model_tag: Which model produced the output. Harness-side only.
        text: The candidate's reflection or essay.
        citations: Fragment ids the output claims to be grounded in. An empty
            tuple is an ungrounded output, which scores zero on grounding.
        refusals: Per-probe record of whether the candidate refused. A probe
            absent from the mapping counts as *not* refused: silence is not a
            refusal.
    """

    model_tag: str
    text: str
    citations: tuple[str, ...] = ()
    refusals: Mapping[RefusalProbe, bool] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class BlindedCandidate:
    """A candidate as the scorers and the judge see it: no identity.

    Attributes:
        candidate_id: Opaque label, derived from the seed and the slot.
        text: The candidate's output, unchanged.
        citations: The claimed fragment ids, unchanged.
        refusals: The per-probe refusal record, unchanged.
    """

    candidate_id: str
    text: str
    citations: tuple[str, ...]
    refusals: Mapping[RefusalProbe, bool]


def candidate_id(*, seed: int, slot: int) -> str:
    """Return the opaque id for the candidate in *slot* of a *seed*ed run.

    Derived from the seed and slot alone, so it is stable across processes
    (``hash()`` is salted per process) and tells the judge nothing about which
    model filled the slot.

    Args:
        seed: The run's presentation seed.
        slot: Zero-based presentation position.

    Returns:
        A :data:`ID_HEX`-character lowercase hex string.
    """
    digest = hashlib.sha256(f"blind:{seed}:{slot}".encode()).hexdigest()
    return digest[:ID_HEX]


def _order(candidates: Sequence[Candidate], seed: int) -> list[int]:
    """Return candidate indices in presentation order for *seed*.

    Raises:
        ValueError: When *candidates* is empty.
    """
    if not candidates:
        raise ValueError(_EMPTY)
    indices = list(range(len(candidates)))
    random.Random(seed).shuffle(indices)
    return indices


def blind(
    candidates: Sequence[Candidate], *, seed: int
) -> tuple[BlindedCandidate, ...]:
    """Shuffle *candidates* from *seed* and strip every model identity.

    Args:
        candidates: The candidates to present.
        seed: The run's presentation seed.

    Returns:
        The blinded candidates in presentation order.

    Raises:
        ValueError: When *candidates* is empty — an eval of nothing is a plan
            error, not an empty successful run.
    """
    return tuple(
        BlindedCandidate(
            candidate_id=candidate_id(seed=seed, slot=slot),
            text=candidates[index].text,
            citations=candidates[index].citations,
            refusals=dict(candidates[index].refusals),
        )
        for slot, index in enumerate(_order(candidates, seed))
    )


def identity_map(candidates: Sequence[Candidate], *, seed: int) -> dict[str, str]:
    """Return ``candidate_id -> model_tag`` for a *seed*ed presentation.

    The harness keeps this so a finished run can be attributed. The judge is
    simply never handed it — there is no code path that passes it on.

    Args:
        candidates: The same candidates, in the same order, as :func:`blind`.
        seed: The same seed.

    Returns:
        The unblinding map.

    Raises:
        ValueError: When *candidates* is empty.
    """
    return {
        candidate_id(seed=seed, slot=slot): candidates[index].model_tag
        for slot, index in enumerate(_order(candidates, seed))
    }
