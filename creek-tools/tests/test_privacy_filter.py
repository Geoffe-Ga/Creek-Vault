"""Tests for :mod:`creek.classify.privacy_filter`.

The module owns tier filtering for every generation flow, so the tests
pin down each branch of the override matrix and confirm the privacy
audit log records exactly the elevated-inclusion calls.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import frontmatter
import pytest

from creek.audit import AuditLog
from creek.classify import privacy_filter
from creek.classify.privacy_filter import (
    PRIVACY_AUDIT_RELPATH,
    PrivacyTierOverride,
    ancestry_tiers,
    build_ancestor_index,
    filter_fragments_by_tier,
    override_elevates,
    parse_include_tier,
    record_privacy_override,
    source_tiers,
    tier_within_override,
)
from creek.models import (
    Authorship,
    Fragment,
    FragmentSource,
    Frequency,
    FrequencyClassification,
    PrivacyTier,
    SourcePlatform,
)

if TYPE_CHECKING:
    from pathlib import Path


def _frag(
    *, id_: str, tier: PrivacyTier, title: str = "T", body: str = "B"
) -> tuple[Fragment, str]:
    """Return a (fragment, body) pair pinned to a specific privacy tier."""
    fragment = Fragment(
        id=id_,
        title=title,
        source=FragmentSource(
            platform=SourcePlatform.JOURNAL,
            author=Authorship.SELF,
            original_file="x.md",
        ),
        created=datetime(2025, 1, 1, tzinfo=UTC),
        privacy_tier=tier,
        frequency=FrequencyClassification(primary=Frequency.UNCLASSIFIED, secondary=[]),
    )
    return fragment, body


def test_default_excludes_intimate_summarises_personal() -> None:
    """Default policy drops intimate and replaces personal bodies."""
    inputs = [
        _frag(id_="frag-i", tier=PrivacyTier.INTIMATE, body="secret stuff"),
        _frag(id_="frag-p", tier=PrivacyTier.PERSONAL, body="personal stuff"),
        _frag(id_="frag-o", tier=PrivacyTier.OPEN, body="open stuff"),
    ]

    out = list(filter_fragments_by_tier(inputs))

    ids = [f.id for f, _ in out]
    assert "frag-i" not in ids
    bodies = {f.id: body for f, body in out}
    assert bodies["frag-o"] == "open stuff"
    assert "personal stuff" not in bodies["frag-p"]
    assert "summary" in bodies["frag-p"].lower()


def test_personal_override_passes_full_body_excludes_intimate() -> None:
    """``--include-tier personal`` keeps personal bodies, drops intimate."""
    inputs = [
        _frag(id_="frag-i", tier=PrivacyTier.INTIMATE, body="x"),
        _frag(id_="frag-p", tier=PrivacyTier.PERSONAL, body="full body"),
    ]

    out = list(
        filter_fragments_by_tier(inputs, override=PrivacyTierOverride.PERSONAL),
    )

    ids = [f.id for f, _ in out]
    assert ids == ["frag-p"]
    assert out[0][1] == "full body"


@pytest.mark.parametrize(
    "override",
    [PrivacyTierOverride.INTIMATE, PrivacyTierOverride.ALL],
)
def test_intimate_or_all_lets_everything_through(
    override: PrivacyTierOverride,
) -> None:
    """``intimate``/``all`` includes every tier with full bodies."""
    inputs = [
        _frag(id_="frag-i", tier=PrivacyTier.INTIMATE, body="secret"),
        _frag(id_="frag-p", tier=PrivacyTier.PERSONAL, body="personal"),
        _frag(id_="frag-o", tier=PrivacyTier.OPEN, body="open"),
    ]

    out = list(filter_fragments_by_tier(inputs, override=override))

    bodies = {f.id: body for f, body in out}
    assert bodies == {"frag-i": "secret", "frag-p": "personal", "frag-o": "open"}


def test_unclassified_tier_is_summarised_like_personal() -> None:
    """``unclassified`` is treated as ``personal`` — title-only by default (#876).

    Rewritten from ``test_unclassified_tier_passes_through_with_full_body``,
    which pinned the exact bug: an untiered fragment ranked alongside
    ``open`` and handed its **full body** to every generation flow. Since
    ``creek classify`` had no privacy caller at all, that meant the entire
    private corpus was mineable, draftable and voice-proxy eligible at the
    open tier.

    New contract: an unclassified fragment is still *yielded* (so the
    operator sees it exists) but contributes a title-only summary unless
    the caller explicitly raises the ceiling.
    """
    inputs = [
        _frag(
            id_="frag-u",
            tier=PrivacyTier.UNCLASSIFIED,
            title="Untiered note",
            body="raw body",
        ),
    ]

    out = list(filter_fragments_by_tier(inputs))

    assert len(out) == 1
    fragment, body = out[0]
    assert fragment.id == "frag-u"
    assert "raw body" not in body
    assert "Untiered note" in body


@pytest.mark.parametrize(
    "override",
    [
        PrivacyTierOverride.PERSONAL,
        PrivacyTierOverride.INTIMATE,
        PrivacyTierOverride.ALL,
    ],
)
def test_unclassified_full_body_only_under_raised_ceiling(
    override: PrivacyTierOverride,
) -> None:
    """An unclassified body passes intact only under personal/intimate/all (#876)."""
    inputs = [
        _frag(
            id_="frag-u",
            tier=PrivacyTier.UNCLASSIFIED,
            title="Untiered note",
            body="raw body",
        ),
    ]

    out = list(filter_fragments_by_tier(inputs, override=override))

    assert [(f.id, b) for f, b in out] == [("frag-u", "raw body")]


def test_tier_of_unknown_string_fails_closed_to_intimate(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Fragments carrying an unrecognised tier string fail closed.

    Regression for PR #193 review (comment 4367360694 LOW): the prior
    ``tier_of`` did ``PrivacyTier(fragment.privacy_tier)`` with no
    safety net, so a hand-edited or schema-migrated vault with an
    unknown tier string would crash generation flows. The new helper
    catches the :class:`ValueError`, logs a warning that names the
    fragment ID, and returns :data:`PrivacyTier.INTIMATE` so the
    fragment is excluded from the default-policy output.
    """
    from creek.classify.privacy_filter import tier_of

    # ``model_construct`` skips Pydantic validation, allowing us to
    # plant a bogus tier value the same way a hand-edited markdown file
    # or a forward-incompatible schema migration would.
    bogus = Fragment.model_construct(
        id="frag-bogus",
        title="t",
        source=FragmentSource(
            platform=SourcePlatform.JOURNAL,
            author=Authorship.SELF,
            original_file="x.md",
        ),
        created=datetime(2025, 1, 1, tzinfo=UTC),
        privacy_tier="brand-new-tier-v2",  # type: ignore[arg-type]
        frequency=FrequencyClassification(primary=Frequency.UNCLASSIFIED, secondary=[]),
    )

    with caplog.at_level("WARNING", logger="creek.classify.privacy_filter"):
        tier = tier_of(bogus)

    assert tier is PrivacyTier.INTIMATE
    assert any(
        "frag-bogus" in r.message and "INTIMATE" in r.message for r in caplog.records
    )


def test_open_override_matches_default_behaviour() -> None:
    """``--include-tier open`` explicitly == default (no flag).

    The flag value exists for symmetry with ``personal``/``intimate``/
    ``all``; users who pass it should observe identical filtering to
    callers who pass nothing. The ``unclassified`` input is present so the
    equivalence covers the #876 rank change too — ``open`` and the default
    must agree about an untiered fragment as well.
    """
    inputs = [
        _frag(id_="frag-i", tier=PrivacyTier.INTIMATE, body="x"),
        _frag(id_="frag-p", tier=PrivacyTier.PERSONAL, body="full"),
        _frag(id_="frag-o", tier=PrivacyTier.OPEN, body="open"),
        _frag(id_="frag-u", tier=PrivacyTier.UNCLASSIFIED, body="untiered"),
    ]

    default_out = list(filter_fragments_by_tier(inputs))
    open_out = list(
        filter_fragments_by_tier(inputs, override=PrivacyTierOverride.OPEN),
    )

    assert [f.id for f, _ in default_out] == [f.id for f, _ in open_out]
    assert [body for _, body in default_out] == [body for _, body in open_out]


def test_override_elevates_matrix() -> None:
    """The elevation predicate is true for everything except None/open."""
    assert not override_elevates(None)
    assert not override_elevates(PrivacyTierOverride.OPEN)
    assert override_elevates(PrivacyTierOverride.PERSONAL)
    assert override_elevates(PrivacyTierOverride.INTIMATE)
    assert override_elevates(PrivacyTierOverride.ALL)


def test_record_privacy_override_writes_audit_entry(tmp_path: Path) -> None:
    """Recording an override appends a chained entry to privacy.jsonl."""
    vault = tmp_path / "vault"
    vault.mkdir()

    record_privacy_override(
        vault_path=vault,
        command="mine",
        fragment_ids=["frag-A", "frag-B"],
        operator="alice",
        override=PrivacyTierOverride.INTIMATE,
    )

    log_path = vault / PRIVACY_AUDIT_RELPATH
    assert log_path.exists()
    entries = list(AuditLog(log_path).read())
    assert len(entries) == 1
    entry = entries[0]
    assert entry["command"] == "mine"
    assert entry["operator"] == "alice"
    assert entry["include_tier"] == "intimate"
    assert entry["fragment_ids"] == ["frag-A", "frag-B"]


def test_parse_include_tier_handles_known_and_unknown() -> None:
    """The parser accepts canonical values and rejects others."""
    assert parse_include_tier(None) is None
    assert parse_include_tier("intimate") is PrivacyTierOverride.INTIMATE
    assert parse_include_tier("ALL") is PrivacyTierOverride.ALL
    with pytest.raises(ValueError, match="--include-tier"):
        parse_include_tier("nope")


@pytest.mark.parametrize(
    ("tier", "override", "expected"),
    [
        (PrivacyTier.OPEN, PrivacyTierOverride.OPEN, True),
        (PrivacyTier.PERSONAL, PrivacyTierOverride.OPEN, False),
        (PrivacyTier.INTIMATE, PrivacyTierOverride.OPEN, False),
        (PrivacyTier.PERSONAL, PrivacyTierOverride.PERSONAL, True),
        (PrivacyTier.INTIMATE, PrivacyTierOverride.PERSONAL, False),
        (PrivacyTier.INTIMATE, PrivacyTierOverride.INTIMATE, True),
        (PrivacyTier.INTIMATE, PrivacyTierOverride.ALL, True),
        (PrivacyTier.INTIMATE, None, False),  # None defaults to OPEN
        # #876: UNCLASSIFIED ranks as PERSONAL, not OPEN. An untiered
        # fragment is content nobody has vouched for, so the strict
        # admission cutoff must exclude it at the default ceiling.
        (PrivacyTier.UNCLASSIFIED, PrivacyTierOverride.OPEN, False),
        (PrivacyTier.UNCLASSIFIED, None, False),
        (PrivacyTier.UNCLASSIFIED, PrivacyTierOverride.PERSONAL, True),
        (PrivacyTier.UNCLASSIFIED, PrivacyTierOverride.ALL, True),
    ],
)
def test_tier_within_override(
    tier: PrivacyTier,
    override: PrivacyTierOverride | None,
    expected: bool,
) -> None:
    """The hard rank cutoff admits a tier iff it is at/below the override (#660)."""
    assert tier_within_override(tier, override) is expected


# ---------------------------------------------------------------------------
# Ancestry survey (issue #931)
# ---------------------------------------------------------------------------


def _node(
    frag_id: str,
    *,
    tier: PrivacyTier = PrivacyTier.OPEN,
    parent_id: str | None = None,
    structural_path: list[str] | None = None,
    omit_tier_key: bool = False,
) -> tuple[Fragment, dict[str, object]]:
    """Return the ``(fragment, raw)`` record shape the ancestry index consumes.

    Args:
        frag_id: Fragment id.
        tier: The fragment's declared privacy tier.
        parent_id: Optional link up the hierarchy.
        structural_path: Optional persisted breadcrumb.
        omit_tier_key: Drop ``privacy_tier`` from *raw* only, reproducing a
            hand-edited or legacy file — the one case
            :func:`creek.classify.privacy_filter.fragment_tier` distinguishes
            from an explicit ``unclassified``.

    Returns:
        A ``(Fragment, raw_frontmatter)`` pair.
    """
    fragment = Fragment(
        id=frag_id,
        title=f"Title {frag_id}",
        source=FragmentSource(
            platform=SourcePlatform.JOURNAL,
            author=Authorship.SELF,
        ),
        created=datetime(2026, 5, 1, tzinfo=UTC),
        privacy_tier=tier,
        parent_id=parent_id,
        structural_path=structural_path or [],
    )
    raw: dict[str, object] = {"id": frag_id, "type": "fragment"}
    if not omit_tier_key:
        raw["privacy_tier"] = tier.value
    return fragment, raw


def test_chain_tiers_emits_the_leaf_and_every_strict_ancestor() -> None:
    """Rule (a): a resolved id contributes its own tier and its whole chain."""
    index = build_ancestor_index(
        [
            _node("root", tier=PrivacyTier.INTIMATE),
            _node("mid", tier=PrivacyTier.PERSONAL, parent_id="root"),
            _node("leaf", tier=PrivacyTier.OPEN, parent_id="mid"),
        ],
    )
    assert set(index.chain_tiers(["leaf"])) == {
        PrivacyTier.OPEN,
        PrivacyTier.PERSONAL,
        PrivacyTier.INTIMATE,
    }


def test_chain_tiers_ignores_an_id_that_does_not_resolve() -> None:
    """Rule (b): an unresolved id contributes nothing, not ``INTIMATE``.

    Load-bearing, and the one rule here that is *not* fail-closed. The
    compile engine's ``ValueError("Fragment(s) not found in vault: ...")``
    must still reach a caller with a typo'd id; if the survey failed closed
    on unresolved ids, every typo would collapse into the content-free
    above-ceiling refusal and a legitimate client's bug would be
    undebuggable. Admission of the *rest* of the call is unaffected: nothing
    that is not in the vault can be rendered into a prompt.
    """
    index = build_ancestor_index([_node("leaf", tier=PrivacyTier.OPEN)])
    assert index.chain_tiers(["nope"]) == []
    assert index.chain_tiers(["leaf", "nope"]) == [PrivacyTier.OPEN]


def test_chain_tiers_fails_closed_on_an_unresolvable_parent() -> None:
    """Rule (c): a ``parent_id`` the index cannot resolve ranks ``INTIMATE``.

    A missing, unreadable, non-``fragment``-typed or schema-invalid parent is
    invisible to :func:`creek.vault.reader.try_load_fragment`, so its tier is
    unknowable while the child's breadcrumb still names it.
    """
    index = build_ancestor_index(
        [_node("leaf", tier=PrivacyTier.OPEN, parent_id="vanished")],
    )
    assert PrivacyTier.INTIMATE in index.chain_tiers(["leaf"])


def test_chain_tiers_terminates_and_fails_closed_on_a_cycle() -> None:
    """Rule (d): a ``parent_id`` cycle ranks ``INTIMATE`` and does not hang.

    A chain that cannot be fully surveyed fails closed. The mutual-parent
    pair below is the minimal case; a self-parent is the degenerate one.
    """
    index = build_ancestor_index(
        [
            _node("a", tier=PrivacyTier.OPEN, parent_id="b"),
            _node("b", tier=PrivacyTier.OPEN, parent_id="a"),
        ],
    )
    assert PrivacyTier.INTIMATE in index.chain_tiers(["a"])

    self_parent = build_ancestor_index(
        [_node("s", tier=PrivacyTier.OPEN, parent_id="s")],
    )
    assert PrivacyTier.INTIMATE in self_parent.chain_tiers(["s"])


def test_chain_tiers_fails_closed_on_an_orphan_breadcrumb() -> None:
    """Rule (e): a persisted breadcrumb with no ``parent_id`` ranks ``INTIMATE``.

    The breadcrumb is a ``list[str]`` with no id binding, so ancestry that
    can be *rendered* but not *walked* is ancestry that cannot be ranked.
    :func:`creek.atomize.split._build_children` is the only writer of the
    field and always sets ``parent_id`` in the same ``model_copy``, so this
    state is anomalous by construction.
    """
    index = build_ancestor_index(
        [_node("orphan", tier=PrivacyTier.OPEN, structural_path=["Ritual with M."])],
    )
    assert PrivacyTier.INTIMATE in index.chain_tiers(["orphan"])


def test_chain_tiers_fails_closed_on_a_breadcrumb_deeper_than_the_ancestry() -> None:
    """Rule (e), the general case: more headings than walkable ancestors.

    The survey ranks fragment *ids* up ``parent_id``; the prompt renders
    *strings* out of ``structural_path``. Nothing in the data binds the two,
    so a fragment re-parented onto a shallower ``open`` parent while keeping
    its deeper breadcrumb would pass rules (c), (d) and the depth-0 orphan
    case and still render headings from a chain nobody ranked. Comparing the
    counts is the only sound check, and it costs nothing:
    ``creek.atomize.split._build_children`` appends at most one heading per
    level, so ``len(structural_path)`` can never legitimately exceed the
    strict-ancestor count.
    """
    index = build_ancestor_index(
        [
            _node("root", tier=PrivacyTier.OPEN),
            _node(
                "leaf",
                tier=PrivacyTier.OPEN,
                parent_id="root",
                structural_path=["Deep", "Deeper", "Ritual with M."],
            ),
        ],
    )
    assert PrivacyTier.INTIMATE in index.chain_tiers(["leaf"])


def test_chain_tiers_admits_a_breadcrumb_matching_its_ancestry_depth() -> None:
    """Anti-vacuity for the depth rule: a well-formed breadcrumb escalates nothing.

    One heading, one strict ancestor — exactly what
    ``creek.atomize.split._build_children`` produces for a titled section of
    a document. If the depth check were off by one, every real split fragment
    in the vault would become uncompilable at cloud tiers and the test above
    would still pass.
    """
    index = build_ancestor_index(
        [
            _node("root", tier=PrivacyTier.OPEN),
            _node(
                "leaf",
                tier=PrivacyTier.OPEN,
                parent_id="root",
                structural_path=["A section heading"],
            ),
        ],
    )
    assert index.chain_tiers(["leaf"]) == [PrivacyTier.OPEN, PrivacyTier.OPEN]


def test_chain_tiers_fails_closed_on_a_duplicated_fragment_id() -> None:
    """Rule (h): two files claiming one id are unrankable, so ``INTIMATE``.

    The index is a mapping and a mapping is last-wins, while the
    :func:`source_tiers` it replaces at the compile gate yields one tier per
    *file* and so had no such hole. Without this rule a shadow file carrying
    an above-ceiling ancestor's id with ``privacy_tier: open`` and a later
    sort position would downgrade the real ancestor while the child's
    breadcrumb still rendered its heading — the one way this change could be
    *less* restrictive than what it replaced.
    """
    index = build_ancestor_index(
        [
            _node("frag-anc", tier=PrivacyTier.INTIMATE),
            _node("frag-anc", tier=PrivacyTier.OPEN),
            _node("leaf", tier=PrivacyTier.OPEN, parent_id="frag-anc"),
        ],
    )
    assert PrivacyTier.INTIMATE in index.chain_tiers(["leaf"])
    assert PrivacyTier.INTIMATE in index.chain_tiers(["frag-anc"])


def test_chain_tiers_leaves_a_breadcrumbless_root_alone() -> None:
    """Anti-vacuity for rule (e): no breadcrumb, no escalation.

    Without this row the orphan rule could be "every root fragment is
    intimate", which would refuse practically every compile at an ``open``
    ceiling and still pass the test above.
    """
    index = build_ancestor_index([_node("root", tier=PrivacyTier.OPEN)])
    assert index.chain_tiers(["root"]) == [PrivacyTier.OPEN]


def test_chain_tiers_reads_a_missing_privacy_tier_key_as_intimate() -> None:
    """Per-entry tiers come from ``fragment_tier``, so a missing key fails closed."""
    index = build_ancestor_index(
        [
            _node("root", tier=PrivacyTier.UNCLASSIFIED, omit_tier_key=True),
            _node("leaf", tier=PrivacyTier.OPEN, parent_id="root"),
        ],
    )
    assert PrivacyTier.INTIMATE in index.chain_tiers(["leaf"])


def test_chain_tiers_surveys_every_requested_id_before_answering() -> None:
    """Rules (f)+(g): the whole batch is surveyed, and shared chains are cheap.

    No short-circuit on the first offender: both leaves are ranked, so the
    cost of a probe does not depend on *where* in the batch (or how far up a
    chain) the above-ceiling ancestor sits. Sharing a memoised ancestor chain
    is what keeps that uniformity affordable on a deep, wide request.
    """
    index = build_ancestor_index(
        [
            _node("root", tier=PrivacyTier.INTIMATE),
            _node("l1", tier=PrivacyTier.OPEN, parent_id="root"),
            _node("l2", tier=PrivacyTier.PERSONAL, parent_id="root"),
        ],
    )
    tiers = set(index.chain_tiers(["l1", "l2"]))
    assert tiers == {PrivacyTier.OPEN, PrivacyTier.PERSONAL, PrivacyTier.INTIMATE}
    # Order of the request cannot change the answer.
    assert set(index.chain_tiers(["l2", "l1"])) == tiers


def test_ancestry_tiers_reads_the_vault_in_one_walk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The MCP entry point surveys ancestry with a single ``01-Fragments`` pass.

    ``iter_vault_fragments`` rglobs and parses every file under the root
    before returning, so a second pass is a real 35k-vault regression rather
    than a micro-optimisation.
    """
    root = tmp_path / "01-Fragments" / "Notes"
    root.mkdir(parents=True)
    for frag_id, tier, parent in (
        ("frag-anc", PrivacyTier.INTIMATE, None),
        ("frag-kid", PrivacyTier.OPEN, "frag-anc"),
    ):
        fragment, _raw = _node(frag_id, tier=tier, parent_id=parent)
        post = frontmatter.Post(content="body", **fragment.model_dump(mode="json"))
        (root / f"{frag_id}.md").write_text(frontmatter.dumps(post), encoding="utf-8")

    real = privacy_filter.iter_vault_fragments
    calls: list[Path] = []

    def _counting(
        walk_root: Path,
    ) -> list[tuple[Path, Fragment, str, dict[str, object]]]:
        """Record the walked root and delegate to the real loader."""
        calls.append(walk_root)
        return real(walk_root)

    monkeypatch.setattr(privacy_filter, "iter_vault_fragments", _counting)

    tiers = ancestry_tiers(tmp_path, ["frag-kid"])

    assert calls == [tmp_path / "01-Fragments"]
    assert PrivacyTier.INTIMATE in tiers


def test_source_tiers_stays_ancestry_blind(tmp_path: Path) -> None:
    """``source_tiers`` must NOT gain the ancestry term (#931).

    ``draft`` / ``journal`` / ``upload`` reduce over it and render no
    breadcrumb, so widening it would refuse calls that leak nothing — and
    silently change three tools this issue never analysed. The two surveys
    are deliberately two functions; this pins the difference so a future
    "de-duplication" has to argue with a test.
    """
    root = tmp_path / "01-Fragments" / "Notes"
    root.mkdir(parents=True)
    for frag_id, tier, parent in (
        ("frag-anc", PrivacyTier.INTIMATE, None),
        ("frag-kid", PrivacyTier.OPEN, "frag-anc"),
    ):
        fragment, _raw = _node(frag_id, tier=tier, parent_id=parent)
        post = frontmatter.Post(content="body", **fragment.model_dump(mode="json"))
        (root / f"{frag_id}.md").write_text(frontmatter.dumps(post), encoding="utf-8")

    assert source_tiers(tmp_path, ["frag-kid"]) == [PrivacyTier.OPEN]
    assert PrivacyTier.INTIMATE in ancestry_tiers(tmp_path, ["frag-kid"])
