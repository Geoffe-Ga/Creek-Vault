"""Containment for the ``creek/generate`` read walks (#1794, lane 1).

Six bespoke ``sorted(root.rglob("*.md"))`` walks in ``creek/generate`` carry no
containment guard, and ``creek/generate/state.py`` carries four twin walks over
the same corpora. Lane 1 takes the corpora whose containment policy is already
settled — ``10-Liminal`` (two readers made one indivisible unit by #1079) and
``01-Fragments`` (settled by #1373/#1789, with ``state._read_fragment_files``
the last unguarded straggler).

**The liminal site is a prompt-injection primitive, not an id echo.**
:class:`~creek.models.Fragment` constrains ``id`` neither by pattern nor by
length, so ``Fragment.model_validate`` accepts a MULTILINE id. The planted id
is interpolated verbatim into ``_seed_from_liminal``'s ``brief_description``,
and from there into a draft prompt's ``## Ask`` block *and* into a
``## Suggested questions`` bullet in ``State/latest.md`` — a durable vault
write, read back by every later run as its session-start context.

**Two shapes, not one.** A leaf link is closed by
:func:`creek._containment.escaping_child`. An escaping *directory* is not: with
``10-Liminal/Unnamed`` a symlink to an out-of-vault folder, ``rglob`` refuses to
descend it so the miner returns ``[]``, while ``_admitted_liminal_notes``
globs the folder it was handed and reads straight through — the #1079
divergence in a shape a leaf-only guard cannot reach. That one needs
:func:`creek._containment.named_path_escapes` on the folder itself.

**Three sites are deliberately NOT guarded here**, and the direction pins below
say why in assertions rather than prose: ``mining._load_essay_titles`` (its
records SUPPRESS, so a guard makes the miner emit MORE — the #1793 inversion),
and the thread/eddy prose sites, which are lane 2 and are held still here by
the routing pins.

**One corpus is read in BOTH directions, and that is the subtlest case in the
lane.** ``state._read_fragment_files`` feeds a LIST (the census, the drift
paths) and a REDUCTION (``derived_link_tiers``, whose result is a maximum).
Guarding it makes the first render less and the second render MORE — the same
#1793 inversion, in the same file as the ruling that names it, measured on a
rendered report at ``ceiling=open`` and ``ceiling=personal``. The guard stays
and the reduction is abandoned instead; the pins for that render the whole
report, because no loader assertion can see a title move.

**The structural tripwires here are hints, not guarantees, and their docstrings
now say so.** An AST scan can only ever name the spellings someone thought of:
an inline re-derivation via ``os.path.islink`` passed every one of them and
leaked. Each one is paired with a behavioural pin that asserts what a reader
RETURNS for a planted vault, which no spelling escapes.
"""

from __future__ import annotations

import ast
import logging
import os
import sys
from pathlib import Path

import pytest

from creek._containment import named_path_escapes
from creek.classify.privacy_filter import PrivacyTierOverride
from creek.generate import drafts, mining, state
from creek.generate.compile_routing import (
    COMPILE_GAPS_RELPATH,
    CompiledSources,
    empty_index,
)
from creek.generate.mining import (
    IdeaMiner,
    _load_liminal_fragments,
    _load_mining_snapshot,
)
from creek.generate.state import (
    EMPTY_PLACEHOLDER,
    UNEVALUATED_COUNT_SUFFIX,
    UNEVALUATED_NOTE,
    StateReportGenerator,
    _admitted_liminal_notes,
    _read_fragment_files,
)
from creek.generate.state_tiers import stamped_content_tier
from creek.models import Phase, PrivacyTier
from creek.vault import reader as reader_module

_CANARY_ID = "CANARY-1794-<<PLANTED>>"
"""Id of the note parked outside the vault and symlinked in.

Arbitrary attacker-supplied text: ``_validate_fragment`` enforces no relation
between a fragment's id and its filename stem, and ``Fragment.id`` carries no
charset constraint.
"""

_BODY_SENTINEL = "PLANTED_BODY_SENTINEL"
"""Body text of the planted note; must never appear in any log record."""

_CEILINGS: tuple[PrivacyTierOverride, ...] = (
    PrivacyTierOverride.OPEN,
    PrivacyTierOverride.PERSONAL,
    PrivacyTierOverride.INTIMATE,
    PrivacyTierOverride.ALL,
)

_PLANTED_NOTE = (
    "---\n"
    "type: fragment\n"
    f"id: {_CANARY_ID}\n"
    "title: escape\n"
    # ``source`` is a nested FragmentSource. A scalar here makes
    # _validate_fragment reject the file, and every assertion below would then
    # pass vacuously against a note that was never loadable in the first place.
    "source:\n"
    "  platform: journal\n"
    "created: 2026-01-01T00:00:00Z\n"
    "privacy_tier: open\n"
    "---\n\n"
    f"{_BODY_SENTINEL} about attunement and eddies\n"
)


def _note(frag_id: str, title: str, body: str = "attunement and eddies") -> str:
    """Return valid fragment frontmatter for an in-root note.

    Args:
        frag_id: The fragment's ``id``.
        title: The fragment's ``title``.
        body: Body text beneath the frontmatter fence.

    Returns:
        The complete markdown document.
    """
    return (
        "---\n"
        "type: fragment\n"
        f"id: {frag_id}\n"
        f"title: {title}\n"
        "source:\n"
        "  platform: journal\n"
        "created: 2026-01-01T00:00:00Z\n"
        "privacy_tier: open\n"
        "---\n\n"
        f"{body}\n"
    )


