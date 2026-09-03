"""A ``creek compile`` page has no tier key; every reader must still refuse it (#1282).

:func:`creek.compile.engine.compile_to_vault` writes its synthesis page with
``page.model_dump(mode="json", exclude={"body"})`` as the whole of the
frontmatter (``engine.py:817-819``), and
:class:`~creek.models.CompiledPage` (``models.py:1186-1202``) declares no
``privacy_tier`` field — nor an ``id``, ``tags``, ``status`` or ``formed``.
The absence on disk is therefore the absence in the model, and it is not an
oversight anybody can fix at the reader.

**There is no live leak, and this module must not be read as closing one.**
Exactly one reader in the repository asks a compiled page for a tier —
:meth:`creek.generate.tags.TagGardenGenerator._extract_tags`, whose
``scan_tags`` rglobs ``02-Threads`` and ``03-Eddies`` (``tags.py:73-79``,
``:234-241``) — and it fails closed:
:func:`~creek.classify.privacy_filter.raw_privacy_tier` maps the missing key
to ``INTIMATE`` (``privacy_filter.py:433-435``), so the page is withheld below
``ceiling=intimate``. Ten further readers walk the same directories and never
ask, each stopped by a different accident: a ``type`` filter, a non-recursive
``glob``, or an absent key. This module pins all eleven answers as an
executable contract, because nothing at HEAD pinned any of them and the
correct behaviour today rests on three independent coincidences rather than on
one shared admission helper. There is no chokepoint to assert at; building one
is out of scope for #1282.

Two premises in the issue body are **false** and are refuted here rather than
carried forward:

* The read gate does *not* see a compiled page.
  :data:`creek_mcp.read_gate._FRAGMENTS_SUBDIR` is ``"01-Fragments"``
  (``read_gate.py:237``) and ``iter_admitted_fragments`` walks only
  ``vault / _FRAGMENTS_SUBDIR`` (``:906-908``), which no compiled-layer
  directory is under. Asserted structurally below rather than described.
* A "reader using Fragment-style model defaults" cannot exist.
  :class:`~creek.models.Fragment` requires ``id`` and ``source``
  (``models.py:730``), neither of which a compiled page's frontmatter carries,
  so ``Fragment.model_validate`` raises rather than defaulting to
  ``unclassified``.

The obvious end-to-end assertion — that ``Tag-Garden.md`` omits the page — is
**vacuous**, and that is proven here rather than asserted:
``_extract_tags`` reads ``post.get("tags", [])`` (``tags.py:463-464``) and
``CompiledPage`` has no ``tags`` field, so the tally is byte-identical whether
the page is admitted or withheld.
:func:`test_the_tag_tally_cannot_witness_the_withholding` demonstrates it.
Every tags assertion below is therefore made on ``_extract_tags``'s **return
value**, never on garden content.

Non-vacuity is the whole claim of a characterization module, so every
assertion here is paired with something that can break it. The tier pins carry
both positive controls (``INTIMATE`` — the boundary case, since
``_TIER_RANK[INTIMATE] == _OVERRIDE_RANK[INTIMATE] == 2`` — and ``ALL``, the
short-circuit at ``privacy_filter.py:252-254``), without which a gate broken in
the drop-everything direction would satisfy every refusal above. Each SAFE
reader row carries a staged mutation that makes that same reader claim the same
page, so no row can pass because its reader never opened the file. Three of
those mutations are deliberately **two-stage**: a single ``type:`` flip is not
enough for ``_eddy_page``, ``_find_related_threads`` or
``_load_active_threads``, and the intermediate stage is asserted separately so
the two guards are not confused for one.

The **stamped** ``privacy_tier`` field #1282 also proposes is out of scope and
is not built here: it would *widen* ``_extract_tags`` from withheld to admitted
at ``ceiling=open``, a compile-time stamp goes permissively stale against an
escalate-only ``privacy_pass`` (``vault/writer.py:1126``, ``:1158``), and
ADR-0010 (``skills.py:769-773``) decided the adjacent ``Thread``/``Eddy``
question the other way while naming exactly this field as its revisit trigger.
:func:`test_a_compiled_page_declares_no_tier_key_of_its_own` is the tripwire
that goes red the day someone lands it without reopening that question.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

import frontmatter
import pytest

from creek.classify.privacy_filter import (
    PrivacyTierOverride,
    raw_privacy_tier,
    within_ceiling,
)
from creek.compile.engine import _TARGET_DIRS, TARGET_KINDS, compile_to_vault
from creek.generate import compost, compost_scan, decisions, mining, skills, state
from creek.generate.tags import _SCAN_DIRS, TagGardenGenerator
from creek.models import (
    Eddy,
    Fragment,
    FragmentSource,
    PrivacyTier,
    SourcePlatform,
    Thread,
)
from creek_mcp import compiled_pages, read_gate

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from creek.models import CompileTargetKind

# ---- Fixture constants ------------------------------------------------------

_PAGE_TITLE: str = "Systems Of Record"
"""Title stamped on every compiled page built here.

