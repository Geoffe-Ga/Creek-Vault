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
from creek.generate.state import _admitted_liminal_notes, _read_fragment_files
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

    loaded = [f.id for _p, f, _raw in _read_fragment_files(vault / "01-Fragments")]

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

    loaded = [f.id for _p, f, _raw in _read_fragment_files(fragments)]

    assert "alias-1" in loaded, (
        "a contained intra-root alias was dropped. Containment is about the "
        f"target leaving the root, not about the link existing.\n\n{loaded}"
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


def test_essay_titles_stay_unguarded_because_they_suppress(tmp_path: Path) -> None:
    """DIRECTION PIN. ``_load_essay_titles`` must NOT acquire a guard (#1793 shape).

    Published essay titles have exactly one consumer,
    ``IdeaMiner._has_matching_essay``, used as ``not self._has_matching_essay(...)``
    in the ``mine_thread_terminus`` candidate filter. The records therefore
    SUPPRESS: dropping one makes the miner emit MORE, not less, and the planted
    title itself reaches no artifact — it is compared and discarded.

    Guarding here would also break the legitimate workflow of symlinking a blog
    repo into ``09-Reference/Published-Essays``, by re-suggesting essays the
    author has already published.

    This asserts the CURRENT behaviour so a future silent guard flips it and
    fails, rather than sliding in for consistency with its guarded neighbours.

    Args:
        tmp_path: pytest's per-test temporary directory.
    """
    vault = tmp_path / "vault"
    thread_title = "The shape of attention"
    _write(
        vault / "02-Threads" / "t.md",
        "---\ntype: thread\nid: thread-1\n"
        f"title: {thread_title}\n"
        "status: active\nfragment_count: 99\n---\n\nbody\n",
    )
    for index in range(12):
        _write(
            vault / "01-Fragments" / f"f{index}.md",
            _note(f"frag-{index}", "F").replace(
                "---\n\n",
                "threads:\n  - thread-1\n---\n\n",
            ),
        )
    essays = vault / "09-Reference" / "Published-Essays"
    essays.mkdir(parents=True)
    planted = _write(
        tmp_path / "outside-essays" / "published.md",
        f"---\ntitle: {thread_title}\n---\n\nbody\n",
    )
    link = essays / "published.md"
    link.symlink_to(planted)

    assert link.is_symlink(), "the fixture did not create a symlink"
    titles = mining._load_essay_titles(essays)

    assert thread_title in titles, (
        "the escaping essay title was dropped, so _load_essay_titles has "
        "acquired a containment guard. That is the #1793 inversion: these "
        "records suppress, so the guard makes the miner emit MORE. If this "
        f"change was deliberate, it belongs in lane 3 with its ruling.\n\n{titles}"
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
    """AC1, as a scan: the predicate is not re-derived in the guarded packages.

    ``creek/generate/`` and ``creek/vault/`` must reach containment only
    through :mod:`creek._containment`. A bare ``is_symlink()`` in either
    package is either a second copy of the predicate or the beginning of one.

    ONE documented exception, narrowed here rather than quietly:
    ``creek/generate/state.py``'s ``latest.exists() or latest.is_symlink()`` is
    an unlink-before-relink existence check on the ``State/latest.md``
    convenience link. It answers "is there something here to remove", not "does
    this leave its root", and is unrelated to containment.
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
        "second copy of the predicate is the drift #1294 closed. If this is a "
        "non-containment existence check, narrow the exception here "
        f"deliberately rather than deleting the assertion.\n\n{offenders}"
    )