def _write(path: Path, text: str) -> Path:
    """Write *text* to *path*, creating parents.

    Args:
        path: Destination file.
        text: Contents.

    Returns:
        *path*, for chaining.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _plant_liminal_leaf(tmp_path: Path) -> tuple[Path, Path]:
    """Build a vault whose ``10-Liminal/Unnamed`` holds one escaping LEAF link.

    Also writes a genuine in-root note and an eddy, so the liminal-cross-eddy
    strategy fires and "the canary is absent" cannot be satisfied by a miner
    that mined nothing.

    Args:
        tmp_path: pytest's per-test temporary directory.

    Returns:
        ``(vault, link)`` — the vault root and the escaping link.
    """
    vault = tmp_path / "vault"
    planted = _write(tmp_path / "outside" / "planted.md", _PLANTED_NOTE)
    _write(vault / "10-Liminal" / "Unnamed" / "real.md", _note("real-1", "Real"))
    _write(
        vault / "03-Eddies" / "e.md",
        "---\ntype: eddy\nid: eddy-1\ntitle: attunement\n"
        "formed: 2026-01-01\ndescription: attunement and eddies\n---\n\nbody\n",
    )
    link = vault / "10-Liminal" / "Unnamed" / "escape.md"
    link.symlink_to(planted)

    assert link.is_symlink(), "the fixture did not create a symlink"
    liminal = str((vault / "10-Liminal").resolve())
    assert not os.path.realpath(link).startswith(liminal + os.sep), (
        "the planted link resolves INSIDE 10-Liminal, so it is a contained "
        "alias and nothing below is being tested."
    )
    return vault, link


def _plant_liminal_directory(tmp_path: Path) -> tuple[Path, Path]:
    """Build a vault whose ``10-Liminal/Unnamed`` IS a link to an outside folder.

    The shape a leaf-only guard cannot see: the walked entries are ordinary
    files, and the escape is the directory they were reached through.

    Args:
        tmp_path: pytest's per-test temporary directory.

    Returns:
        ``(vault, folder)`` — the vault root and the escaping subfolder link.
    """
    vault = tmp_path / "vault"
    (vault / "10-Liminal").mkdir(parents=True)
    outside = tmp_path / "outside-dir"
    _write(outside / "planted.md", _PLANTED_NOTE)
    folder = vault / "10-Liminal" / "Unnamed"
    folder.symlink_to(outside, target_is_directory=True)

    assert folder.is_symlink(), "the fixture did not create a directory symlink"
    assert named_path_escapes(folder), (
        "the linked subfolder does not escape its own parent, so this fixture "
        "is not the escaping-directory case."
    )
    return vault, folder


def _seeds(vault: Path) -> list[mining.IdeaSeed]:
    """Return liminal-cross-eddy seeds mined from *vault*.

    The similarity function is pinned to ``1.0`` so the strategy's threshold
    can never be the reason a seed is absent.

    Args:
        vault: The vault root.

    Returns:
        Every seed the strategy emits.
    """
    miner = IdeaMiner(similarity_fn=lambda _a, _b: 1.0, bypass_compiled=True)
    return miner.mine_liminal_cross_eddy(vault)


# ---------------------------------------------------------------------------
# STEP 1 — the mining half: loader, snapshot, ## Ask, State/latest.md
# ---------------------------------------------------------------------------


def test_the_liminal_loader_skips_a_note_symlinked_out_of_the_liminal_root(
    tmp_path: Path,
) -> None:
    """RED. The headline: the planted id must not enter the corpus.

    Args:
        tmp_path: pytest's per-test temporary directory.
    """
    vault, _link = _plant_liminal_leaf(tmp_path)

    ids = [f.id for f, _body, _kind in _load_liminal_fragments(vault / "10-Liminal")]

    assert "real-1" in ids, f"the genuine note was not loaded either.\n\n{ids}"
    assert _CANARY_ID not in ids, (
        "a 10-Liminal note that is a symlink to a file outside that root "
        f"entered the mining corpus.\n\n{ids}"
    )


def test_the_mining_snapshot_excludes_the_planted_liminal_note(
    tmp_path: Path,
) -> None:
    """RED. The snapshot every strategy reads is narrowed too, not just the loader.

    Args:
        tmp_path: pytest's per-test temporary directory.
    """
    vault, _link = _plant_liminal_leaf(tmp_path)

    snapshot = _load_mining_snapshot(vault, bypass_compiled=True)

    ids = [f.id for f, _body, _kind in snapshot.liminal_fragments]
    assert "real-1" in ids, f"the snapshot holds no liminal notes at all.\n\n{ids}"
    assert _CANARY_ID not in ids, (
        f"the planted note reached MiningSnapshot.liminal_fragments.\n\n{ids}"
    )


def test_the_planted_id_never_reaches_a_composed_ask_block(tmp_path: Path) -> None:
    """RED. The injection, at the surface that matters: ``## Ask``.

    Asserted on the composed prompt rather than on a loader's return value.
    The planted id is attacker-chosen free text with no length or charset
    constraint, so it carries whatever it likes — up to and including its own
    ``## Ask`` header — into the block that instructs the model.

    Args:
        tmp_path: pytest's per-test temporary directory.
    """
    vault, _link = _plant_liminal_leaf(tmp_path)

    seeds = _seeds(vault)
    asks = [drafts._compose_ask_section(seed, per_dimension=False) for seed in seeds]

    assert asks, "no seed was mined, so the assertion below is vacuous."
    assert not any(_CANARY_ID in ask for ask in asks), (
        "the planted note's id was interpolated into a draft prompt's ## Ask "
        f"block.\n\n{asks}"
    )


def test_the_planted_id_never_reaches_a_suggested_questions_bullet(
    tmp_path: Path,
) -> None:
    """RED. The DURABLE reach: ``State/latest.md``, not just an ephemeral prompt.

    ``phase_filtered_seeds`` + ``_seed_as_prompt`` render the seed's
    ``brief_description`` as a ``## Suggested questions`` bullet, and that file
    is the documented session-start context every later run reads back.

    Args:
        tmp_path: pytest's per-test temporary directory.
    """
    vault, _link = _plant_liminal_leaf(tmp_path)
    miner = IdeaMiner(similarity_fn=lambda _a, _b: 1.0, bypass_compiled=True)
    snapshot = _load_mining_snapshot(vault, bypass_compiled=True)

    bullets = [
        state._seed_as_prompt(seed)
        for seed in mining.phase_filtered_seeds(
            vault,
            Phase.RESTORATION,
            miner=miner,
            snapshot=snapshot,
        )
    ]

    assert bullets, "no seed survived phase filtering, so this is vacuous."
    assert not any(_CANARY_ID in bullet for bullet in bullets), (
        "the planted id would be written into State/latest.md, where every "
        f"later run reads it back.\n\n{bullets}"
    )


# ---------------------------------------------------------------------------
# STEP 2 — the state half, including the escaping-DIRECTORY divergence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ceiling", _CEILINGS)
def test_the_state_reader_skips_the_escaping_leaf_at_every_ceiling(
    tmp_path: Path,
    ceiling: PrivacyTierOverride,
) -> None:
    """RED. The planted note declares ``open``, so only containment can drop it.

    Args:
        tmp_path: pytest's per-test temporary directory.
        ceiling: The admission ceiling.
    """
    vault, _link = _plant_liminal_leaf(tmp_path)

    admitted = _admitted_liminal_notes(
        vault / "10-Liminal" / "Unnamed",
        ceiling,
        liminal_root=vault / "10-Liminal",
    )

    stems = [stem for stem, _tier in admitted]
    assert "real" in stems, f"nothing was admitted at {ceiling.value!r}.\n\n{stems}"
    assert "escape" not in stems, (
        "state._admitted_liminal_notes admitted a note whose file links out of "
        "10-Liminal. Its p.is_file() filter does not screen it: is_file() "
        f"FOLLOWS the link.\n\n{stems}"
    )


@pytest.mark.parametrize("ceiling", _CEILINGS)
def test_an_out_of_vault_linked_subfolder_is_refused_whole(
    tmp_path: Path,
    ceiling: PrivacyTierOverride,
) -> None:
    """RED. The escaping-DIRECTORY divergence a leaf guard cannot close.

    Measured at the parent commit with ``10-Liminal/Unnamed`` a symlink to an
    out-of-vault directory holding ``planted.md``:

    * ``mining._load_liminal_fragments`` returns ``[]`` — ``rglob`` will not
      descend a symlinked directory, so the miner never sees the note.
    * ``state._admitted_liminal_notes`` returns ``[('planted', OPEN)]`` — it
      globs the folder it was handed and reads straight through the link.

    That is the #1079 "two tools, one file, two answers" divergence in a shape
    the leaf predicate cannot reach, because every entry the glob yields is an
    ordinary file and the escape is the directory they were reached through.

    Args:
        tmp_path: pytest's per-test temporary directory.
        ceiling: The admission ceiling.
    """
    vault, folder = _plant_liminal_directory(tmp_path)
    liminal_root = vault / "10-Liminal"

    mined = _load_liminal_fragments(liminal_root, privacy_override=ceiling)
    admitted = _admitted_liminal_notes(folder, ceiling, liminal_root=liminal_root)

    assert mined == [], (
        "the miner reached through a symlinked directory, which would make "
        f"the divergence this test pins the other way round.\n\n{mined}"
    )
    assert admitted == [], (
        "state._admitted_liminal_notes read through a subfolder that is itself "
        "a symlink out of the vault, while the miner returned []. One document "
        f"disagreeing with itself about one folder.\n\n{admitted}"
    )


def test_a_subfolder_linked_inside_the_liminal_tree_is_still_read(
    tmp_path: Path,
) -> None:
    """NON-VACUITY ANCHOR. The refusal is ``named_path_escapes``, not ``is_symlink``.

    The tempting simplification — ``if folder.is_symlink(): return []`` —
    manufactures a NEW divergence rather than closing one. Measured with
    ``10-Liminal/Unnamed -> 10-Liminal/Archive``: the miner reaches the real
    directory by its own name and returns the note, so a state reader that
    dropped the whole folder would disagree with it in the opposite direction,
    and a legitimate in-root relocation would silently empty the Liminal Watch.

    Args:
        tmp_path: pytest's per-test temporary directory.
    """
    vault = tmp_path / "vault"
    liminal_root = vault / "10-Liminal"
    _write(liminal_root / "Archive" / "moved.md", _note("intra-1", "Moved"))
    folder = liminal_root / "Unnamed"
    folder.symlink_to(liminal_root / "Archive", target_is_directory=True)

    assert folder.is_symlink(), "the fixture did not create a directory symlink"
    assert not named_path_escapes(folder), (
        "the linked subfolder escapes its parent, so this is not the in-root "
        "case the anchor is about."
    )
    mined = [f.id for f, _b, _k in _load_liminal_fragments(liminal_root)]
    assert "intra-1" in mined, (
        "the miner does not reach the relocated note, so the two readers would "
        f"agree on empty and this anchor proves nothing.\n\n{mined}"
    )

    admitted = _admitted_liminal_notes(
        folder,
        PrivacyTierOverride.ALL,
        liminal_root=liminal_root,
    )

    assert [stem for stem, _tier in admitted] == ["moved"], (
        "a subfolder linked to a target INSIDE 10-Liminal was dropped. The "
        "refusal must be named_path_escapes(folder), never a blanket "
        f"is_symlink() — the miner still reads it.\n\n{admitted}"
    )


def test_a_sibling_folder_whose_name_extends_the_liminal_root_is_refused(
    tmp_path: Path,
) -> None:
    """BEHAVIOURAL PIN against a re-derived containment predicate, any spelling.

    The structural scans in this file and in
    ``tests/test_liminal_tier_reader_agreement.py`` catch exactly two
    spellings: an ``ast.Attribute`` call named ``is_symlink``, and the literal
    strings ``escaping_child(`` / ``resolves_within(`` / ``.is_symlink()``. An
    inline re-derivation written any other way passes both — measured, with

        os.path.islink(folder) and not str(folder.resolve()).startswith(
            str(folder.parent.resolve())
        )

    which survived every test in this lane AND leaked. This vault is the shape
    that separates the two rules: ``10-Liminal-secrets`` is a sibling of
    ``10-Liminal`` whose path string STARTS WITH it, so a prefix comparison
    answers "contained" while ``relative_to`` answers "escaped". The
    precondition below asserts that separation, so this pin cannot decay into
    a restatement of the shipped rule.

    Asserting the reader's OUTPUT rather than which call it made is the whole
    point: no scan can enumerate the spellings of a predicate, but every
    spelling has to produce an answer, and a wrong one is visible here.

    Args:
        tmp_path: pytest's per-test temporary directory.
    """
    vault = tmp_path / "vault"
    liminal_root = vault / "10-Liminal"
    liminal_root.mkdir(parents=True)
    outside = vault / "10-Liminal-secrets"
    _write(outside / "Planted.md", _PLANTED_NOTE)
    folder = liminal_root / "Unnamed"
    folder.symlink_to(outside, target_is_directory=True)

    resolved = os.path.realpath(folder)
    assert resolved.startswith(str(liminal_root.resolve())), (
        "the sibling folder's path does not share 10-Liminal's prefix, so a "
        "startswith-based re-derivation would refuse it anyway and this pin "
        f"is vacuous.\n\n{resolved}"
    )
    assert named_path_escapes(folder), (
        "the shipped predicate considers this folder contained, so the "
        "fixture is not the escaping case."
    )

    admitted = _admitted_liminal_notes(
        folder,
        PrivacyTierOverride.ALL,
        liminal_root=liminal_root,
    )

    assert admitted == [], (
        "the state reader admitted a note from a folder that resolves OUTSIDE "
        "10-Liminal, while the miner returns nothing for the same tree — the "
        "#1079 divergence in its permissive direction. Containment has been "
        "re-derived somewhere with a prefix comparison instead of "
        f"creek._containment.\n\n{admitted}"
    )


def test_a_nested_liminal_subfolder_is_judged_against_the_whole_tree(
    tmp_path: Path,
) -> None:
    """The ``liminal_root`` parameter, made load-bearing rather than argued for.

    ``_admitted_liminal_notes`` takes ``liminal_root`` as a required keyword
    instead of deriving it from ``folder.parent``, because the root has to be
    the whole tree the miner walks. Today ``_LIMINAL_SUBDIRS`` is flat, so at
    the single production call site the two coincide and the mutant
    ``iter_contained(folder.parent, ...)`` is equivalent — it survives 301
    tests, measured.

    This is the case that separates them: the alias sits one level deeper than
    a production subfolder, and its target is a SIBLING subfolder. Judged
    against the whole tree it is contained; judged against ``folder.parent``
    it escapes and is dropped, while the miner — which rglobs
    ``10-Liminal`` — keeps it. That is the #1079 divergence, reached by
    nesting rather than by symlinking.

    Args:
        tmp_path: pytest's per-test temporary directory.
    """
    vault = tmp_path / "vault"
    liminal_root = vault / "10-Liminal"
    target = _write(liminal_root / "Paradoxes" / "Target.md", _note("nest-1", "T"))
    folder = liminal_root / "Unnamed" / "sub"
    folder.mkdir(parents=True)
    link = folder / "NestedAlias.md"
    link.symlink_to(target)

    assert link.is_symlink(), "the fixture did not create a symlink"
    subfolder_root = str(folder.parent.resolve()) + os.sep
    assert not os.path.realpath(link).startswith(subfolder_root), (
        "the alias target resolves inside folder.parent, so judging against "
        "folder.parent would admit it too and this pin cannot separate the "
        "two roots."
    )
    mined = [f.id for f, _b, _k in _load_liminal_fragments(liminal_root)]
    assert "nest-1" in mined, (
        f"the miner does not reach the nested alias, so the two readers would "
        f"agree on empty and this pin proves nothing.\n\n{mined}"
    )

    admitted = _admitted_liminal_notes(
        folder,
        PrivacyTierOverride.ALL,
        liminal_root=liminal_root,
    )

    assert [stem for stem, _tier in admitted] == ["NestedAlias"], (
        "a nested alias into a sibling liminal subfolder was dropped, so "
        "containment is being judged against the subfolder rather than the "
        "10-Liminal tree the miner walks. liminal_root is not being "
        f"honoured.\n\n{admitted}"
    )


def test_a_skipped_liminal_note_loses_its_stem_and_its_tier_together(
    tmp_path: Path,
) -> None:
    """A guard must not lower the stamp while leaving derived content behind.

    ``_admitted_liminal_notes`` returns ``(stem, tier)`` pairs and
    ``_load_liminal_watch`` splits them into the rendered stems and the tiers
    that feed ``_content_tier``'s stamp. Dropping a note from one list but not
    the other would under-report the artifact's tier — a read gate failing
    open — so the two must move together by construction.

    Args:
        tmp_path: pytest's per-test temporary directory.
    """
    vault, _link = _plant_liminal_leaf(tmp_path)
    _write(
        vault / "10-Liminal" / "Unnamed" / "hot.md",
        _note("hot-1", "Hot").replace("privacy_tier: open", "privacy_tier: intimate"),
    )

    admitted = _admitted_liminal_notes(
        vault / "10-Liminal" / "Unnamed",
        PrivacyTierOverride.ALL,
        liminal_root=vault / "10-Liminal",
    )

    stems = [stem for stem, _tier in admitted]
    tiers = [tier for _stem, tier in admitted]
    assert len(stems) == len(tiers) == 2, f"expected two admitted notes.\n\n{admitted}"
    assert "escape" not in stems, "the planted note kept its stem."
    assert PrivacyTier.INTIMATE in tiers, (
        "the surviving intimate note lost its tier, which would under-report "
        f"the artifact stamp the read gate trusts.\n\n{admitted}"
    )


# ---------------------------------------------------------------------------
# STEP 3 — the 01-Fragments straggler in state.py
# ---------------------------------------------------------------------------


def test_the_state_fragment_census_skips_an_escaping_fragment(
    tmp_path: Path,
) -> None:
    """RED. One rendered report must not disagree with itself.

    ``creek.vault.reader.iter_vault_fragments`` has guarded ``01-Fragments``
    since #1373, but ``state._read_fragment_files`` walks the same root with
    its own unguarded ``rglob`` — so the same document logs a containment skip
    on one half and counts the planted fragment on the other.

    Args:
        tmp_path: pytest's per-test temporary directory.
    """
    vault = tmp_path / "vault"
    (vault / "01-Fragments").mkdir(parents=True)
    planted = _write(tmp_path / "outside" / "planted.md", _PLANTED_NOTE)
    _write(vault / "01-Fragments" / "real.md", _note("frag-1", "Real"))
    (vault / "01-Fragments" / "escape.md").symlink_to(planted)

    files = _read_fragment_files(vault / "01-Fragments")
    loaded = [f.id for _p, f, _raw in files.records]

    assert "frag-1" in loaded, f"no fragment was read at all.\n\n{loaded}"
    assert _CANARY_ID not in loaded, (
        "state._read_fragment_files counted a fragment symlinked in from "
        "outside the vault, while iter_vault_fragments over the same root "
        f"skips it.\n\n{loaded}"
    )


def test_the_state_fragment_census_still_reads_an_intra_root_alias(
    tmp_path: Path,
) -> None:
    """NON-VACUITY ANCHOR for the fragment straggler.

    The alias target carries a suffix ``*.md`` cannot match, so the link is the
    record's only route and the anchor survives an ``is_symlink() -> skip``
    mutation — unlike an alias placed beside the ``.md`` file it aliases, which
    the walk finds anyway.

    Args:
        tmp_path: pytest's per-test temporary directory.
    """
    vault = tmp_path / "vault"
    fragments = vault / "01-Fragments"
    target = _write(fragments / "aliased.markdown", _note("alias-1", "Aliased"))
    link = fragments / "alias.md"
    link.symlink_to(target)

    assert link.is_symlink(), "the fixture did not create a symlink"
    assert target not in set(fragments.rglob("*.md")), (
        "the alias target is reachable by the walk's own glob, so this anchor "
        "is vacuous against an 'is_symlink() -> skip' mutation."
    )

    loaded = [f.id for _p, f, _raw in _read_fragment_files(fragments).records]

    assert "alias-1" in loaded, (
        "a contained intra-root alias was dropped. Containment is about the "
        f"target leaving the root, not about the link existing.\n\n{loaded}"
    )


_EDDY_CANARY = "EddyTitleCanary"
_THREAD_CANARY = "ThreadTitleCanary"


def _linked_note(frag_id: str, tier: str) -> str:
    """Return a fragment naming both canary titles at *tier*.

    Args:
        frag_id: The fragment's ``id``.
        tier: The ``privacy_tier`` value written into the frontmatter.

    Returns:
        The complete markdown document.
    """
    return (
        "---\n"
        "type: fragment\n"
        f"id: {frag_id}\n"
        f"title: {frag_id}\n"
        "source:\n"
        "  platform: journal\n"
        "created: 2026-01-01T00:00:00Z\n"
        f"privacy_tier: {tier}\n"
        "eddies:\n"
        f"  - '[[{_EDDY_CANARY}]]'\n"
        "threads:\n"
        f"  - '[[{_THREAD_CANARY}]]'\n"
        "---\n\nbody\n"
    )


def _mixed_membership_vault(tmp_path: Path, *, escaping: bool) -> Path:
    """Build a vault whose eddy and thread have one ``open`` member.

    When *escaping* is set, a SECOND member is added at ``intimate`` and
    reached only through a symlink out of the vault — the realistic mixed
    case. The boundary where the escaping link is the *only* member is not
    this shape and does not reproduce anything:
    ``max_source_tier([])`` fails closed to ``INTIMATE`` and every revision
    withholds the title.

    Args:
        tmp_path: pytest's per-test temporary directory.
        escaping: Whether to add the escaping ``intimate`` member.

    Returns:
        The vault root.
    """
    vault = tmp_path / "vault"
    _write(
        vault / "03-Eddies" / "e.md",
        "---\ntype: eddy\nid: eddy-1\n"
        f"title: {_EDDY_CANARY}\n"
        "formed: 2026-01-01\ndescription: d\n---\n\nbody\n",
    )
    _write(
        vault / "02-Threads" / "t.md",
        "---\ntype: thread\nid: thread-1\n"
        f"title: {_THREAD_CANARY}\n"
        "started: 2026-01-01\nlast_seen: 2026-09-09\n"
        "fragment_count: 2\nstatus: active\n---\n\nbody\n",
    )
    _write(vault / "01-Fragments" / "Notes" / "open.md", _linked_note("open-1", "open"))
    if escaping:
        target = _write(
            tmp_path / "outside" / "secret.md",
            _linked_note("secret-1", "intimate"),
        )
        link = vault / "01-Fragments" / "Notes" / "alias.md"
        link.symlink_to(target)
        assert link.is_symlink(), "the fixture did not create a symlink"
    return vault


@pytest.mark.parametrize(
    "ceiling",
    [PrivacyTierOverride.OPEN, PrivacyTierOverride.PERSONAL],
)
def test_an_escaping_member_never_lowers_a_rendered_title_past_the_ceiling(
    tmp_path: Path,
    ceiling: PrivacyTierOverride,
) -> None:
    """BLOCKER PIN. Guarding a REDUCED-OVER corpus inverts direction (#1793).

    ``_read_fragment_files`` is read twice by ``_load_fragments_admitted``: as
    a LIST (census, drift), where dropping a record renders less, and as the
    input to ``derived_link_tiers``, whose result is a MAXIMUM, where dropping
    a record renders MORE. Round 2 guarded the walk and measured only the
    first. Measured on the rendered report, the second half moved the other
    way::

        - Eddies: 0                ->  - Eddies: 1
        ## Active eddies
        _No surfacing this week._  ->  - EddyTitleCanary — 1 fragment(s)

    at ``ceiling=open`` AND ``ceiling=personal`` — the two strictest surfaces,
    ``open`` being the MCP default in ``creek_mcp.tools.state``. That is
    #969's leak (3) reopened by a guard.

    This renders the whole report rather than asserting on a loader, because
    no loader assertion can see it: the census half is correct in both
    revisions and it is the *titles* that move.

    Args:
        tmp_path: pytest's per-test temporary directory.
        ceiling: The admission ceiling to render under.
    """
    vault = _mixed_membership_vault(tmp_path, escaping=True)

    report = StateReportGenerator(vault, override=ceiling).render()

    assert _EDDY_CANARY not in report, (
        "an eddy title rendered at ceiling="
        f"{ceiling.value!r} whose membership includes an intimate fragment. "
        "The containment guard dropped that member, which LOWERED the derived "
        "maximum — the #1793 inversion, measured at the consumer."
    )
    assert _THREAD_CANARY not in report, (
        f"a thread title rendered at ceiling={ceiling.value!r} for the same "
        "reason as the eddy above."
    )


@pytest.mark.parametrize("escaping", [False, True])
def test_the_open_report_renders_a_title_only_when_nothing_was_skipped(
    tmp_path: Path,
    escaping: bool,
) -> None:
    """POSITIVE CONTROL, both halves of the same fixture.

    The ``escaping=False`` half proves the pipeline does render these titles
    at ``ceiling=open``, so their absence in the ``escaping=True`` half is
    evidence rather than an artefact of an empty report.

    Args:
        tmp_path: pytest's per-test temporary directory.
        escaping: Whether the fixture plants the escaping intimate member.
    """
    vault = _mixed_membership_vault(tmp_path, escaping=escaping)

    report = StateReportGenerator(vault, override=PrivacyTierOverride.OPEN).render()

    assert (_EDDY_CANARY in report) is not escaping, (
        "with no escaping member the eddy title must render at ceiling=open "
        "(all evidence is open); with one it must not. Rendered="
        f"{_EDDY_CANARY in report}, escaping={escaping}."
    )


def test_an_unevaluable_section_says_so_rather_than_rendering_silence(
    tmp_path: Path,
) -> None:
    """Silence must mean "evaluated and found nothing", never "could not tell".

    Failing the derived-tier sections closed is only half the answer. A
    section that renders :data:`EMPTY_PLACEHOLDER` when the run could not
    evaluate it is a confident zero: two runs over the same vault produce
    byte-identical silence for two different reasons, and the operator has no
    way to tell a quiet week from a skipped fragment. The WARNING in the log
    is not a substitute — the report is the durable artifact, read back by
    every later run as session-start context.

    Args:
        tmp_path: pytest's per-test temporary directory.
    """
    vault = _mixed_membership_vault(tmp_path, escaping=True)

    report = StateReportGenerator(vault, override=PrivacyTierOverride.OPEN).render()
    eddies_body = report.split("## Active eddies\n\n", 1)[1].split("\n\n##", 1)[0]

    assert UNEVALUATED_NOTE in report, (
        "the report withheld the titles without saying why, so its silence "
        "claims complete evidence it does not have."
    )
    assert eddies_body.strip() != EMPTY_PLACEHOLDER, (
        "## Active eddies rendered the ordinary empty-state placeholder for a "
        f"run that could not evaluate it.\n\n{eddies_body}"
    )


def test_a_quiet_vault_still_renders_the_ordinary_empty_placeholder(
    tmp_path: Path,
) -> None:
    """NON-VACUITY ANCHOR for the note above: the two states stay distinct.

    A note that appeared on every report would be worthless. With nothing
    skipped, the sections must render the FEAT-006 placeholder unchanged.

    Args:
        tmp_path: pytest's per-test temporary directory.
    """
    vault = tmp_path / "vault"
    (vault / "01-Fragments").mkdir(parents=True)

    report = StateReportGenerator(vault, override=PrivacyTierOverride.OPEN).render()

    assert UNEVALUATED_NOTE not in report, (
        "a vault with no containment skip at all reported its derived-tier "
        "sections as unevaluable, so the note carries no information."
    )
    assert EMPTY_PLACEHOLDER in report, (
        "the ordinary empty-state placeholder disappeared from a quiet vault."
    )


def test_an_unevaluable_run_stays_recoverable_at_the_narrow_ceiling(
    tmp_path: Path,
) -> None:
    """The fail-closed sections must not make the artifact unreadable for good.

    At ``ceiling=all`` the withheld-but-rendered titles carry the fail-closed
    ``INTIMATE``, so the stamp rises to ``intimate`` and ``creek.state.read``
    refuses the report at its default ``ceiling=open``. Under-stamping would be
    a read gate failing open over content nobody vouched for, so the stamp is
    right and the outage has to be the recoverable kind #969 requires: one
    re-render at the narrow ceiling withholds the titles and stamps ``open``
    again. This pins that recovery, which is the whole reason the coarse rule
    is acceptable.

    Args:
        tmp_path: pytest's per-test temporary directory.
    """
    vault = _mixed_membership_vault(tmp_path, escaping=True)

    # Both renders write the same ISO-week file, so each is read back before
    # the next overwrites it.
    broad_gen = StateReportGenerator(vault, override=PrivacyTierOverride.ALL)
    broad = broad_gen.write().read_text(encoding="utf-8")
    narrow_gen = StateReportGenerator(vault, override=PrivacyTierOverride.OPEN)
    narrow = narrow_gen.write().read_text(encoding="utf-8")

    assert stamped_content_tier(broad) is PrivacyTier.INTIMATE, (
        "the broad render stamped below intimate while rendering titles whose "
        "tier evidence was incomplete, so the read gate fails open."
    )
    assert stamped_content_tier(narrow) is PrivacyTier.OPEN, (
        "re-rendering at ceiling=open did not recover a readable artifact, so "
        "one stray symlink locks the operator out of their own state report "
        "until they widen every reader's ceiling."
    )


def test_the_census_declares_an_unevaluated_count_instead_of_a_bare_zero(
    tmp_path: Path,
) -> None:
    """A zero is an assertion, and this run cannot make it.

    ``- Eddies: 0`` reads as "this vault surfaced no eddies". On a run whose
    derived-tier gate could not be evaluated the honest statement is different,
    and the two must not print identically — the confident-zero defect the
    #1769 alarms lane produced two majors on, one section over. The neighbouring
    sections carrying an explanation is mitigation, not correctness.

    The fragment count deliberately does NOT take the suffix: that is the
    listing half, where excluding a fragment that links out of the root is a
    complete answer rather than an unevaluated one. Asserting its absence is
    what keeps the suffix informative rather than decorative.

    Args:
        tmp_path: pytest's per-test temporary directory.
    """
    vault = _mixed_membership_vault(tmp_path, escaping=True)

    report = StateReportGenerator(vault, override=PrivacyTierOverride.OPEN).render()
    summary = report.split("## Vault summary\n\n", 1)[1].split("\n\n", 1)[0]

    assert f"- Eddies: 0{UNEVALUATED_COUNT_SUFFIX}" in summary, (
        "the eddy census printed a bare zero for a run that could not "
        f"evaluate the gate that zero came through.\n\n{summary}"
    )
    assert f"- Threads: 0{UNEVALUATED_COUNT_SUFFIX}" in summary, (
        f"the thread census printed a bare zero for the same reason.\n\n{summary}"
    )
    assert "- Fragments: 1\n" in summary + "\n", (
        "the fragment count took the unevaluated suffix. The listing half is "
        f"a complete answer and marking it dilutes the marker.\n\n{summary}"
    )


def test_a_quiet_vault_census_still_prints_a_bare_zero(tmp_path: Path) -> None:
    """NON-VACUITY ANCHOR for the census marker: the two states stay distinct.

    A suffix on every report would carry no information at all.

    Args:
        tmp_path: pytest's per-test temporary directory.
    """
    vault = tmp_path / "vault"
    (vault / "01-Fragments").mkdir(parents=True)

    report = StateReportGenerator(vault, override=PrivacyTierOverride.OPEN).render()

    assert "- Eddies: 0\n" in report, (
        "a vault with no containment skip lost its plain census count."
    )
    assert UNEVALUATED_COUNT_SUFFIX not in report, (
        "a vault with no containment skip reported its census as unevaluated, "
        "so the marker says nothing."
    )


def test_an_eddy_named_only_by_the_escaping_fragment_never_renders(
    tmp_path: Path,
) -> None:
    """The skipped file must never become the FIRST voucher for a vault title.

    This is the leak the "just keep the tier evidence" repair opens, and the
    reason it is refused is the EMPTY case rather than the maximum. Adding a
    contributor to a max cannot lower it — ``max_source_tier([intimate, open,
    open, open])`` is ``intimate`` — so a planted ``open`` cannot demote an
    eddy the vault already vouches for. But ``max_source_tier([])`` answers
    ``INTIMATE`` *by policy*, and the first contributor replaces that floor
    with whatever it declares. Measured on this vault: feeding the skipped
    file's ``privacy_tier: open`` into the reduction moves ``LonelyEddy``
    ``intimate -> open`` and renders its title at ``ceiling=open``.

    Nothing in-root names ``LonelyEddy``, so the vault's own evidence about it
    is empty and the correct answer at every ceiling below ``intimate`` is to
    withhold. A file outside ``01-Fragments`` must not be able to change that.

    Args:
        tmp_path: pytest's per-test temporary directory.
    """
    vault = tmp_path / "vault"
    _write(
        vault / "03-Eddies" / "lonely.md",
        "---\ntype: eddy\nid: eddy-9\ntitle: LonelyEddy\n"
        "formed: 2026-01-01\ndescription: d\n---\n\nbody\n",
    )
    _write(vault / "01-Fragments" / "open.md", _linked_note("open-1", "open"))
    target = _write(
        tmp_path / "outside" / "planted.md",
        _linked_note("planted-1", "open").replace(
            f"[[{_EDDY_CANARY}]]",
            "[[LonelyEddy]]",
        ),
    )
    link = vault / "01-Fragments" / "alias.md"
    link.symlink_to(target)

    assert link.is_symlink(), "the fixture did not create a symlink"
    assert "LonelyEddy" not in (vault / "01-Fragments" / "open.md").read_text(
        encoding="utf-8",
    ), (
        "an in-root fragment names LonelyEddy, so the vault vouches for it "
        "anyway and this pin cannot see the empty-case escape."
    )

    report = StateReportGenerator(vault, override=PrivacyTierOverride.OPEN).render()

    assert "LonelyEddy" not in report, (
        "an eddy whose ONLY voucher is a fragment symlinked in from outside "
        "01-Fragments rendered its title at ceiling=open. The skipped file is "
        "being read back for its tier, which lets out-of-root content replace "
        "max_source_tier's fail-closed empty case."
    )


def test_the_rendered_census_still_drops_the_escaping_fragment(
    tmp_path: Path,
) -> None:
    """The guard's BENEFIT half, pinned at the render rather than the loader.

    The fix for the inversion above is a split, not a revert, and this is the
    half that must survive it: at ``ceiling=all`` the unguarded read counted
    the out-of-root fragment in the census and could list its slugified
    filename under drift warnings.

    Args:
        tmp_path: pytest's per-test temporary directory.
    """
    vault = _mixed_membership_vault(tmp_path, escaping=True)

    report = StateReportGenerator(vault, override=PrivacyTierOverride.ALL).render()

    assert "- Fragments: 1" in report, (
        "the census counted a fragment symlinked in from outside the vault, "
        f"so the containment guard has been reverted rather than split.\n\n{report}"
    )
    assert "alias" not in report, (
        f"the escaping fragment's filename reached the rendered report.\n\n{report}"
    )


# ---------------------------------------------------------------------------
# STEP 4 — the mandated liminal anchor, asserted on BOTH readers
# ---------------------------------------------------------------------------


def test_a_contained_liminal_alias_still_loads_through_both_readers(
    tmp_path: Path,
) -> None:
    """NON-VACUITY ANCHOR. One alias, both readers, and the link is its only route.

    ``10-Liminal/Unnamed/alias.md -> 10-Liminal/Synchronicities/aliased.markdown``
    is unreachable by either walk on its own: the ``.markdown`` suffix the
    ``*.md`` glob misses, under a ``Synchronicities`` parent that
    ``mining._liminal_kind`` returns ``None`` for. So the record exists only
    because the link does, and an ``if md_file.is_symlink(): continue``
    mutation deletes it from both sides.

    The state-side half is also the pin on the containment ROOT: judged against
    ``10-Liminal/Unnamed`` the alias escapes and is dropped; judged against
    ``10-Liminal`` — the root the miner uses — it is contained and kept.

    Args:
        tmp_path: pytest's per-test temporary directory.
    """
    vault = tmp_path / "vault"
    liminal_root = vault / "10-Liminal"
    target = _write(
        liminal_root / "Synchronicities" / "aliased.markdown",
        _note("LIM-ALIAS", "alias"),
    )
    link = liminal_root / "Unnamed" / "alias.md"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target)

    assert link.is_symlink(), "the fixture did not create a symlink"
    assert target not in set(liminal_root.rglob("*.md")), (
        "the alias target is reachable by the miner's own glob, so this anchor "
        "is vacuous against an 'is_symlink() -> skip' mutation."
    )
    assert os.path.realpath(link).startswith(str(liminal_root.resolve()) + os.sep), (
        "the alias target resolves outside 10-Liminal, so it is an escape "
        "rather than the contained alias this anchor is about."
    )

    mined = [f.id for f, _b, _k in _load_liminal_fragments(liminal_root)]
    admitted = _admitted_liminal_notes(
        link.parent,
        PrivacyTierOverride.ALL,
        liminal_root=liminal_root,
    )

    assert "LIM-ALIAS" in mined, (
        f"the miner dropped a contained intra-root alias.\n\n{mined}"
    )
    assert [stem for stem, _tier in admitted] == ["alias"], (
        "the state reader dropped a contained intra-root alias. This is the "
        "assertion that fails if the state guard is rooted at the subfolder "
        f"instead of at 10-Liminal.\n\n{admitted}"
    )


# ---------------------------------------------------------------------------
# STEP 5 — the log pin, behavioural and captured at DEBUG
# ---------------------------------------------------------------------------


def test_the_skip_is_logged_without_ever_naming_the_resolved_target(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Every skip announces itself; no record at ANY level names the target.

    Captured at DEBUG for the whole run rather than at WARNING, because the
    claim is about every record the run emits and not merely the warning: a
    DEBUG line quoting the resolved path would rebuild the exfiltration oracle
    #1087 closed just as effectively.

    Asserted behaviourally — "some record names the link, no record names the
    target" — never on message text, so the shared helper stays free to reword.

    Args:
        tmp_path: pytest's per-test temporary directory.
        caplog: pytest log-capture fixture.
    """
    vault, link = _plant_liminal_leaf(tmp_path)
    outside = str((tmp_path / "outside").resolve())

    with caplog.at_level(logging.DEBUG):
        _load_liminal_fragments(vault / "10-Liminal")
        _admitted_liminal_notes(
            link.parent,
            PrivacyTierOverride.ALL,
            liminal_root=vault / "10-Liminal",
        )

    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.WARNING
    ]
    every = [record.getMessage() for record in caplog.records]
    assert sum(link.name in message for message in warnings) >= 2, (
        "fewer than two readers announced the skip. An operator whose vault "
        f"silently loses a note cannot tell a skip from a lost file.\n\n{warnings}"
    )
    assert not any(outside in message for message in every), (
        f"a log record named the link's resolved target.\n\n{every}"
    )
    assert not any(_BODY_SENTINEL in message for message in every), (
        f"a log record quoted the content it declined to read.\n\n{every}"
    )


# ---------------------------------------------------------------------------
# STEP 13 — holding pins: no source change, they hold lanes 2 and 3 still
# ---------------------------------------------------------------------------


def _essay_suppression_vault(tmp_path: Path) -> tuple[Path, Path, str]:
    """Build a vault holding one active thread and an empty essays folder.

    The thread alone is load-bearing: with 99 fragments it clears
    ``min_thread_fragments``, so ``mine_thread_terminus`` yields exactly one
    seed unless a published essay title suppresses it. No member fragments are
    written, because the seed count is identical with and without them — a
    fixture that does not move the assertion is not a fixture.

    Args:
        tmp_path: pytest's per-test temporary directory.

    Returns:
        ``(vault, essays dir, thread title)``.
    """
    vault = tmp_path / "vault"
    thread_title = "The shape of attention"
    _write(
        vault / "02-Threads" / "t.md",
        "---\ntype: thread\nid: thread-1\n"
        f"title: {thread_title}\n"
        "status: active\nfragment_count: 99\n---\n\nbody\n",
    )
    essays = vault / "09-Reference" / "Published-Essays"
    essays.mkdir(parents=True)
    return vault, essays, thread_title


def _terminus_seed_count(vault: Path) -> int:
    """Return how many thread-terminus seeds the miner emits for *vault*.

    Args:
        vault: The vault root.

    Returns:
        The seed count — the number the essay records SUPPRESS.
    """
    return len(IdeaMiner(bypass_compiled=True).mine_thread_terminus(vault))


def test_a_thread_with_no_published_essay_yields_one_terminus_seed(
    tmp_path: Path,
) -> None:
    """POSITIVE CONTROL for the direction pin below.

    Without it, "the seed count did not increase" is satisfiable by a miner
    that emits nothing for an unrelated reason, and the pin would be vacuous.

    Args:
        tmp_path: pytest's per-test temporary directory.
    """
    vault, _essays, _title = _essay_suppression_vault(tmp_path)

    assert _terminus_seed_count(vault) == 1, (
        "the fixture yields no seed even with no essay planted, so the "
        "suppression pin below cannot observe suppression at all."
    )


def test_an_escaping_published_essay_still_suppresses_its_thread(
    tmp_path: Path,
) -> None:
    """DIRECTION PIN, asserted at the CONSUMER (#1793 shape).

    Published essay titles have exactly one consumer,
    ``IdeaMiner._has_matching_essay``, used as ``not self._has_matching_essay(...)``
    in the ``mine_thread_terminus`` candidate filter. The records therefore
    SUPPRESS: dropping one makes the miner emit MORE, not less. So
    ``mining._load_essay_titles`` must NOT acquire a containment guard, and
    the planted title itself reaches no artifact — it is compared and
    discarded.

    **This asserts the seed count, not the loader's return value.** The round-2
    version of this pin asserted ``thread_title in _load_essay_titles(essays)``,
    which a future author defeats without touching that function at all: guard
    the essays walk at its CALL SITE in ``_load_mining_snapshot`` instead, and
    the loader still returns the title while the snapshot no longer carries it.
    That mutant was built and measured — it flips the miner from zero seeds to
    one, re-suggesting an already-published essay — and the loader-return
    assertion cannot see it. A consumer assertion sees any spelling.

    Guarding here would also break the legitimate workflow of symlinking a blog
    repo into ``09-Reference/Published-Essays``, by re-suggesting essays the
    author has already published.

    Args:
        tmp_path: pytest's per-test temporary directory.
    """
    vault, essays, thread_title = _essay_suppression_vault(tmp_path)
    planted = _write(
        tmp_path / "outside-essays" / "published.md",
        f"---\ntitle: {thread_title}\n---\n\nbody\n",
    )
    link = essays / "published.md"
    link.symlink_to(planted)

    assert link.is_symlink(), "the fixture did not create a symlink"
    assert not os.path.realpath(link).startswith(str(vault.resolve()) + os.sep), (
        "the planted essay resolves INSIDE the vault, so no containment guard "
        "would drop it and this pin is vacuous."
    )

    assert _terminus_seed_count(vault) == 0, (
        "the miner emitted a seed for a thread whose essay is already "
        "published, so the escaping essay title stopped suppressing. Either "
        "_load_essay_titles or its call site in _load_mining_snapshot has "
        "acquired a containment guard. That is the #1793 inversion: these "
        "records suppress, so a guard makes the miner emit MORE. If this "
        "change was deliberate, it belongs in lane 3 with its ruling."
    )


def test_an_unresolved_source_id_still_routes_intimate() -> None:
    """HOLDING PIN. No prompt that routed INTIMATE may come to route OPEN.

    Lane 1 must not relax routing while it narrows corpora.
    ``_rendered_source_tiers`` fails an id it cannot resolve closed to
    INTIMATE, which is what keeps a planted source id off a cloud call.
    """
    tiers = drafts._rendered_source_tiers([_CANARY_ID], {}, {})

    assert tiers == [PrivacyTier.INTIMATE], (
        "an unresolved source id no longer routes INTIMATE, so a prompt "
        f"naming a planted id could reach a cloud provider.\n\n{tiers}"
    )


def test_an_opaque_compiled_section_still_routes_intimate() -> None:
    """HOLDING PIN. The compiled-section arm fails closed on an opaque survey.

    This is what makes the lane-2 thread/eddy injection cloud-blocked BY
    CONSTRUCTION: the frontmatter fallback that renders out-of-root prose fires
    only on a compiled-page miss, and that same miss is reported ``opaque`` off
    the same index object composition used.
    """
    tiers = drafts._compiled_section_tiers(
        CompiledSources(fragment_ids=(), opaque=True),
        {},
    )

    assert tiers == [PrivacyTier.INTIMATE], (
        "an opaque compiled survey no longer contributes INTIMATE, so the "
        f"lane-2 sites are no longer cloud-blocked by construction.\n\n{tiers}"
    )


def test_a_lane_one_skip_appends_nothing_to_the_compile_gaps_log(
    tmp_path: Path,
) -> None:
    """A security skip must not acquire a vault WRITE as a side effect.

    ``compile-gaps.jsonl`` is a durable vault artifact. Lane 1's skips are
    read-side narrowings and must leave it untouched — asserted rather than
    assumed, because the neighbouring lane-3 hazard is real: dropping every
    synchronicity note takes ``mining._resonance_fallback_reason`` down its
    ``not synchronicities`` branch, which calls ``record_compile_gap``. This
    pin makes that cost visible before lane 3 decides.

    Args:
        tmp_path: pytest's per-test temporary directory.
    """
    vault, _link = _plant_liminal_leaf(tmp_path)
    gaps = vault / COMPILE_GAPS_RELPATH

    _load_liminal_fragments(vault / "10-Liminal")
    _admitted_liminal_notes(
        vault / "10-Liminal" / "Unnamed",
        PrivacyTierOverride.ALL,
        liminal_root=vault / "10-Liminal",
    )
    _read_fragment_files(vault / "01-Fragments")

    assert not gaps.exists(), (
        "a lane-1 containment skip wrote to the compile-gaps log. A read-side "
        f"security narrowing must not produce a vault artifact.\n\n"
        f"{gaps.read_text(encoding='utf-8')}"
    )


def test_the_resonance_fallback_still_writes_a_gap_for_no_synchronicities(
    tmp_path: Path,
) -> None:
    """STANDING PIN for lane 3: the write it would acquire, measured now.

    ``_resonance_fallback_reason``'s ``not synchronicities`` branch calls
    ``record_compile_gap``. A lane-3 guard that dropped every escaping sync
    note would therefore turn a security skip into a vault write — the cost
    that ruling has to weigh. Pinned here so lane 3 inherits the measurement
    rather than rediscovering it.

    Args:
        tmp_path: pytest's per-test temporary directory.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    reason = IdeaMiner()._resonance_fallback_reason(
        synchronicities=(),
        seeds_kept=0,
        largest_component=0,
        compiled=empty_index(bypassed=False),
        vault_path=vault,
    )

    assert reason, "the fallback reason is empty, so the branch did not fire."
    assert (vault / COMPILE_GAPS_RELPATH).exists(), (
        "the no-synchronicities branch no longer records a compile gap. If "
        "that is deliberate the lane-3 ruling changes, because the cost it "
        "weighs has gone."
    )


def test_rglob_does_not_descend_a_symlinked_directory(tmp_path: Path) -> None:
    """Leaf-only sufficiency, pinned as a test rather than assumed.

    ``escaping_child`` inspects LEAVES. That is sufficient for the ``rglob``
    walks only because ``**`` refuses to descend a symlinked directory — a
    property of pathlib, not of this code, and one 3.13 could have changed when
    ``glob`` gained ``recurse_symlinks``. Verified across the CI matrix
    (3.11.15, 3.12.3, 3.13.12); this keeps the matrix proving it.

    The flat-glob site is exactly the residual this leaves, which is why
    ``_admitted_liminal_notes`` needs the folder-level ``named_path_escapes``
    refusal on top.

    Args:
        tmp_path: pytest's per-test temporary directory.
    """
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    _write(outside / "inside.md", "body\n")
    (root / "linked").symlink_to(outside, target_is_directory=True)

    assert (root / "linked").is_dir(), "the fixture's link is not a directory"
    assert list(root.rglob("*.md")) == [], (
        "rglob descended a symlinked directory on "
        f"{sys.version_info.major}.{sys.version_info.minor}. Leaf-only "
        "containment is no longer sufficient for the rglob walks and every "
        "guarded site in this lane needs a directory-level check."
    )
    assert list(root.glob("**/*.md")) == [], (
        "glob('**/*.md') descended a symlinked directory, diverging from "
        "rglob. The two must agree; the helper uses one of them."
    )


def test_no_module_re_derives_containment_with_a_bare_is_symlink() -> None:
    """A LINT-SHAPED HINT for ONE spelling. It is not the containment guarantee.

    What it actually asserts, stated in full so nobody has to infer it: no
    ``ast.Call`` in ``creek/generate/`` or ``creek/vault/`` whose function is
    an ``ast.Attribute`` named ``is_symlink`` — except one whose receiver
    unparses to the text ``latest`` inside ``state.py``, which is the
    unlink-before-relink existence check on the ``State/latest.md`` convenience
    link ("is there something here to remove", not "does this leave its root").

    What it does NOT assert, measured rather than assumed. The exemption is
    keyed on the receiver's *text*, module-wide, so any local named ``latest``
    anywhere in ``state.py`` is exempt. And an inline re-derivation spelled
    with any other stdlib call passes untouched::

        os.path.islink(folder) and not str(folder.resolve()).startswith(
            str(folder.parent.resolve())
        )

    That mutant was built and it is not behaviour-preserving: with
    ``10-Liminal/Unnamed`` linked to the sibling ``10-Liminal-secrets``, the
    prefix comparison answers "contained" where ``relative_to`` answers
    "escaped", and the state report admitted a note the miner refused. It
    survived every test in this lane.

    **The response is not a longer pattern list.** Widening the scan moves the
    hole to the next spelling, and this repo's AST/identity tripwires have now
    been found overclaiming on five consecutive changes. The guarantee lives in
    behaviour instead:
    ``test_a_sibling_folder_whose_name_extends_the_liminal_root_is_refused``
    asserts what the reader RETURNS for exactly that vault, which kills the
    re-derivation in any spelling — no scan can enumerate spellings, but every
    spelling has to produce an answer. This scan is kept as a cheap first
    alarm for the one shape it does catch, and its failure message should be
    read as "look here", not as "containment is proven".
    """
    package_roots = (
        Path(state.__file__).parent,
        Path(reader_module.__file__).parent,
    )
    offenders: list[str] = []
    for root in package_roots:
        for module in sorted(root.rglob("*.py")):
            source = module.read_text(encoding="utf-8")
            # Parsed, not grepped: these docstrings quote the very shapes they
            # rule out, and a text scan would flag the prose that explains the
            # rule as a violation of it.
            for node in ast.walk(ast.parse(source)):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if not isinstance(func, ast.Attribute) or func.attr != "is_symlink":
                    continue
                target = ast.unparse(func.value)
                if module.name == "state.py" and target == "latest":
                    continue
                offenders.append(f"{module.name}:{node.lineno}: {target}.is_symlink()")

    assert offenders == [], (
        "a module in creek/generate/ or creek/vault/ calls is_symlink() "
        "directly. Containment there must go through creek._containment — a "
        "second copy of the predicate is the drift #1294 closed. This scan "
        "catches one spelling only; if you are here because it fired, check "
        "the behavioural pins too. If this is a non-containment existence "
        "check, narrow the exception here deliberately rather than deleting "
        f"the assertion.\n\n{offenders}"
    )