Deliberately disjoint from every other canary in this module: the target ids
below share no token with it, so an assertion that sees ``"systems"`` can only
have got it from ``title`` and one that sees ``"canary"`` can only have got it
from an id or a filename.
"""

_TARGET_IDS: dict[str, str] = {
    "thread": "thread-canary",
    "eddy": "eddy-canary",
    "frequency_index": "freq-canary",
}
"""Per-kind compiled-page ``target_id``, which is also the filename stem."""

_INJECTED_ID: str = "injected-id-canary"
"""The ``id`` value the ``decisions`` mutations add to the page.

Distinct from every ``_TARGET_IDS`` entry on purpose. ``_score_thread`` reads
``post.get("id")`` (``decisions.py:969``) while ``_extract_tags`` falls back to
``md_file.stem`` (``tags.py:461``); a shared token could not tell those two
sources apart.
"""

_FILLER_TAG: str = "tag-garden-reachability"
"""Tag on the open-tier filler thread note, present in no other fixture."""

_FILLER_ID: str = "filler-thread"
"""Id of the open-tier filler thread note."""

_EXPECTED_SUBDIR: dict[str, str] = {
    "thread": "02-Threads/Active",
    "eddy": "03-Eddies",
    "frequency_index": "06-Frequencies",
}
"""Where each compiled surface lands, mirroring ``engine.py:95-99``.

