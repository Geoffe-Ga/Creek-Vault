"""Tests for creek.generate.compost_scan — the vault-scanning compost pass (#882).

Before this module the FEAT-018 compost detector had no production caller:
``creek compost`` exposed only ``calibrate``, which scores the detector
against a labelled fixture and never touches a vault. ``10-Liminal/Compost/``
was therefore unreachable — on a 35,330-fragment demo vault with every command
run it contained nothing but ``.gitkeep``.

These tests pin the behaviour that makes the folder reachable *honestly*:

* confirmed candidates land in ``10-Liminal/Compost/``, ambiguous ones in the
  review queue, rejected ones nowhere;
* ``--no-llm`` (modelled as ``verifier=None``) never egresses content and
  never files an unverified fragment as canonical compost;
* intimate-tier fragments never reach the verifier at all;
* a re-scan is idempotent — already-composted sources are skipped, counted,
  and not rewritten;
* the pre-flight plan reports the LLM call count *before* any call is made.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING

import frontmatter
import pytest

from creek.config import CompostConfig
from creek.generate.compost import CompostTracker
from creek.generate.compost_scan import (
    load_composted_source_ids,
    run_compost_scan,
)
from creek.generate.compost_verifier import CompostVerdict, CompostVerifierResult
from creek.models import (
    Fragment,
    FragmentSource,
    Frequency,
    FrequencyClassification,
    PraxisPotential,
    PrivacyTier,
    SourcePlatform,
    Thread,
    ThreadStatus,
    VoiceClassification,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_NOW = datetime(2026, 4, 1, 12, 0, 0)
"""Deterministic reference clock for every scan in this module."""

_CANONICAL_RELDIR = "10-Liminal/Compost"
"""Vault-relative folder that confirmed compost notes must land in."""


class _StubVerifier:
    """Deterministic verifier that records every call it receives.

    ``calls`` is the assertion surface for the privacy tests: a fragment
    that must never egress is one this list never mentions.
    """

    def __init__(
        self,
        responses: dict[str, CompostVerdict] | None = None,
        default: CompostVerdict = CompostVerdict.YES,
    ) -> None:
        """Store the title→verdict map and the fallback verdict."""
        self.responses = responses or {}
        self.default = default
        self.calls: list[tuple[str, str]] = []

    def verify(self, *, title: str, body: str) -> CompostVerifierResult:
        """Record the call and return the configured verdict."""
        self.calls.append((title, body))
        verdict = self.responses.get(title, self.default)
        return CompostVerifierResult(verdict=verdict, reasoning="stub reasoning")


def _always_high(_text: str) -> float:
    """Similarity stub clearing the default 0.6 embedding floor."""
    return 0.95


def _similarity_by_title(scores: dict[str, float]) -> Callable[[str], float]:
    """Return a similarity fn scoring by substring match on the embedded text."""

    def _fn(text: str) -> float:
        for token, value in scores.items():
            if token in text:
                return value
        return 0.0

    return _fn


@pytest.fixture()
def vault(tmp_path: Path) -> Path:
    """Create a minimal vault with the folders the scan reads and writes."""
    for rel in ("01-Fragments", "02-Threads", _CANONICAL_RELDIR):
        (tmp_path / rel).mkdir(parents=True, exist_ok=True)
    return tmp_path


_RECENT = datetime(2026, 3, 1, 9, 0, 0)
"""Default fragment authoring date — 31 days before :data:`_NOW`."""

_BACK_DATED = datetime(2024, 5, 1, 9, 0, 0)
"""Authoring date old enough to trip the 180-day project-silence gap.

