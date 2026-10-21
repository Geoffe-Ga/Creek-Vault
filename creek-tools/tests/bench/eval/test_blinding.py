"""Blinding: opaque ids, a seeded shuffle, and no model identity downstream.

The blinded candidate is the *only* thing the judge-facing layer sees, so the
model tag is absent from it by construction rather than by convention — a
dataclass with no such field cannot leak one.
"""

from __future__ import annotations

from dataclasses import fields, replace

import pytest

from creek_mcp.bench.eval.blinding import (
    BlindedCandidate,
    Candidate,
    blind,
    candidate_id,
    identity_map,
)
from tests.bench.eval.conftest import (
    IN_VOICE_TEXT,
    SLOP_TEXT,
    in_voice_candidate,
    slop_candidate,
)

_IDENTITY_FIELDS = frozenset({"model_tag", "model", "provider", "vendor"})


def _candidates() -> tuple[Candidate, ...]:
    """Return four candidates with distinct texts and distinct model tags."""
    return (
        replace(in_voice_candidate("alpha-tiny"), text=f"{IN_VOICE_TEXT} One."),
        replace(slop_candidate("beta-large"), text=f"{SLOP_TEXT} Two."),
        replace(in_voice_candidate("gamma-mid"), text=f"{IN_VOICE_TEXT} Three."),
        replace(slop_candidate("delta-huge"), text=f"{SLOP_TEXT} Four."),
    )


def test_blinded_candidate_has_no_identity_field_at_all() -> None:
    """Structural: the judge-facing record cannot carry a model name."""
    names = {field.name for field in fields(BlindedCandidate)}
    assert not names & _IDENTITY_FIELDS


def test_blinded_ids_never_contain_the_model_tag() -> None:
    """The opaque id is derived from seed and slot, never from the model."""
    blinded = blind(_candidates(), seed=3)
    tags = {candidate.model_tag for candidate in _candidates()}
    for item in blinded:
        for tag in tags:
            assert tag not in item.candidate_id


def test_blinding_is_reproducible_at_the_same_seed() -> None:
    """Two runs at one seed present the same candidates in the same order."""
    assert blind(_candidates(), seed=11) == blind(_candidates(), seed=11)


def test_a_different_seed_changes_presentation_order() -> None:
    """The shuffle is real: across seeds, more than one order is presented."""
    candidates = _candidates()
    orders = {
        tuple(item.text for item in blind(candidates, seed=seed)) for seed in range(40)
    }
    assert len(orders) > 1
    for order in orders:
        assert sorted(order) == sorted(c.text for c in candidates)


def test_blinding_preserves_every_candidate_exactly_once() -> None:
    """A shuffle must not drop, duplicate or edit a candidate's output."""
    candidates = _candidates()
    blinded = blind(candidates, seed=5)
    assert len(blinded) == len(candidates)
    assert sorted(item.text for item in blinded) == sorted(c.text for c in candidates)


def test_identity_map_recovers_the_model_behind_each_opaque_id() -> None:
    """The harness can unblind; the judge is simply never given the map."""
    candidates = _candidates()
    blinded = blind(candidates, seed=9)
    mapping = identity_map(candidates, seed=9)
    assert set(mapping) == {item.candidate_id for item in blinded}
    assert sorted(mapping.values()) == sorted(c.model_tag for c in candidates)


def test_identity_map_pairs_the_right_model_with_the_right_text() -> None:
    """Unblinding must be correct, not merely consistent in its key set."""
    candidates = _candidates()
    blinded = blind(candidates, seed=4)
    mapping = identity_map(candidates, seed=4)
    by_text = {c.text: c.model_tag for c in candidates}
    for item in blinded:
        assert mapping[item.candidate_id] == by_text[item.text]


def test_candidate_id_is_a_closed_hex_identifier() -> None:
    """The id goes into an artifact, so it carries no content of its own."""
    ident = candidate_id(seed=2, slot=0)
    assert len(ident) == 16
    assert all(char in "0123456789abcdef" for char in ident)


def test_candidate_ids_are_distinct_within_a_run() -> None:
    """Two candidates must never collide onto one opaque label."""
    blinded = blind(_candidates(), seed=6)
    assert len({item.candidate_id for item in blinded}) == len(blinded)


def test_blinding_refuses_an_empty_candidate_set() -> None:
    """Blinding nothing is a plan error, not an empty successful run."""
    with pytest.raises(ValueError, match="at least one candidate"):
        blind((), seed=0)