Written out rather than imported so that relocating a compiled surface reddens
:func:`test_every_compiled_target_kind_lands_where_the_readers_look` and forces
the next lane to re-ask whether the readers below still reach it. The mirror is
checked against :data:`creek.compile.engine._TARGET_DIRS` there, so it cannot
drift silently.
"""


# ---- Fixture construction ---------------------------------------------------


def _write_open_fragment(vault: Path, frag_id: str) -> None:
    """Persist one ``privacy_tier: open`` fragment under ``01-Fragments/Notes``.

    The source is open on purpose: the compiled page's ``INTIMATE`` read must
    be visibly a property of its own absent key, not something inherited from
    a sensitive source.

    Args:
        vault: Vault root.
        frag_id: Fragment id, also the filename stem.
    """
    fragment = Fragment(
        id=frag_id,
        title="A fragment about ledgers",
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
        created=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
        ingested=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
        privacy_tier=PrivacyTier.OPEN,
    )
    root = vault / "01-Fragments" / "Notes"
    root.mkdir(parents=True, exist_ok=True)
    post = frontmatter.Post(content="body of A", **fragment.model_dump(mode="json"))
    (root / f"{frag_id}.md").write_text(frontmatter.dumps(post), encoding="utf-8")


def _compile_page(vault: Path, target_kind: str) -> Path:
    """Compile one open fragment into *target_kind*'s page and return its path.

    Routed through the real :func:`creek.compile.engine.compile_to_vault` with
    a stub ``llm_factory`` rather than through ``tests/factories/compiled.py``,
    which re-implements ``_write_compiled_page`` independently: an assertion
    against the factory would prove nothing about what the engine puts on disk.

    Args:
        vault: Vault root.
        target_kind: One of :data:`creek.compile.engine.TARGET_KINDS`.

    Returns:
        The path of the written compiled-layer page.
    """
    for sub in (
        "00-Creek-Meta/Processing-Log",
        "01-Fragments/Notes",
        "02-Threads/Active",
        "03-Eddies",
        "06-Frequencies",
    ):
        (vault / sub).mkdir(parents=True, exist_ok=True)
    _write_open_fragment(vault, "frag-aaa")
    response = json.dumps(
        {
            "claims": [
                {
                    "id": "claim-001",
                    "text": "A surfaced claim.",
                    "fragment_ids": ["frag-aaa"],
                },
            ],
            "paradoxes": [],
        },
    )

    def _factory(_tier: PrivacyTier) -> Callable[[str], str]:
        """Return a canned compile LLM ignoring its prompt."""
        return lambda _prompt: response

    return compile_to_vault(
        fragment_ids=["frag-aaa"],
        vault_path=vault,
        target_kind=cast("CompileTargetKind", target_kind),
        target_id=_TARGET_IDS[target_kind],
        target_title=_PAGE_TITLE,
        llm_factory=_factory,
    )


def _assert_reachable(vault: Path, written: Path, target_kind: str) -> None:
    """Assert the page exists where the readers below actually walk.

    No refusal row may pass because its file was never scanned.
    ``.resolve()`` mirrors ``engine.py:762``: on macOS ``tmp_path`` is under
    ``/var`` and the engine returns ``/private/var``, so
    ``written.relative_to(vault)`` would raise here rather than assert.

    Args:
        vault: Vault root.
        written: The compiled page's path.
        target_kind: The kind that produced it.
    """
    assert written.exists()
    assert written.parent == (vault / _EXPECTED_SUBDIR[target_kind]).resolve()


def _restamp(path: Path, **updates: object) -> Path:
    """Rewrite *path*'s frontmatter with *updates* applied, in place.

    Args:
        path: The markdown file to rewrite.
        updates: Frontmatter keys to set.

    Returns:
        *path*, for call chaining.
    """
    post = frontmatter.load(str(path))
    post.metadata.update(updates)
    path.write_text(frontmatter.dumps(post), encoding="utf-8")
    return path


def _copy_into(path: Path, dest_dir: Path) -> Path:
    """Copy *path*'s bytes into *dest_dir* under the same name.

    Args:
        path: The source markdown file.
        dest_dir: Destination directory, created if absent.

    Returns:
        The new file's path.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / path.name
    dest.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    return dest


def _title_tokens() -> set[str]:
    """Return the token set of :data:`_PAGE_TITLE`.

    Used as the query tokens for the ``decisions`` rows so the Jaccard
    similarity against the page's own title is 1.0 — far above
    ``DecisionContextGatherer.similarity_threshold`` (0.1,
    ``decisions.py:793``). A refusal measured with maximally overlapping
    tokens cannot be a similarity miss.

    Returns:
        The tokenised title.
    """
    return decisions._tokenize(_PAGE_TITLE)


# ---- Parametrize-source identity -------------------------------------------


def test_every_compiled_target_kind_lands_where_the_readers_look() -> None:
    """Every engine surface is exercised, and lands where this module expects.

    The zero-skip guard in executable form. Emptying
    :data:`~creek.compile.engine.TARGET_KINDS` would turn every parametrized
    test below into a green no-op; adding a fourth compiled surface would let
    it escape this module entirely. Both redden here instead.
    """
    assert TARGET_KINDS, "TARGET_KINDS is empty; every parametrized row is a no-op"
    assert set(_EXPECTED_SUBDIR) == set(TARGET_KINDS)
    assert {kind: str(path) for kind, path in _TARGET_DIRS.items()} == _EXPECTED_SUBDIR


# ---- The central tier pins, read off disk -----------------------------------