Load-bearing for every project-path test in this module. With the
:data:`_RECENT` default the silence detector never fires, so a test that
asserts "no note names this tag" passes on a vault where no note was
written for any reason at all — vacuously green, which is precisely the
failure mode the privacy tests below exist to rule out.
"""


def _write_fragment(
    vault_path: Path,
    *,
    frag_id: str,
    title: str,
    body: str = "I have let this one go.",
    tags: list[str] | None = None,
    privacy_tier: PrivacyTier = PrivacyTier.UNCLASSIFIED,
    created: datetime = _RECENT,
    threads: list[str] | None = None,
    praxis_potential: PraxisPotential = PraxisPotential.NONE,
    drop_privacy_tier: bool = False,
) -> Fragment:
    """Write a fragment note into ``01-Fragments`` and return the model.

    ``created``/``ingested`` default to :data:`_RECENT` so the FEAT-031
    project-silence detector never fires incidentally — most tests here
    are about the fragment path, and a stray project candidate would make
    the plan counts ambiguous. Pass ``created=_BACK_DATED`` to exercise
    the project path deliberately.

    Args:
        vault_path: Root of the vault to write into.
        frag_id: Fragment ID, also the filename stem.
        title: Fragment title.
        body: Note body text.
        tags: Tags carried by the fragment; each one is a project identity
            to :meth:`CompostTracker._group_fragments_by_tag`.
        privacy_tier: Declared tier written into the frontmatter.
        created: Authoring timestamp, mirrored into ``ingested``.
        threads: ``[[Wikilink]]`` thread references.
        praxis_potential: ``EXPLICIT`` marks the fragment as carrying
            surviving energy, which is what puts its **title** into a
            candidate's ``energy_excerpts`` — the only path by which a
            fragment title, rather than its ID, reaches a compost note.
        drop_privacy_tier: When ``True`` the ``privacy_tier`` key is
            removed from the written frontmatter entirely, modelling a
            hand-written or legacy note. Distinct from an explicit
            ``unclassified``: the model default silently supplies
            ``unclassified`` for a *missing* key, which is why
            :func:`creek.classify.privacy_filter.raw_privacy_tier` reads
            the raw mapping instead.

    Returns:
        The :class:`~creek.models.Fragment` as written (before any
        fail-closed tier narrowing the loader applies).
    """
    fragment = Fragment(
        id=frag_id,
        title=title,
        source=FragmentSource(platform=SourcePlatform.JOURNAL),
        created=created,
        ingested=created,
        frequency=FrequencyClassification(primary=Frequency.F5),
        voice=VoiceClassification(),
        praxis_potential=praxis_potential,
        threads=threads or [],
        tags=tags or [],
        privacy_tier=privacy_tier,
    )
    metadata = fragment.model_dump(mode="json")
    if drop_privacy_tier:
        metadata.pop("privacy_tier")
    post = frontmatter.Post(content=body)
    post.metadata.update(metadata)
    path = vault_path / "01-Fragments" / f"{frag_id}.md"
    path.write_text(frontmatter.dumps(post), encoding="utf-8")
    return fragment


def _write_thread(vault_path: Path, *, thread_id: str, title: str) -> Thread:
    """Write a long-dormant thread note into ``02-Threads``."""
    thread = Thread(
        id=thread_id,
        title=title,
        status=ThreadStatus.DORMANT,
        first_seen=date(2024, 1, 1),
        last_seen=date(2025, 1, 1),
        fragment_count=3,
        frequency_affinity=[Frequency.F5],
        description="Ran hot for a season and then went quiet.",
    )
    post = frontmatter.Post(content="A thread that fell silent.")
    post.metadata.update(thread.model_dump(mode="json"))
    path = vault_path / "02-Threads" / f"{thread_id}.md"
    path.write_text(frontmatter.dumps(post), encoding="utf-8")
    return thread


def _notes_in(vault_path: Path, relpath: str) -> list[Path]:
    """Return every compost note under *relpath*, excluding the report shell."""
    folder = vault_path / relpath
    if not folder.exists():
        return []
    return [p for p in sorted(folder.glob("*.md")) if not p.name.startswith("_")]


def _compost_tree(vault_path: Path) -> list[Path]:
    """Return every markdown file under the whole ``10-Liminal/Compost`` subtree.

    ``rglob``, not ``glob``: :data:`CompostConfig.review_queue_relpath`
    defaults to ``10-Liminal/Compost/Review``, and :func:`_notes_in` — like
    :meth:`CompostTracker._load_existing_compost_notes` — only walks the top
    level. A privacy assertion that misses the review subtree misses half
    the surface.
    """
    folder = vault_path / _CANONICAL_RELDIR
    if not folder.exists():
        return []
    return sorted(folder.rglob("*.md"))


def _compost_tree_text(vault_path: Path) -> str:
    """Return the concatenated bytes of every note in the compost subtree.

    Assertions run against written vault content rather than against
    :class:`CompostCandidate` models, because the leak this guards is in
    what lands on disk — including in filenames, which no model-level
    assertion can see.
    """
    return "\n".join(p.read_text(encoding="utf-8") for p in _compost_tree(vault_path))


# ---- Routing: confirmed / ambiguous / rejected ----


def test_confirmed_fragment_lands_in_the_canonical_compost_folder(
    vault: Path,
) -> None:
    """A ``yes`` verdict writes a note into ``10-Liminal/Compost/``."""
    _write_fragment(vault, frag_id="frag-yes", title="Letting the zine go")

    result = run_compost_scan(
        vault,
        similarity_fn=_always_high,
        verifier=_StubVerifier(default=CompostVerdict.YES),
        config=CompostConfig(),
        now=_NOW,
    )

    assert len(result.composted) == 1
    assert result.review_queued == []
    written = _notes_in(vault, _CANONICAL_RELDIR)
    assert len(written) == 1
    post = frontmatter.load(str(written[0]))
    assert post.get("original_fragment") == "[[frag-yes]]"
    assert post.get("type") == "compost"


def test_ambiguous_fragment_routes_to_the_review_queue(vault: Path) -> None:
    """An ``ambiguous`` verdict files under the review queue, not canonically."""
    _write_fragment(vault, frag_id="frag-maybe", title="Unsure about the zine")

    config = CompostConfig()
    result = run_compost_scan(
        vault,
        similarity_fn=_always_high,
        verifier=_StubVerifier(default=CompostVerdict.AMBIGUOUS),
        config=config,
        now=_NOW,
    )

    assert result.composted == []
    assert len(result.review_queued) == 1
    assert _notes_in(vault, _CANONICAL_RELDIR) == []
    queued = _notes_in(vault, config.review_queue_relpath)
    assert len(queued) == 1
    assert "compost-review" in frontmatter.load(str(queued[0])).get("tags", [])


def test_rejected_fragment_writes_no_note_anywhere(vault: Path) -> None:
    """A ``no`` verdict is dropped — the embedding gate does not get the last word."""
    _write_fragment(vault, frag_id="frag-no", title="Still working on the zine")

    config = CompostConfig()
    result = run_compost_scan(
        vault,
        similarity_fn=_always_high,
        verifier=_StubVerifier(default=CompostVerdict.NO),
        config=config,
        now=_NOW,
    )

    assert result.composted == []
    assert result.review_queued == []
    assert _notes_in(vault, _CANONICAL_RELDIR) == []
    assert _notes_in(vault, config.review_queue_relpath) == []


def test_below_threshold_fragment_is_never_a_candidate(vault: Path) -> None:
    """The embedding gate still filters — a low-similarity fragment is skipped."""
    _write_fragment(vault, frag_id="frag-low", title="Groceries")
    verifier = _StubVerifier()

    result = run_compost_scan(
        vault,
        similarity_fn=_similarity_by_title({"Groceries": 0.1}),
        verifier=verifier,
        config=CompostConfig(),
        now=_NOW,
    )

    assert verifier.calls == []
    assert result.plan.llm_calls == 0
    assert result.composted == []


# ---- --no-llm: never egress, never file unverified as canonical ----


def test_no_llm_routes_every_fragment_candidate_to_review(vault: Path) -> None:
    """Without a verifier the gate's word is not enough to file canonically.

    This is the honesty guarantee: an embedding-only hit is a *suspicion*,
    so it goes to the operator queue rather than being asserted as compost.
    """
    _write_fragment(vault, frag_id="frag-gate", title="Maybe done with the zine")

    config = CompostConfig()
    result = run_compost_scan(
        vault,
        similarity_fn=_always_high,
        verifier=None,
        config=config,
        now=_NOW,
    )

    assert result.composted == []
    assert len(result.review_queued) == 1
    assert result.plan.llm_calls == 0
    assert _notes_in(vault, _CANONICAL_RELDIR) == []
    assert len(_notes_in(vault, config.review_queue_relpath)) == 1


def test_thread_candidates_stay_canonical_without_a_verifier(vault: Path) -> None:
    """Thread dormancy is deterministic, so ``--no-llm`` does not demote it.

    Only the *fragment* path depends on the LLM. Routing a dormancy verdict
    to the review queue would ask the operator to re-confirm arithmetic.
    """
    _write_thread(vault, thread_id="thread-quiet", title="Sourdough experiments")

    result = run_compost_scan(
        vault,
        similarity_fn=_always_high,
        verifier=None,
        config=CompostConfig(),
        now=_NOW,
    )

    assert result.review_queued == []
    assert len(result.composted) == 1
    post = frontmatter.load(str(result.composted[0]))
    assert post.get("original_thread") == "[[thread-quiet]]"


# ---- Privacy: intimate content never reaches the verifier ----


def test_intimate_fragment_never_reaches_the_verifier(vault: Path) -> None:
    """An intimate fragment is dropped before the verifier can see it.

    ``creek/cli.py``'s ``_build_compost_verifier`` carries a durable caveat:
    a vault-scanning compost path feeds *fragment content* to the verifier,
    so the Intimate-never-cloud rule has to hold here structurally rather
    than by the operator remembering a flag. The verifier's call log is the
    proof — an intimate fragment must not appear in it even when its
    embedding similarity is maximal.
    """
    _write_fragment(
        vault,
        frag_id="frag-intimate",
        title="Letting go of something private",
        privacy_tier=PrivacyTier.INTIMATE,
    )
    verifier = _StubVerifier(default=CompostVerdict.YES)

    result = run_compost_scan(
        vault,
        similarity_fn=_always_high,
        verifier=verifier,
        config=CompostConfig(),
        now=_NOW,
    )

    assert verifier.calls == []
    assert result.composted == []
    assert result.review_queued == []


def test_intimate_fragment_is_skipped_even_without_a_verifier(vault: Path) -> None:
    """``--no-llm`` does not license writing intimate content into the queue."""
    _write_fragment(
        vault,
        frag_id="frag-intimate2",
        title="Private release",
        privacy_tier=PrivacyTier.INTIMATE,
    )

    result = run_compost_scan(
        vault,
        similarity_fn=_always_high,
        verifier=None,
        config=CompostConfig(),
        now=_NOW,
    )

    assert result.composted == []
    assert result.review_queued == []


def test_paradox_fragment_is_skipped_when_configured(vault: Path) -> None:
    """Paradox notes hold contradiction by design and are not compost."""
    _write_fragment(
        vault,
        frag_id="frag-paradox",
        title="Both true at once",
        tags=["paradox"],
    )
    verifier = _StubVerifier()

    result = run_compost_scan(
        vault,
        similarity_fn=_always_high,
        verifier=verifier,
        config=CompostConfig(skip_paradox=True),
        now=_NOW,
    )

    assert verifier.calls == []
    assert result.composted == []


# ---- Privacy: withheld fragments reach no note, no filename, no report ----
#
# Issue #1311. ``skip_intimate`` used to guard only the *fragment* detection
# path, so a withheld fragment's title and ``[[id]]`` still landed in thread
# and project notes — and, for projects, the intimate-derived *tag* became the
# note's ``title``, its ``original_project``, its FILENAME, and a line in
# ``_Compost-Report.md``. Filtering the emitted ``fragment_ids`` /
# ``energy_excerpts`` lists — the fix the issue body proposed — does not close
# that last channel, because the project candidate's identity *is* the tag.
# These tests therefore assert on written vault bytes and on note names.


_WITHHELD_KINDS: tuple[str, ...] = ("intimate", "paradox")
"""The two skip policies that must behave identically at the boundary.

