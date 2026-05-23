"""Tests for :mod:`creek.hierarchy` — level-policy filtering (FEAT-025).

Pin the single source of truth that compile / state / lint consult when
deciding which structural levels they operate on. The helpers must:

* default to "leaves" semantics that respect the input set (a leaf is
  a fragment whose children are not currently being considered, not
  one whose ``child_ids`` is unconditionally empty);
* expose a "documents" projection that keeps the whole-source levels
  (``document`` / ``session``) without leaking sentence/paragraph rows;
* return a deterministic ``source_levels`` list suitable for compiled-
  page frontmatter; and
* derive a ``structural_path`` breadcrumb from in-memory ancestry when
  the persisted field is empty.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from creek.hierarchy import (
    LevelPolicy,
    select_by_policy,
    source_levels,
    structural_path_context,
)
from creek.models import (
    Authorship,
    Fragment,
    FragmentSource,
    SourcePlatform,
)


def _frag(
    frag_id: str,
    *,
    level: str = "document",
    parent_id: str | None = None,
    child_ids: list[str] | None = None,
    structural_path: list[str] | None = None,
    title: str | None = None,
) -> Fragment:
    """Build a minimal Fragment for hierarchy tests."""
    return Fragment(
        id=frag_id,
        title=title or f"Title for {frag_id}",
        source=FragmentSource(
            platform=SourcePlatform.MARKDOWN,
            author=Authorship.SELF,
        ),
        created=datetime(2026, 5, 1, tzinfo=UTC),
        ingested=datetime(2026, 5, 1, tzinfo=UTC),
        level=level,  # type: ignore[arg-type]
        parent_id=parent_id,
        child_ids=child_ids or [],
        structural_path=structural_path or [],
    )


class TestSelectByPolicyLeaves:
    """``leaves`` keeps the most-atomic fragments in the input set."""

    def test_flat_vault_returns_everyone(self) -> None:
        """A flat vault — every fragment is a leaf — passes through unchanged."""
        a = _frag("a")
        b = _frag("b")
        result = select_by_policy([a, b], "leaves")
        assert [f.id for f in result] == ["a", "b"]

    def test_parent_dropped_when_children_in_set(self) -> None:
        """A parent fragment is filtered out when its children are present."""
        parent = _frag("p", level="document", child_ids=["c1", "c2"])
        child1 = _frag("c1", level="paragraph", parent_id="p")
        child2 = _frag("c2", level="paragraph", parent_id="p")
        result = select_by_policy([parent, child1, child2], "leaves")
        assert [f.id for f in result] == ["c1", "c2"]

    def test_parent_retained_when_children_absent(self) -> None:
        """A parent with no in-set children is itself a leaf for this slice."""
        parent = _frag("p", level="document", child_ids=["c1"])
        result = select_by_policy([parent], "leaves")
        assert [f.id for f in result] == ["p"]

    def test_three_level_hierarchy_keeps_grandchildren(self) -> None:
        """Sentence-level grandchildren win against paragraph + section parents."""
        section = _frag("sec", level="section", child_ids=["p1"])
        paragraph = _frag("p1", level="paragraph", parent_id="sec", child_ids=["s1"])
        sentence = _frag("s1", level="sentence", parent_id="p1")
        result = select_by_policy([section, paragraph, sentence], "leaves")
        assert [f.id for f in result] == ["s1"]

    def test_preserves_input_order(self) -> None:
        """Selected leaves retain their relative order from the input list."""
        a = _frag("a")
        b = _frag("b")
        c = _frag("c")
        result = select_by_policy([c, a, b], "leaves")
        assert [f.id for f in result] == ["c", "a", "b"]


class TestSelectByPolicyDocuments:
    """``documents`` keeps whole-source granularity only."""

    def test_keeps_document_and_session(self) -> None:
        """``document`` and ``session`` survive; finer levels are dropped."""
        doc = _frag("doc", level="document")
        session = _frag("ses", level="session")
        sentence = _frag("sen", level="sentence")
        paragraph = _frag("par", level="paragraph")
        result = select_by_policy([doc, session, sentence, paragraph], "documents")
        assert {f.id for f in result} == {"doc", "ses"}

    def test_empty_when_no_document_levels_present(self) -> None:
        """A purely-sentence-level slice yields zero documents."""
        result = select_by_policy([_frag("s", level="sentence")], "documents")
        assert result == []


class TestSelectByPolicyAll:
    """``all`` is a passthrough — used by legacy / regression paths."""

    def test_passthrough(self) -> None:
        """Every fragment, regardless of level, is returned."""
        a = _frag("a", level="sentence")
        b = _frag("b", level="document")
        result = select_by_policy([a, b], "all")
        assert [f.id for f in result] == ["a", "b"]


class TestSelectByPolicyRejectsUnknown:
    """An unknown policy name is a programmer error, not a silent passthrough."""

    def test_unknown_policy_raises(self) -> None:
        """A typo should surface immediately rather than fall through."""
        with pytest.raises(ValueError, match="level_policy"):
            select_by_policy([_frag("a")], "everything")  # type: ignore[arg-type]


class TestSourceLevels:
    """``source_levels`` returns a deterministic sorted list of distinct levels."""

    def test_distinct_levels_sorted(self) -> None:
        """Mixed-level inputs collapse to a sorted set of strings."""
        frags = [
            _frag("a", level="paragraph"),
            _frag("b", level="sentence"),
            _frag("c", level="paragraph"),
        ]
        assert source_levels(frags) == ["paragraph", "sentence"]

    def test_empty_inputs_returns_empty(self) -> None:
        """An empty fragment list yields an empty levels list."""
        assert source_levels([]) == []


class TestStructuralPathContext:
    """Breadcrumb derivation from the data model."""

    def test_persisted_structural_path_wins(self) -> None:
        """When the fragment carries its own breadcrumb, return that verbatim."""
        leaf = _frag(
            "s1",
            level="sentence",
            parent_id="p1",
            structural_path=["Section A", "Paragraph 3"],
        )
        path = structural_path_context(leaf, {})
        assert path == ["Section A", "Paragraph 3"]

    def test_walks_parents_when_path_empty(self) -> None:
        """Without a persisted path, walk ``parent_id`` and collect titles."""
        grandparent = _frag(
            "sec",
            level="section",
            child_ids=["p1"],
            title="The Capricorn Moon",
        )
        parent = _frag(
            "p1",
            level="paragraph",
            parent_id="sec",
            child_ids=["s1"],
            title="On grief",
        )
        leaf = _frag("s1", level="sentence", parent_id="p1")
        by_id = {f.id: f for f in (grandparent, parent, leaf)}
        path = structural_path_context(leaf, by_id)
        assert path == ["The Capricorn Moon", "On grief"]

    def test_missing_parent_terminates_walk(self) -> None:
        """A dangling ``parent_id`` ends the walk without raising."""
        leaf = _frag("s1", level="sentence", parent_id="missing")
        assert structural_path_context(leaf, {"s1": leaf}) == []

    def test_no_parent_returns_empty(self) -> None:
        """A root fragment has no breadcrumb."""
        assert structural_path_context(_frag("a"), {}) == []


class TestLevelPolicyType:
    """Sanity: the public literal accepts the documented strings."""

    @pytest.mark.parametrize("policy", ["leaves", "documents", "all"])
    def test_accepts_documented_values(self, policy: LevelPolicy) -> None:
        """The literal must include every value the generators rely on."""
        # The call must not raise for any documented policy.
        select_by_policy([], policy)