@pytest.mark.parametrize("target_kind", TARGET_KINDS)
def test_a_compiled_page_declares_no_tier_key_of_its_own(
    tmp_path: Path, target_kind: str
) -> None:
    """The written page carries no ``privacy_tier`` key.

    Read back off disk rather than from the in-memory
    :class:`~creek.models.CompiledPage`: an assertion against the model would
    pass even if ``_write_compiled_page`` stopped emitting frontmatter
    entirely.

    This is the tripwire for the deferred stamped field. Appending
    ``privacy_tier: PrivacyTier = PrivacyTier.UNCLASSIFIED`` to ``CompiledPage``
    reddens it for all three kinds without touching ``engine.py``, because
    ``model_dump(mode="json", exclude={"body"})`` emits every field regardless
    of its default. Anyone who reddens this test is changing what readers admit
    and owes the widening question a fresh answer.
    """
    written = _compile_page(tmp_path, target_kind)
    _assert_reachable(tmp_path, written, target_kind)

    post = frontmatter.load(str(written))

    assert "privacy_tier" not in post.metadata
    assert "tags" not in post.metadata
    assert "id" not in post.metadata
    assert "status" not in post.metadata
    assert "formed" not in post.metadata


@pytest.mark.parametrize("target_kind", TARGET_KINDS)
def test_a_compiled_page_reads_intimate_from_its_absent_tier_key(
    tmp_path: Path, target_kind: str
) -> None:
    """The raw tier readers answer ``INTIMATE`` and refuse below that ceiling.

    ``INTIMATE`` appears nowhere in the fixture — the input is the *absence* of
    a key — so only the fail-closed branch at ``privacy_filter.py:433-435`` can
    manufacture it.

    The two positive controls are load-bearing, not decoration: a gate broken
    in the drop-everything direction satisfies both refusals above them.
    ``INTIMATE`` is the boundary case (``_TIER_RANK[INTIMATE] == 2 <=
    _OVERRIDE_RANK[INTIMATE] == 2``) and ``ALL`` is the short-circuit at
    ``privacy_filter.py:252-254``.

    The ``PERSONAL`` row is the load-bearing refusal and must not be dropped as
    redundant with ``OPEN``. Under a mutation that returns ``UNCLASSIFIED``
    instead of ``INTIMATE``, the ``OPEN`` row stays green — ``UNCLASSIFIED``
    ranks with ``PERSONAL`` at 1, so ``1 <= 0`` is still ``False``
    (``privacy_filter.py:165-178``, #876) — and only ``PERSONAL`` catches it.
    """
    written = _compile_page(tmp_path, target_kind)
    _assert_reachable(tmp_path, written, target_kind)

    post = frontmatter.load(str(written))

    assert raw_privacy_tier(post.metadata) is PrivacyTier.INTIMATE
    assert within_ceiling(post.metadata, PrivacyTierOverride.OPEN) is False
    assert within_ceiling(post.metadata, PrivacyTierOverride.PERSONAL) is False
    assert within_ceiling(post.metadata, PrivacyTierOverride.INTIMATE) is True
    assert within_ceiling(post.metadata, PrivacyTierOverride.ALL) is True


# ---- The one live reader ----------------------------------------------------