Every leak test below is parametrised over this tuple. An emptied
parametrize list makes privacy tests vanish behind a green gate, so
:func:`test_the_withheld_kind_matrix_covers_both_skip_policies` pins its
contents and the suite is run with ``-rs`` to confirm nothing is skipped.
"""


def _never_similar(_text: str) -> float:
    """Similarity stub below every floor — isolates the thread/project paths."""
    return 0.0


def _write_withheld(
    vault_path: Path,
    *,
    kind: str,
    frag_id: str,
    title: str,
    tags: list[str] | None = None,
    threads: list[str] | None = None,
    created: datetime = _RECENT,
    praxis_potential: PraxisPotential = PraxisPotential.NONE,
) -> Fragment:
    """Write a fragment that compost detection must withhold.

    Args:
        vault_path: Root of the vault to write into.
        kind: ``"intimate"`` (declares ``privacy_tier: intimate``) or
            ``"paradox"`` (adds the ``paradox`` tag).
        frag_id: Fragment ID, also the filename stem.
        title: Fragment title — the string that must not escape.
        tags: Additional tags; each is a project identity to the detector.
        threads: ``[[Wikilink]]`` thread references.
        created: Authoring timestamp, mirrored into ``ingested``.
        praxis_potential: ``EXPLICIT`` routes the title into
            ``energy_excerpts``, putting it on the leak surface.

    Returns:
        The written :class:`~creek.models.Fragment`.
    """
    extra_tags = list(tags or [])
    privacy_tier = PrivacyTier.UNCLASSIFIED
    if kind == "intimate":
        privacy_tier = PrivacyTier.INTIMATE
    else:
        extra_tags.append("paradox")
    return _write_fragment(
        vault_path,
        frag_id=frag_id,
        title=title,
        tags=extra_tags,
        privacy_tier=privacy_tier,
        created=created,
        threads=threads,
        praxis_potential=praxis_potential,
    )


def test_the_withheld_kind_matrix_covers_both_skip_policies() -> None:
    """Guard the parametrize list itself against silently emptying out."""
    assert set(_WITHHELD_KINDS) == {"intimate", "paradox"}


@pytest.mark.parametrize("kind", _WITHHELD_KINDS)
def test_a_tag_carried_only_by_withheld_fragments_produces_no_compost_note(
    vault: Path,
    kind: str,
) -> None:
    """The criterion the issue body omits: the *tag itself* is the leak.

    A project candidate's ``source_id`` and ``title`` are the tag, so a tag
    carried only by withheld fragments must yield no candidate at all —
    otherwise the tag reaches the filename, the frontmatter, the body, and
    the rollup report even after every fragment ID has been filtered out.
    """
    for idx in (1, 2):
        _write_withheld(
            vault,
            kind=kind,
            frag_id=f"f-hidden-{idx}",
            title=f"Therapy: the affair and the shame ({idx})",
            tags=["affair-recovery"],
            created=_BACK_DATED,
        )
    config = CompostConfig()

    result = run_compost_scan(
        vault,
        similarity_fn=_never_similar,
        verifier=None,
        config=config,
        now=_NOW,
    )
    CompostTracker(now=_NOW).generate_compost_report(vault)

    assert result.composted == []
    assert result.review_queued == []
    assert _notes_in(vault, _CANONICAL_RELDIR) == []
    assert not any("affair-recovery" in p.name for p in _compost_tree(vault))
    text = _compost_tree_text(vault)
    for secret in ("affair-recovery", "f-hidden-1", "f-hidden-2", "the affair"):
        assert secret not in text
    assert (
        load_composted_source_ids(
            vault,
            review_queue_relpath=config.review_queue_relpath,
        )
        == set()
    )


@pytest.mark.parametrize("kind", _WITHHELD_KINDS)
def test_a_withheld_fragment_never_reaches_a_thread_compost_note(
    vault: Path,
    kind: str,
) -> None:
    """A dormant thread's note names its open fragments and only those.

    Both fragments are marked ``EXPLICIT`` so both titles are on the
    ``energy_excerpts`` surface: the assertion is that the guard subtracts
    the withheld one rather than that titles never appear.
    """
    _write_thread(vault, thread_id="thr-deep", title="Deep Work")
    _write_withheld(
        vault,
        kind=kind,
        frag_id="f-hidden-1",
        title="Therapy: the affair and the shame",
        threads=["[[Deep Work]]"],
        praxis_potential=PraxisPotential.EXPLICIT,
    )
    _write_fragment(
        vault,
        frag_id="f-open-1",
        title="Timeboxing experiment",
        threads=["[[Deep Work]]"],
        praxis_potential=PraxisPotential.EXPLICIT,
    )

    result = run_compost_scan(
        vault,
        similarity_fn=_never_similar,
        verifier=None,
        config=CompostConfig(),
        now=_NOW,
    )
    CompostTracker(now=_NOW).generate_compost_report(vault)

    assert len(result.composted) == 1
    text = _compost_tree_text(vault)
    # Negative branch: withholding must subtract, not suppress wholesale.
    assert "f-open-1" in text
    assert "Timeboxing experiment" in text
    assert "f-hidden-1" not in text
    assert "the affair" not in text
    assert not any("affair" in p.name for p in _compost_tree(vault))


@pytest.mark.parametrize("kind", _WITHHELD_KINDS)
def test_a_dormant_thread_whose_only_fragments_are_withheld_writes_no_note(
    vault: Path,
    kind: str,
) -> None:
    """SILENT OMISSION is the contract — an empty note is itself a disclosure.

    Writing the note with ``_No related fragments were recorded._`` in place
    of the fragment list announces "this thread's only fragments are
    protected", which is the fact being protected. The candidate is dropped.
    """
    _write_thread(vault, thread_id="thr-deep", title="Deep Work")
    _write_withheld(
        vault,
        kind=kind,
        frag_id="f-hidden-1",
        title="Therapy: the affair and the shame",
        threads=["[[Deep Work]]"],
    )

    result = run_compost_scan(
        vault,
        similarity_fn=_never_similar,
        verifier=None,
        config=CompostConfig(),
        now=_NOW,
    )
    CompostTracker(now=_NOW).generate_compost_report(vault)

    assert result.composted == []
    assert _notes_in(vault, _CANONICAL_RELDIR) == []
    text = _compost_tree_text(vault)
    assert "_No related fragments were recorded._" not in text
    assert "Deep Work" not in text
    assert "f-hidden-1" not in text


def test_a_dormant_thread_with_no_fragments_at_all_still_composts(
    vault: Path,
) -> None:
    """REGRESSION FENCE, not a mutation test — expected green before and after.

    Suppression is conditioned on *withheld-ness*, never on emptiness.
    ``tests/test_compost.py::TestDetectCompostCandidates::
    test_dormant_thread_detected`` passes ``fragments=[]`` and requires a
    candidate, so simplifying the branch to ``if not related: continue``
    would silently stop composting every thread whose fragments have not
    been ingested yet. This test exists to make that simplification fail.
    """
    _write_thread(vault, thread_id="thr-lonely", title="Sourdough experiments")

    result = run_compost_scan(
        vault,
        similarity_fn=_never_similar,
        verifier=None,
        config=CompostConfig(),
        now=_NOW,
    )

    assert len(result.composted) == 1
    assert frontmatter.load(str(result.composted[0])).get("original_thread") == (
        "[[thr-lonely]]"
    )


# ---- Privacy: failing closed on tiers the model would default open ----


def test_a_fragment_with_no_privacy_tier_key_is_withheld(vault: Path) -> None:
    """A missing ``privacy_tier`` key must read INTIMATE, not ``unclassified``.

    ``Fragment.privacy_tier`` defaults to ``unclassified`` when the key is
    absent, so reading the tier off the validated model admits a note that
    never declared a tier at all.
    :func:`creek.classify.privacy_filter.raw_privacy_tier` — the house
    fail-closed reader, mirrored onto liminal fragments by
    ``creek.generate.mining`` — resolves the same file to ``intimate``. Two
    readers must not disagree about one file, so the scan's loader
    materialises the raw-derived tier before the tracker ever sees it.
    """
    for idx in (1, 2):
        _write_fragment(
            vault,
            frag_id=f"f-untiered-{idx}",
            title=f"Notes from the untiered project ({idx})",
            tags=["untiered-project"],
            created=_BACK_DATED,
            drop_privacy_tier=True,
        )

    result = run_compost_scan(
        vault,
        similarity_fn=_never_similar,
        verifier=None,
        config=CompostConfig(),
        now=_NOW,
    )
    CompostTracker(now=_NOW).generate_compost_report(vault)

    assert result.composted == []
    assert _notes_in(vault, _CANONICAL_RELDIR) == []
    assert not any("untiered-project" in p.name for p in _compost_tree(vault))
    assert "untiered-project" not in _compost_tree_text(vault)


def test_an_explicitly_unclassified_fragment_is_still_admitted(vault: Path) -> None:
    """The narrowing is scoped to the *missing* key, not to every open note.

    An explicit ``unclassified`` ranks with ``personal`` (#876/#961) and is
    admitted; only the absent key fails closed. Without this negative branch
    "withhold everything" would satisfy every other privacy test here.
    """
    for idx in (1, 2):
        _write_fragment(
            vault,
            frag_id=f"f-open-{idx}",
            title=f"Notes from the open project ({idx})",
            tags=["open-project"],
            created=_BACK_DATED,
            privacy_tier=PrivacyTier.UNCLASSIFIED,
        )

    result = run_compost_scan(
        vault,
        similarity_fn=_never_similar,
        verifier=None,
        config=CompostConfig(),
        now=_NOW,
    )

    assert len(result.composted) == 1
    assert any("open-project" in p.name for p in _compost_tree(vault))


def test_a_fragment_with_an_unrecognised_privacy_tier_never_contributes(
    vault: Path,
) -> None:
    """PIN, not a mutation-tested assertion — this is already green today.

    An unrecognised tier string fails ``Fragment.model_validate``, so
    ``creek.vault.reader.try_load_fragment`` drops the note before the
    tracker sees it. That is fail-closed *by accident*: a future
    ``mode="before"`` coercer on ``Fragment.privacy_tier``, of the kind
    ``AuthorProfile.default_privacy_tier`` already carries, would make the
    note load with a defaulted tier and silently admit it. This test is what
    turns that accident into a contract.
    """
    for idx in (1, 2):
        _write_fragment(
            vault,
            frag_id=f"f-bad-{idx}",
            title=f"Notes from the secret project ({idx})",
            tags=["secret-project"],
            created=_BACK_DATED,
        )
        path = vault / "01-Fragments" / f"f-bad-{idx}.md"
        post = frontmatter.load(str(path))
        post.metadata["privacy_tier"] = "super-secret"
        path.write_text(frontmatter.dumps(post), encoding="utf-8")

    result = run_compost_scan(
        vault,
        similarity_fn=_never_similar,
        verifier=None,
        config=CompostConfig(),
        now=_NOW,
    )
    CompostTracker(now=_NOW).generate_compost_report(vault)

    assert result.composted == []
    assert not any("secret-project" in p.name for p in _compost_tree(vault))
    assert "secret-project" not in _compost_tree_text(vault)


# ---- Idempotency ----


def test_rescan_does_not_rewrite_an_already_composted_fragment(vault: Path) -> None:
    """A second scan skips sources already recorded and reports the count."""
    _write_fragment(vault, frag_id="frag-once", title="Done with the zine")

    first = run_compost_scan(
        vault,
        similarity_fn=_always_high,
        verifier=_StubVerifier(default=CompostVerdict.YES),
        config=CompostConfig(),
        now=_NOW,
    )
    assert len(first.composted) == 1
    assert first.skipped_existing == 0

    second_verifier = _StubVerifier(default=CompostVerdict.YES)
    second = run_compost_scan(
        vault,
        similarity_fn=_always_high,
        verifier=second_verifier,
        config=CompostConfig(),
        now=_NOW,
    )

    assert second.composted == []
    assert second.skipped_existing == 1
    assert second_verifier.calls == []
    assert len(_notes_in(vault, _CANONICAL_RELDIR)) == 1


def test_rescan_skips_sources_sitting_in_the_review_queue(vault: Path) -> None:
    """The review queue counts as "already seen" — no duplicate filing."""
    _write_fragment(vault, frag_id="frag-queued", title="Maybe done with this")

    run_compost_scan(
        vault,
        similarity_fn=_always_high,
        verifier=_StubVerifier(default=CompostVerdict.AMBIGUOUS),
        config=CompostConfig(),
        now=_NOW,
    )
    second_verifier = _StubVerifier(default=CompostVerdict.AMBIGUOUS)
    second = run_compost_scan(
        vault,
        similarity_fn=_always_high,
        verifier=second_verifier,
        config=CompostConfig(),
        now=_NOW,
    )

    assert second.review_queued == []
    assert second.skipped_existing == 1
    assert second_verifier.calls == []


def test_load_composted_source_ids_reads_both_folders(vault: Path) -> None:
    """The idempotency index spans canonical notes and the review queue."""
    _write_fragment(vault, frag_id="frag-a", title="Released the zine")
    _write_fragment(vault, frag_id="frag-b", title="Unsure about the album")
    config = CompostConfig()

    run_compost_scan(
        vault,
        similarity_fn=_always_high,
        verifier=_StubVerifier(
            responses={
                "Released the zine": CompostVerdict.YES,
                "Unsure about the album": CompostVerdict.AMBIGUOUS,
            },
        ),
        config=config,
        now=_NOW,
    )

    seen = load_composted_source_ids(
        vault,
        review_queue_relpath=config.review_queue_relpath,
    )
    assert seen == {"frag-a", "frag-b"}


def test_load_composted_source_ids_is_empty_for_a_fresh_vault(vault: Path) -> None:
    """A vault that has never been scanned reports no composted sources."""
    assert load_composted_source_ids(vault, review_queue_relpath="x/y") == set()


# ---- Pre-flight plan ----


def test_plan_reports_llm_calls_before_any_call_is_made(vault: Path) -> None:
    """The estimate counts gate survivors, so an operator can bail on cost."""
    for idx in range(3):
        _write_fragment(vault, frag_id=f"frag-{idx}", title=f"Released project {idx}")

    result = run_compost_scan(
        vault,
        similarity_fn=_always_high,
        verifier=_StubVerifier(default=CompostVerdict.YES),
        config=CompostConfig(),
        now=_NOW,
    )

    assert result.plan.fragment_candidates == 3
    assert result.plan.llm_calls == 3


def test_dry_run_estimates_without_writing_or_verifying(vault: Path) -> None:
    """``--dry-run`` produces the plan and stops — no egress, no vault writes."""
    _write_fragment(vault, frag_id="frag-dry", title="Released the zine")
    verifier = _StubVerifier(default=CompostVerdict.YES)

    result = run_compost_scan(
        vault,
        similarity_fn=_always_high,
        verifier=verifier,
        config=CompostConfig(),
        now=_NOW,
        dry_run=True,
    )

    assert result.plan.llm_calls == 1
    assert result.composted == []
    assert result.review_queued == []
    assert verifier.calls == []
    assert _notes_in(vault, _CANONICAL_RELDIR) == []


def test_scan_on_an_empty_vault_reports_an_empty_plan(vault: Path) -> None:
    """No fragments, no threads — the scan is a clean no-op."""
    result = run_compost_scan(
        vault,
        similarity_fn=_always_high,
        verifier=_StubVerifier(),
        config=CompostConfig(),
        now=_NOW,
    )

    assert result.plan.fragment_candidates == 0
    assert result.plan.thread_candidates == 0
    assert result.plan.llm_calls == 0
    assert result.composted == []


def test_embedding_threshold_from_config_is_honoured(vault: Path) -> None:
    """A tightened floor rejects a candidate the default would have admitted."""
    _write_fragment(vault, frag_id="frag-mid", title="Middling signal")
    verifier = _StubVerifier()

    result = run_compost_scan(
        vault,
        similarity_fn=_similarity_by_title({"Middling signal": 0.7}),
        verifier=verifier,
        config=CompostConfig(embedding_threshold=0.9),
        now=_NOW,
    )

    assert verifier.calls == []
    assert result.plan.fragment_candidates == 0