@pytest.mark.parametrize("target_kind", ("thread", "eddy"))
@pytest.mark.parametrize(
    ("override", "withheld"),
    [
        (PrivacyTierOverride.OPEN, True),
        (PrivacyTierOverride.PERSONAL, True),
        (PrivacyTierOverride.INTIMATE, False),
        (PrivacyTierOverride.ALL, False),
    ],
)
def test_the_tag_scan_withholds_a_compiled_page_below_the_intimate_ceiling(
    tmp_path: Path,
    target_kind: str,
    override: PrivacyTierOverride,
    withheld: bool,
) -> None:
    """``_extract_tags`` is the only reader that asks, and it fails closed.

    ``thread`` and ``eddy`` are the two compiled surfaces inside
    :data:`~creek.generate.tags._SCAN_DIRS`; ``frequency_index`` is not
    scanned at all and carries only the tier pins above.

    Asserted on the return value. See
    :func:`test_the_tag_tally_cannot_witness_the_withholding` for the proof
    that the garden itself cannot show this.

    ``_extract_tags`` is a ``@staticmethod`` (``tags.py:419``), so it is called
    on the class: an instance would suggest the ceiling came from
    ``self.override`` when it is the explicit argument.
    """
    written = _compile_page(tmp_path, target_kind)
    _assert_reachable(tmp_path, written, target_kind)

    result = TagGardenGenerator._extract_tags(written, override)

    assert (result is None) is withheld
    if result is not None:
        # The id is the filename stem, not an ``id`` key: ``CompiledPage`` has
        # none, so ``post.get("id", md_file.stem)`` takes its fallback.
        assert result.fragment_id == _TARGET_IDS[target_kind]
        assert result.tags == []


def test_the_tag_tally_cannot_witness_the_withholding(tmp_path: Path) -> None:
    """The tag tally is identical at ``OPEN`` and ``ALL`` — hence assert elsewhere.

    This is why no assertion in this module reads ``Tag-Garden.md``.
    ``_extract_tags`` returns ``post.get("tags", [])`` and ``scan_tags`` folds
    only per-tag entries into the tally (``tags.py:243-245``), so a page with
    no ``tags`` field contributes nothing whether it is admitted or withheld.
    An end-to-end assertion on garden content would pass against a completely
    removed tier gate.

    The open-tier filler note is the non-vacuity control: it proves the scan
    really does rglob into ``02-Threads/Active`` — the exact directory holding
    the compiled page — so the identical tallies are the tags field being
    empty, not the scan finding nothing.

    Reddens if ``CompiledPage`` ever gains a ``tags`` field, which is the point
    at which the garden *would* become a valid place to assert.
    """
    written = _compile_page(tmp_path, "thread")
    _assert_reachable(tmp_path, written, "thread")
    filler = Thread(
        id=_FILLER_ID,
        title="An open thread",
        tags=[_FILLER_TAG],
    )
    filler_meta = filler.model_dump(mode="json")
    filler_meta["privacy_tier"] = PrivacyTier.OPEN.value
    (written.parent / f"{_FILLER_ID}.md").write_text(
        frontmatter.dumps(frontmatter.Post(content="filler", **filler_meta)),
        encoding="utf-8",
    )

    at_open = TagGardenGenerator(
        tmp_path, override=PrivacyTierOverride.OPEN
    ).scan_tags()
    at_all = TagGardenGenerator(tmp_path, override=PrivacyTierOverride.ALL).scan_tags()

    assert at_open.tag_fragments[_FILLER_TAG] == [_FILLER_ID]
    assert at_open == at_all
    for tallied in at_all.tag_fragments.values():
        assert _TARGET_IDS["thread"] not in tallied


def test_the_frequency_index_surface_has_no_tag_scan_gate_at_all() -> None:
    """``06-Frequencies`` is outside ``_SCAN_DIRS``, so nothing gates it there.

    Recorded as a pin rather than a comment because it is the one compiled
    surface whose safety is scope, not a gate: a future scan directory added to
    :data:`~creek.generate.tags._SCAN_DIRS` arrives with no tier question asked
    of it beyond ``_extract_tags``'s, and whoever adds one should have to
    update this line knowingly. Appending ``"06-Frequencies"`` to
    ``tags.py:73-79`` reddens it.
    """
    assert _SCAN_DIRS
    assert "06-Frequencies" not in _SCAN_DIRS
    assert "02-Threads" in _SCAN_DIRS
    assert "03-Eddies" in _SCAN_DIRS


def test_the_read_gate_cannot_reach_the_compiled_layer(tmp_path: Path) -> None:
    """The issue body's read-gate premise is false, asserted rather than argued.

    ``iter_admitted_fragments`` walks ``vault / _FRAGMENTS_SUBDIR``
    (``read_gate.py:906-908``) and nothing else, so no compiled-layer
    directory is in its scope. Made executable because "found them all" prose
    has been wrong here before; a future widening of the gate's scope should
    redden a privacy test rather than pass silently.
    """
    assert read_gate._FRAGMENTS_SUBDIR == "01-Fragments"
    fragments_root = (tmp_path / read_gate._FRAGMENTS_SUBDIR).resolve()
    for target_kind in TARGET_KINDS:
        written = _compile_page(tmp_path, target_kind)
        _assert_reachable(tmp_path, written, target_kind)
        assert fragments_root not in written.parents


# ---- The SAFE reader table --------------------------------------------------


@dataclass(frozen=True)
class _ReaderRow:
    """One reader that walks the compiled layer and makes no record of a page.

    Attributes:
        name: Test id.
        target_kind: The compiled surface this reader walks.
        guard: The specific mechanism that stops it, quoted in the failure
            message. Named per row on purpose: nine of these eleven rows pin
            an *accident* rather than an invariant, so a deliberate refactor
            (``glob`` to ``rglob``, say) is expected to update the assertion
            knowingly rather than delete it.
        read: Number of records the reader makes of the page. ``0`` at HEAD.
        admit: Applies the full staged mutation that makes the reader claim
            the page, and returns the path the reader should then be pointed
            at. Without it a row could pass because the reader never opened
            the file.
    """

    name: str
    target_kind: str
    guard: str
    read: Callable[[Path, Path], int]
    admit: Callable[[Path, Path], Path]


def _none_to_zero(value: object | None) -> int:
    """Collapse an optional record to a count.

    Args:
        value: A reader's optional result.

    Returns:
        ``0`` when *value* is ``None``, else ``1``.
    """
    return 0 if value is None else 1


_TYPE_GUARD: str = "metadata.get('type') != <expected>, and the page says compiled_page"

_READER_ROWS: tuple[_ReaderRow, ...] = (
    _ReaderRow(
        name="mining._load_typed[thread]",
        target_kind="thread",
        guard=f"mining.py:540 — {_TYPE_GUARD}",
        read=lambda v, _p: len(
            mining._load_typed(v / "02-Threads", type_tag="thread", cls=Thread)
        ),
        admit=lambda _v, p: _restamp(p, type="thread"),
    ),
    _ReaderRow(
        name="mining._load_typed[eddy]",
        target_kind="eddy",
        guard=f"mining.py:540 — {_TYPE_GUARD}",
        read=lambda v, _p: len(
            mining._load_typed(v / "03-Eddies", type_tag="eddy", cls=Eddy)
        ),
        admit=lambda _v, p: _restamp(p, type="eddy"),
    ),
    _ReaderRow(
        name="state._load_typed_models[thread]",
        target_kind="thread",
        guard=f"state.py:358 — {_TYPE_GUARD}",
        read=lambda v, _p: len(
            state._load_typed_models(v / "02-Threads", type_tag="thread", cls=Thread)
        ),
        admit=lambda _v, p: _restamp(p, type="thread"),
    ),
    _ReaderRow(
        name="state._load_typed_models[eddy]",
        target_kind="eddy",
        guard=f"state.py:358 — {_TYPE_GUARD}",
        read=lambda v, _p: len(
            state._load_typed_models(v / "03-Eddies", type_tag="eddy", cls=Eddy)
        ),
        admit=lambda _v, p: _restamp(p, type="eddy"),
    ),
    _ReaderRow(
        name="skills._collect_typed[thread]",
        target_kind="thread",
        guard=f"skills.py:753-766 — {_TYPE_GUARD}",
        read=lambda v, _p: len(
            skills._collect_typed(
                v / "02-Threads", expected_type="thread", model_cls=Thread
            )
        ),
        admit=lambda _v, p: _restamp(p, type="thread"),
    ),
    _ReaderRow(
        name="skills._collect_typed[eddy]",
        target_kind="eddy",
        guard=f"skills.py:753-766 — {_TYPE_GUARD}",
        read=lambda v, _p: len(
            skills._collect_typed(v / "03-Eddies", expected_type="eddy", model_cls=Eddy)
        ),
        admit=lambda _v, p: _restamp(p, type="eddy"),
    ),
    _ReaderRow(
        name="compost_scan._load_threads",
        target_kind="thread",
        guard=f"compost_scan.py:229 — {_TYPE_GUARD}; takes the vault ROOT",
        read=lambda v, _p: len(compost_scan._load_threads(v)),
        admit=lambda _v, p: _restamp(p, type="thread"),
    ),
    _ReaderRow(
        name="compiled_pages._eddy_page",
        target_kind="eddy",
        guard=(
            "compiled_pages.py:320 type guard, THEN :322-329 requires a truthy "
            "`formed` and an int `fragment_count` the page also lacks"
        ),
        read=lambda _v, p: _none_to_zero(compiled_pages._eddy_page(p)),
        admit=lambda _v, p: _restamp(
            p, formed="2026-04-01", fragment_count=1, type="eddy"
        ),
    ),
    _ReaderRow(
        name="decisions._score_thread",
        target_kind="thread",
        guard="decisions.py:969-971 — `if not thread_id: return None`; no `id` key",
        read=lambda _v, p: _none_to_zero(
            decisions.DecisionContextGatherer()._score_thread(p, _title_tokens(), [])
        ),
        admit=lambda _v, p: _restamp(p, id=_INJECTED_ID),
    ),
    _ReaderRow(
        name="decisions._find_related_threads",
        target_kind="thread",
        guard=(
            "decisions.py:1212-1216 — `_iter_markdown` is a non-recursive "
            "glob('*.md') on 02-Threads; the page is in 02-Threads/Active"
        ),
        read=lambda v, _p: len(
            decisions.DecisionContextGatherer()._find_related_threads(
                v, _title_tokens(), []
            )
        ),
        admit=lambda v, p: _copy_into(_restamp(p, id=_INJECTED_ID), v / "02-Threads"),
    ),
    _ReaderRow(
        name="compost.CompostTracker._load_active_threads",
        target_kind="thread",
        guard=(
            "compost.py:1058 non-recursive glob('*.md') on 02-Threads, AND "
            ":1063-1065 `status != active`; the page has neither"
        ),
        read=lambda v, _p: len(
            compost.CompostTracker._load_active_threads(v / "02-Threads")
        ),
        admit=lambda v, p: _copy_into(
            _restamp(p, status="active", id=_INJECTED_ID), v / "02-Threads"
        ),
    ),
)
"""Every reader that walks a compiled-layer directory without asking for a tier.

Rows collapse to an ``int`` so a twelfth reader is one row rather than one more
test. The honest invariant is *"a compiled page must not be admitted under a
ceiling below intimate"*, and only the tags rows above test that; these eleven
test the weaker *"this reader currently ignores compiled pages"*. They are kept
because enumerating every site is the only defence against the next reader
being written without the question being asked, and each row names its guard so
a legitimate refactor updates it deliberately.
"""


@pytest.mark.parametrize("row", _READER_ROWS, ids=lambda row: row.name)
def test_no_reader_makes_a_record_of_a_compiled_page(
    tmp_path: Path, row: _ReaderRow
) -> None:
    """Eleven readers walk the compiled layer; none claims a compiled page.

    Args:
        tmp_path: Pytest temporary directory used as the vault root.
        row: The reader under test.
    """
    written = _compile_page(tmp_path, row.target_kind)
    _assert_reachable(tmp_path, written, row.target_kind)

    assert row.read(tmp_path, written) == 0, row.guard


@pytest.mark.parametrize("row", _READER_ROWS, ids=lambda row: row.name)
def test_every_safe_reader_row_would_claim_the_page_once_its_guard_falls(
    tmp_path: Path, row: _ReaderRow
) -> None:
    """Each refusal above is the guard, not the reader never opening the file.

    Without this, every row in
    :func:`test_no_reader_makes_a_record_of_a_compiled_page` could be passing
    because its reader was pointed at the wrong directory — the failure mode
    that makes a whole privacy table vacuous while reading as green.

    Args:
        tmp_path: Pytest temporary directory used as the vault root.
        row: The reader under test.
    """
    written = _compile_page(tmp_path, row.target_kind)
    _assert_reachable(tmp_path, written, row.target_kind)
    assert row.read(tmp_path, written) == 0

    admitted = row.admit(tmp_path, written)

    assert row.read(tmp_path, admitted) == 1, row.guard


# ---- Staged-guard isolation: the intermediate stage is not enough -----------


def test_the_eddy_page_type_guard_alone_withholds_a_well_formed_compiled_page(
    tmp_path: Path,
) -> None:
    """``_eddy_page`` is guarded twice; completing its fields is not enough.

    Isolates ``compiled_pages.py:320`` from ``:322-329``. A single ``type:``
    flip would also have returned ``None`` here — for the *wrong* reason — so
    without this stage the type guard would look load-bearing when the missing
    ``formed`` and ``fragment_count`` were doing the work.
    """
    written = _compile_page(tmp_path, "eddy")
    _assert_reachable(tmp_path, written, "eddy")

    _restamp(written, formed="2026-04-01", fragment_count=1)
    assert frontmatter.load(str(written)).metadata["type"] == "compiled_page"

    assert compiled_pages._eddy_page(written) is None

    _restamp(written, type="eddy")
    admitted = compiled_pages._eddy_page(written)
    assert admitted is not None
    assert admitted.title == _PAGE_TITLE


def test_the_decision_thread_scan_scope_alone_withholds_an_identified_page(
    tmp_path: Path,
) -> None:
    """Giving the page an ``id`` does not reach ``_find_related_threads``.

    Isolates the non-recursive ``glob`` at ``decisions.py:1212-1216`` from the
    ``id`` gate at ``:969-971``. The scope guard fires first and alone; the
    page must also be relocated to ``02-Threads`` before the ``id`` matters.
    """
    written = _compile_page(tmp_path, "thread")
    _assert_reachable(tmp_path, written, "thread")
    gatherer = decisions.DecisionContextGatherer()
    tokens = _title_tokens()

    _restamp(written, id=_INJECTED_ID)
    assert gatherer._find_related_threads(tmp_path, tokens, []) == []
    # The scope, not the score: the same file scores 1.0 when handed over
    # directly, so only its directory is keeping it out.
    assert gatherer._score_thread(written, tokens, []) == (1.0, _INJECTED_ID)

    _copy_into(written, tmp_path / "02-Threads")
    assert gatherer._find_related_threads(tmp_path, tokens, []) == [_INJECTED_ID]


def test_the_compost_active_scan_needs_both_relocation_and_a_status_key(
    tmp_path: Path,
) -> None:
    """``_load_active_threads`` is guarded by scope AND by ``status``.

    Isolates ``compost.py:1058`` from ``:1063-1065``. Each stage alone still
    yields ``[]``, so a single combined mutation would leave either guard
    looking load-bearing when only one of them was.
    """
    written = _compile_page(tmp_path, "thread")
    _assert_reachable(tmp_path, written, "thread")
    threads_root = tmp_path / "02-Threads"

    # Stage 1: the status key, still in Active/ — the scope guard alone.
    _restamp(written, status="active", id=_INJECTED_ID)
    assert compost.CompostTracker._load_active_threads(threads_root) == []

    # Stage 2: relocated to the scanned directory, but with no status key —
    # the status guard alone.
    unstatused = _copy_into(written, threads_root)
    post = frontmatter.load(str(unstatused))
    del post.metadata["status"]
    unstatused.write_text(frontmatter.dumps(post), encoding="utf-8")
    assert compost.CompostTracker._load_active_threads(threads_root) == []

    # Both stages: claimed.
    _restamp(unstatused, status="active")
    assert compost.CompostTracker._load_active_threads(threads_root) == [
        (_PAGE_TITLE, _INJECTED_ID)
    ]
