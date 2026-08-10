"""Tests for creek.generate.voice — voice exemplar collection.

Covers the ``VoiceExemplarCollector`` class implementing Section 11.1 of
the Creek Ontology: scan vault fragments for high-confidence voice
samples grouped by register, rank by quality, and persist the top
exemplars under ``07-Voice/Register-Samples/``.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

import frontmatter
import pytest

from creek.generate.voice import (
    DEFAULT_MAX_PER_REGISTER,
    DEFAULT_MIN_PER_REGISTER,
    VOICE_REGISTERS,
    VoiceExemplarCollector,
    _eligible_register,
    _is_other_authors_path,
    _is_safe_sample_stem,
)
from creek.models import (
    Authorship,
    Confidence,
    Fragment,
    FragmentSource,
    Frequency,
    FrequencyClassification,
    Mode,
    Phase,
    PrivacyTier,
    SourcePlatform,
    VoiceClassification,
    VoiceRegister,
    WavelengthClassification,
)

if TYPE_CHECKING:
    from pathlib import Path


# ---- Fixtures ----


@pytest.fixture()
def vault(tmp_path: Path) -> Path:
    """Create a minimal vault tree with the folders the collector touches."""
    for folder in (
        "01-Fragments/Journal",
        "01-Fragments/Conversations",
        "01-Fragments/Writing",
        "07-Voice/Register-Samples",
    ):
        (tmp_path / folder).mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture()
def collector() -> VoiceExemplarCollector:
    """Create a default VoiceExemplarCollector."""
    return VoiceExemplarCollector()


def _build_fragment(
    *,
    frag_id: str,
    title: str,
    register: VoiceRegister | None,
    confidence: Confidence | None,
    privacy: PrivacyTier = PrivacyTier.PERSONAL,
    fully_classified: bool = True,
    voice_weight: float | None = None,
    author: Authorship = Authorship.SELF,
) -> Fragment:
    """Build a Fragment with the desired voice / privacy / classification state.

    ``voice_weight`` is omitted from the constructor call when ``None`` so a
    test can exercise the *model default* (``1.0``, ``creek/models.py``)
    rather than a value the helper supplied — the distinction #1213 turns on,
    since a non-self fragment with no explicit weight is exactly what
    ``DocumentIngestor`` produces.
    """
    if fully_classified:
        frequency = FrequencyClassification(primary=Frequency.F5)
        wavelength = WavelengthClassification(
            phase=Phase.RISING,
            mode=Mode.EXPRESS,
        )
    else:
        frequency = FrequencyClassification()
        wavelength = WavelengthClassification()
    weight_kwargs = {} if voice_weight is None else {"voice_weight": voice_weight}
    return Fragment(
        id=frag_id,
        title=title,
        source=FragmentSource(platform=SourcePlatform.JOURNAL, author=author),
        created=datetime(2026, 1, 15, 12, 0, 0),
        ingested=datetime(2026, 1, 15, 12, 0, 0),
        frequency=frequency,
        wavelength=wavelength,
        voice=VoiceClassification(voice_register=register, confidence=confidence),
        privacy_tier=privacy,
        **weight_kwargs,
    )


def _write_fragment(
    vault_path: Path,
    fragment: Fragment,
    *,
    body_words: int,
    subfolder: str = "Journal",
) -> Path:
    """Persist *fragment* under ``01-Fragments/{subfolder}/`` with a body."""
    body = " ".join(["word"] * body_words)
    data = fragment.model_dump(mode="json")
    post = frontmatter.Post(content=body, **data)
    target = vault_path / "01-Fragments" / subfolder / f"{fragment.id}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(frontmatter.dumps(post), encoding="utf-8")
    return target


# ---- Module surface ----


class TestModuleSurface:
    """Exported constants must match the issue specification."""

    def test_voice_registers_covers_seven_canonical_registers(self) -> None:
        """All seven ontology voice registers must be enumerated."""
        expected = {
            "confessional",
            "analytical",
            "playful",
            "prophetic",
            "instructional",
            "raw",
            "conversational",
        }
        assert set(VOICE_REGISTERS) == expected

    def test_default_min_is_five(self) -> None:
        """Default minimum exemplars per register should follow the spec."""
        assert DEFAULT_MIN_PER_REGISTER == 5

    def test_default_max_is_twenty(self) -> None:
        """Default maximum exemplars per register should follow the spec."""
        assert DEFAULT_MAX_PER_REGISTER == 20


# ---- collect_exemplars ----


class TestCollectExemplars:
    """Scan the vault, filter by confidence/privacy, group by register."""

    def test_collects_settled_and_conviction_only(
        self,
        vault: Path,
        collector: VoiceExemplarCollector,
    ) -> None:
        """Only ``settled`` and ``conviction`` confidence levels qualify."""
        keep = _build_fragment(
            frag_id="frag-keep1",
            title="Keep me",
            register=VoiceRegister.CONFESSIONAL,
            confidence=Confidence.SETTLED,
        )
        keep2 = _build_fragment(
            frag_id="frag-keep2",
            title="Keep me too",
            register=VoiceRegister.CONFESSIONAL,
            confidence=Confidence.CONVICTION,
        )
        drop = _build_fragment(
            frag_id="frag-drop1",
            title="Drop me",
            register=VoiceRegister.CONFESSIONAL,
            confidence=Confidence.MUSING,
        )
        for frag in (keep, keep2, drop):
            _write_fragment(vault, frag, body_words=400)

        exemplars = collector.collect_exemplars(vault)
        ids = {f.id for f in exemplars["confessional"]}
        assert ids == {"frag-keep1", "frag-keep2"}

    def test_groups_by_register(
        self,
        vault: Path,
        collector: VoiceExemplarCollector,
    ) -> None:
        """Fragments end up in the bucket matching their voice register."""
        confess = _build_fragment(
            frag_id="frag-c1",
            title="A confession",
            register=VoiceRegister.CONFESSIONAL,
            confidence=Confidence.SETTLED,
        )
        analytic = _build_fragment(
            frag_id="frag-a1",
            title="An analysis",
            register=VoiceRegister.ANALYTICAL,
            confidence=Confidence.SETTLED,
        )
        _write_fragment(vault, confess, body_words=400)
        _write_fragment(vault, analytic, body_words=400, subfolder="Writing")

        exemplars = collector.collect_exemplars(vault)
        assert [f.id for f in exemplars["confessional"]] == ["frag-c1"]
        assert [f.id for f in exemplars["analytical"]] == ["frag-a1"]

    def test_returns_all_seven_register_keys(
        self,
        vault: Path,
        collector: VoiceExemplarCollector,
    ) -> None:
        """Every register key is present, even when empty."""
        exemplars = collector.collect_exemplars(vault)
        assert set(exemplars) == set(VOICE_REGISTERS)
        assert all(value == [] for value in exemplars.values())

    def test_excludes_intimate_by_default(
        self,
        vault: Path,
        collector: VoiceExemplarCollector,
    ) -> None:
        """Intimate-tier fragments are dropped unless explicitly opted in."""
        intimate = _build_fragment(
            frag_id="frag-int1",
            title="Intimate piece",
            register=VoiceRegister.RAW,
            confidence=Confidence.SETTLED,
            privacy=PrivacyTier.INTIMATE,
        )
        _write_fragment(vault, intimate, body_words=400)
        exemplars = collector.collect_exemplars(vault)
        assert exemplars["raw"] == []

    def test_includes_intimate_when_opted_in(
        self,
        vault: Path,
    ) -> None:
        """Setting ``allow_intimate=True`` includes intimate-tier fragments."""
        intimate = _build_fragment(
            frag_id="frag-int1",
            title="Intimate piece",
            register=VoiceRegister.RAW,
            confidence=Confidence.SETTLED,
            privacy=PrivacyTier.INTIMATE,
        )
        _write_fragment(vault, intimate, body_words=400)
        opted_in = VoiceExemplarCollector(allow_intimate=True)
        exemplars = opted_in.collect_exemplars(vault)
        assert [f.id for f in exemplars["raw"]] == ["frag-int1"]

    def test_skips_fragments_without_register(
        self,
        vault: Path,
        collector: VoiceExemplarCollector,
    ) -> None:
        """Fragments missing a voice_register are not collected."""
        no_register = _build_fragment(
            frag_id="frag-none",
            title="No register",
            register=None,
            confidence=Confidence.SETTLED,
        )
        _write_fragment(vault, no_register, body_words=400)
        exemplars = collector.collect_exemplars(vault)
        for frags in exemplars.values():
            assert "frag-none" not in {f.id for f in frags}

    def test_warns_when_register_below_minimum(
        self,
        vault: Path,
        collector: VoiceExemplarCollector,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Registers with fewer than the minimum exemplars emit a warning."""
        single = _build_fragment(
            frag_id="frag-only1",
            title="Lone exemplar",
            register=VoiceRegister.PROPHETIC,
            confidence=Confidence.CONVICTION,
        )
        _write_fragment(vault, single, body_words=400)
        with caplog.at_level(logging.WARNING, logger="creek.generate.voice"):
            collector.collect_exemplars(vault)
        assert any("prophetic" in record.message for record in caplog.records)

    def test_handles_missing_fragments_directory(
        self,
        tmp_path: Path,
        collector: VoiceExemplarCollector,
    ) -> None:
        """A vault without ``01-Fragments`` returns the empty-bucket dict."""
        exemplars = collector.collect_exemplars(tmp_path)
        assert set(exemplars) == set(VOICE_REGISTERS)
        assert all(value == [] for value in exemplars.values())

    def test_skips_unparseable_files(
        self,
        vault: Path,
        collector: VoiceExemplarCollector,
    ) -> None:
        """Markdown files that fail to parse are silently skipped."""
        (vault / "01-Fragments" / "Journal" / "broken.md").write_bytes(b"\x00\x01\x02")
        good = _build_fragment(
            frag_id="frag-good",
            title="Good fragment",
            register=VoiceRegister.CONFESSIONAL,
            confidence=Confidence.SETTLED,
        )
        _write_fragment(vault, good, body_words=400)
        exemplars = collector.collect_exemplars(vault)
        assert [f.id for f in exemplars["confessional"]] == ["frag-good"]


class TestVoiceCorpusExcludesBorrowedContent:
    """Issue #466: borrowed / AI-authored text must never reach the voice corpus.

    Three additive gates protect voice fidelity, mirroring the privacy
    fail-closed rule:

    1. ``source.author == self`` — the frontmatter axis that
       :attr:`~creek.models.Fragment.voice_proxy_eligible` is derived from.
       This is the primary gate (#1213); the two below are backstops to it.
    2. ``voice_weight > 0`` — a fragment with ``voice_weight <= 0`` (the
       ``ai-as-user`` analogue with ``voice_weight=0.0``) is ineligible.
    3. ``11-Other-Authors/`` path exclusion — any fragment whose source
       path contains that segment is skipped regardless of weight.
    """

    def test_excludes_zero_voice_weight_and_other_authors_path(
        self,
        vault: Path,
        collector: VoiceExemplarCollector,
    ) -> None:
        """Only native, positive-weight fragments outside 11-Other-Authors survive."""
        native = _build_fragment(
            frag_id="frag-native",
            title="Native voice",
            register=VoiceRegister.CONFESSIONAL,
            confidence=Confidence.SETTLED,
        )
        borrowed = _build_fragment(
            frag_id="frag-ai-as-user",
            title="AI as user",
            register=VoiceRegister.CONFESSIONAL,
            confidence=Confidence.SETTLED,
            voice_weight=0.0,
        )
        other_author = _build_fragment(
            frag_id="frag-other-author",
            title="Borrowed by path",
            register=VoiceRegister.CONFESSIONAL,
            confidence=Confidence.SETTLED,
        )
        _write_fragment(vault, native, body_words=400)
        # Weight gate: ai-as-user content sits in the scanned corpus.
        _write_fragment(vault, borrowed, body_words=400)
        # Path gate: a full-weight fragment nested under an
        # ``11-Other-Authors`` segment inside the scanned tree.
        _write_fragment(
            vault,
            other_author,
            body_words=400,
            subfolder="11-Other-Authors/some-author",
        )

        exemplars = collector.collect_exemplars(vault)
        ids = {f.id for f in exemplars["confessional"]}
        assert ids == {"frag-native"}
        assert "frag-ai-as-user" not in ids
        assert "frag-other-author" not in ids

    def test_zero_voice_weight_excluded(
        self,
        vault: Path,
        collector: VoiceExemplarCollector,
    ) -> None:
        """An ai-as-user-style fragment (voice_weight=0.0) is dropped by the gate."""
        borrowed = _build_fragment(
            frag_id="frag-zero",
            title="Zero weight",
            register=VoiceRegister.ANALYTICAL,
            confidence=Confidence.CONVICTION,
            voice_weight=0.0,
        )
        _write_fragment(vault, borrowed, body_words=400)
        exemplars = collector.collect_exemplars(vault)
        assert exemplars["analytical"] == []

    def test_positive_voice_weight_is_kept(
        self,
        vault: Path,
        collector: VoiceExemplarCollector,
    ) -> None:
        """A reduced-but-positive voice_weight still qualifies for the corpus."""
        weighted = _build_fragment(
            frag_id="frag-half",
            title="Half weight",
            register=VoiceRegister.ANALYTICAL,
            confidence=Confidence.SETTLED,
            voice_weight=0.5,
        )
        _write_fragment(vault, weighted, body_words=400)
        exemplars = collector.collect_exemplars(vault)
        assert [f.id for f in exemplars["analytical"]] == ["frag-half"]

    def test_path_helper_rejects_other_authors_segment(self, tmp_path: Path) -> None:
        """The path-exclusion helper rejects an 11-Other-Authors path segment."""
        other = tmp_path / "01-Fragments" / "11-Other-Authors" / "x" / "f.md"
        native = tmp_path / "01-Fragments" / "Journal" / "f.md"
        assert _is_other_authors_path(other) is True
        assert _is_other_authors_path(native) is False

    @pytest.mark.parametrize(
        "author",
        [Authorship.AI, Authorship.OTHER, Authorship.COLLABORATIVE],
    )
    def test_excludes_non_self_authors_at_default_voice_weight(
        self,
        vault: Path,
        collector: VoiceExemplarCollector,
        author: Authorship,
    ) -> None:
        """Non-self prose is refused on authorship alone, with no weight help.

        Issue #1213. Neither backstop applies here: the fragment sits in
        ``01-Fragments/Journal/`` (so the ``11-Other-Authors`` path gate
        never sees it) and carries no explicit ``voice_weight``, so the
        model default of ``1.0`` applies. ``other`` is the live case —
        ``DocumentIngestor`` resolves a DOCX ``core_properties.author`` /
        PDF ``/Author`` to :attr:`~creek.models.Authorship.OTHER` and sets
        no weight, so a stranger's document reaches this walk today.

        The fragment round-trips through ``frontmatter`` and
        ``Fragment.model_validate``, so the gate is exercised against the
        plain ``str`` that ``FragmentSource``'s ``use_enum_values=True``
        yields at runtime — not against an enum member.
        """
        borrowed = _build_fragment(
            frag_id=f"frag-authored-{author.value}",
            title=f"Written by {author.value}",
            register=VoiceRegister.CONFESSIONAL,
            confidence=Confidence.SETTLED,
            author=author,
        )
        assert borrowed.voice_weight == 1.0
        _write_fragment(vault, borrowed, body_words=400)

        exemplars = collector.collect_exemplars(vault)

        assert [f.id for f in exemplars["confessional"]] == []

    @pytest.mark.parametrize("author", list(Authorship))
    @pytest.mark.parametrize(
        "tier",
        [PrivacyTier.OPEN, PrivacyTier.PERSONAL, PrivacyTier.INTIMATE],
    )
    def test_gate_matches_the_canonical_property_and_the_intimate_override(
        self,
        author: Authorship,
        tier: PrivacyTier,
    ) -> None:
        """The gate is equivalent to the canonical predicate, override aside.

        Two arms, and both are load-bearing (#1213):

        * At ``allow_intimate=False`` admission must equal
          :attr:`~creek.models.Fragment.voice_proxy_eligible` exactly.
        * At ``allow_intimate=True`` admission must equal *self-authorship
          alone* — the opt-in overrides the INTIMATE exclusion the property
          bakes in, and nothing else. This arm is what fails if anyone
          "simplifies" ``_eligible_register`` to
          ``return fragment.voice_proxy_eligible``: that would silently
          revoke the operator's opt-in to their own intimate writing.

        ``list(Authorship)`` is deliberate: a fifth member added later is
        excluded by the property and must be excluded here too, without
        anyone remembering to extend a hand-written list.

        The tier axis is deliberately **not** ``list(PrivacyTier)``.
        ``PrivacyTier.UNCLASSIFIED`` is omitted because #1212 will make
        ``_eligible_register`` stop admitting a self-authored *untiered*
        fragment, at which point the first arm stops holding for that cell
        while the property (which only excludes INTIMATE) still reports
        ``True``. Widening this parametrization is green today and a
        landmine tomorrow.
        """
        fragment = _build_fragment(
            frag_id="frag-equivalence",
            title="Equivalence probe",
            register=VoiceRegister.CONFESSIONAL,
            confidence=Confidence.SETTLED,
            privacy=tier,
            author=author,
        )

        admitted_default = _eligible_register(fragment, allow_intimate=False)
        admitted_opted_in = _eligible_register(fragment, allow_intimate=True)

        assert (admitted_default is not None) == fragment.voice_proxy_eligible
        assert (admitted_opted_in is not None) == (
            str(fragment.source.author) == Authorship.SELF.value
        )

    def test_self_authored_intimate_admitted_when_allow_intimate(
        self,
        vault: Path,
    ) -> None:
        """The operator's own intimate writing survives the authorship gate.

        The regression the issue body's suggested fix would have caused
        (#1213): delegating to ``voice_proxy_eligible`` bakes in the
        INTIMATE exclusion and silently drops this fragment even though the
        operator opted the voice proxy in.
        """
        mine = _build_fragment(
            frag_id="frag-self-intimate",
            title="My own intimate writing",
            register=VoiceRegister.CONFESSIONAL,
            confidence=Confidence.SETTLED,
            privacy=PrivacyTier.INTIMATE,
        )
        _write_fragment(vault, mine, body_words=400)

        exemplars = VoiceExemplarCollector(allow_intimate=True).collect_exemplars(vault)

        assert [f.id for f in exemplars["confessional"]] == ["frag-self-intimate"]

    def test_self_author_with_positive_weight_is_still_admitted(
        self,
        vault: Path,
        collector: VoiceExemplarCollector,
    ) -> None:
        """The admit direction is pinned independently of the weight gate (#1213)."""
        mine = _build_fragment(
            frag_id="frag-self-default",
            title="My own writing",
            register=VoiceRegister.PROPHETIC,
            confidence=Confidence.CONVICTION,
        )
        _write_fragment(vault, mine, body_words=400)

        exemplars = collector.collect_exemplars(vault)

        assert [f.id for f in exemplars["prophetic"]] == ["frag-self-default"]

    @pytest.mark.parametrize(
        "author",
        [Authorship.AI, Authorship.OTHER, Authorship.COLLABORATIVE],
    )
    def test_collect_all_exemplars_excludes_non_self_authors(
        self,
        vault: Path,
        collector: VoiceExemplarCollector,
        author: Authorship,
    ) -> None:
        """The flat lexicon-facing walk shares the authorship gate (#1213).

        ``collect_all_exemplars`` is a second, independent walk; asserting
        the gate on ``collect_exemplars`` alone would not cover it.
        """
        _write_fragment(
            vault,
            _build_fragment(
                frag_id=f"frag-flat-{author.value}",
                title="Borrowed",
                register=VoiceRegister.ANALYTICAL,
                confidence=Confidence.SETTLED,
                author=author,
            ),
            body_words=400,
        )

        assert collector.collect_all_exemplars(vault) == []

    def test_register_samples_are_not_written_for_non_self_authors(
        self,
        vault: Path,
    ) -> None:
        """No verbatim copy of borrowed prose lands in ``Register-Samples`` (#1213).

        ``generate_register_samples`` ``shutil.copy2``-s the source file into
        the vault byte for byte, so this surface turns an excerpt into a
        durable copy of someone else's writing.
        """
        from creek.generate.voice import generate_register_samples

        _write_fragment(
            vault,
            _build_fragment(
                frag_id="frag-samples-other",
                title="Stranger's document",
                register=VoiceRegister.INSTRUCTIONAL,
                confidence=Confidence.CONVICTION,
                author=Authorship.OTHER,
            ),
            body_words=400,
        )

        assert generate_register_samples(vault) == {}
        register_dir = vault / "07-Voice" / "Register-Samples" / "instructional"
        assert not register_dir.exists()

    def test_previously_written_non_self_samples_are_pruned(
        self,
        vault: Path,
    ) -> None:
        """A sample written before the fix is deleted on the next run (#1213/#879).

        Two runs through the public entry point. The first writes the copy
        while the fragment is still self-authored; the frontmatter is then
        rewritten on disk to ``author: other`` — the state a pre-fix vault is
        already in — and the second run must remove the copy it recorded
        writing and rewrite the summary to an honest zero. ``_save_register``
        prunes above its empty-bucket branch precisely so an emptied register
        is cleared rather than left describing a cohort that has gone.
        """
        from creek.generate.voice import generate_register_samples

        source = _write_fragment(
            vault,
            _build_fragment(
                frag_id="frag-prune-other",
                title="Later reattributed",
                register=VoiceRegister.RAW,
                confidence=Confidence.CONVICTION,
            ),
            body_words=400,
        )
        register_dir = vault / "07-Voice" / "Register-Samples" / "raw"
        copy = register_dir / "frag-prune-other.md"
        summary = register_dir / "_Summary.md"

        first_pruned: list[Path] = []
        generate_register_samples(vault, on_prune=first_pruned.append)
        assert copy.is_file()
        assert summary.is_file()
        assert first_pruned == []

        post = frontmatter.load(str(source))
        post["source"] = {**post["source"], "author": Authorship.OTHER.value}
        source.write_text(frontmatter.dumps(post), encoding="utf-8")

        second_pruned: list[Path] = []
        result = generate_register_samples(vault, on_prune=second_pruned.append)

        assert second_pruned == [copy]
        assert not copy.exists()
        assert "raw" not in result
        assert "frag-prune-other" not in summary.read_text(encoding="utf-8")

    def test_path_helper_no_substring_false_positive(self, tmp_path: Path) -> None:
        """A folder merely containing the text is not excluded (segment match only)."""
        lookalike = tmp_path / "01-Fragments" / "my-11-Other-Authors-notes" / "f.md"
        assert _is_other_authors_path(lookalike) is False

    def test_tracer_invariant_no_other_authors_unchanged(
        self,
        vault: Path,
        collector: VoiceExemplarCollector,
    ) -> None:
        """A vault with no 11-Other-Authors and default weights behaves as before."""
        a = _build_fragment(
            frag_id="frag-a",
            title="Native A",
            register=VoiceRegister.CONFESSIONAL,
            confidence=Confidence.SETTLED,
        )
        b = _build_fragment(
            frag_id="frag-b",
            title="Native B",
            register=VoiceRegister.CONFESSIONAL,
            confidence=Confidence.CONVICTION,
        )
        _write_fragment(vault, a, body_words=400)
        _write_fragment(vault, b, body_words=400)
        exemplars = collector.collect_exemplars(vault)
        assert {f.id for f in exemplars["confessional"]} == {"frag-a", "frag-b"}


# ---- rank_exemplars ----


class TestRankExemplars:
    """Ranking by confidence, content length, and classification completeness."""

    def test_conviction_outranks_settled(
        self,
        vault: Path,
        collector: VoiceExemplarCollector,
    ) -> None:
        """Conviction-confidence fragments rank above settled ones."""
        settled = _build_fragment(
            frag_id="frag-settled",
            title="Settled",
            register=VoiceRegister.ANALYTICAL,
            confidence=Confidence.SETTLED,
        )
        conviction = _build_fragment(
            frag_id="frag-conviction",
            title="Conviction",
            register=VoiceRegister.ANALYTICAL,
            confidence=Confidence.CONVICTION,
        )
        _write_fragment(vault, settled, body_words=400)
        _write_fragment(vault, conviction, body_words=400)
        collector.collect_exemplars(vault)
        ranked = collector.rank_exemplars([settled, conviction])
        assert [f.id for f in ranked] == ["frag-conviction", "frag-settled"]

    def test_medium_length_outranks_short(
        self,
        vault: Path,
        collector: VoiceExemplarCollector,
    ) -> None:
        """Medium-length (200-800 words) bodies score higher than short ones."""
        short = _build_fragment(
            frag_id="frag-short",
            title="Short",
            register=VoiceRegister.PLAYFUL,
            confidence=Confidence.SETTLED,
        )
        medium = _build_fragment(
            frag_id="frag-medium",
            title="Medium",
            register=VoiceRegister.PLAYFUL,
            confidence=Confidence.SETTLED,
        )
        _write_fragment(vault, short, body_words=50)
        _write_fragment(vault, medium, body_words=400)
        collector.collect_exemplars(vault)
        ranked = collector.rank_exemplars([short, medium])
        assert ranked[0].id == "frag-medium"

    def test_full_classification_outranks_partial(
        self,
        vault: Path,
        collector: VoiceExemplarCollector,
    ) -> None:
        """Fragments with full classification rank above partial ones."""
        partial = _build_fragment(
            frag_id="frag-partial",
            title="Partial",
            register=VoiceRegister.INSTRUCTIONAL,
            confidence=Confidence.SETTLED,
            fully_classified=False,
        )
        complete = _build_fragment(
            frag_id="frag-complete",
            title="Complete",
            register=VoiceRegister.INSTRUCTIONAL,
            confidence=Confidence.SETTLED,
            fully_classified=True,
        )
        _write_fragment(vault, partial, body_words=400)
        _write_fragment(vault, complete, body_words=400)
        collector.collect_exemplars(vault)
        ranked = collector.rank_exemplars([partial, complete])
        assert ranked[0].id == "frag-complete"

    def test_top_n_truncation(self, vault: Path) -> None:
        """rank_exemplars returns at most ``max_per_register`` entries."""
        fragments: list[Fragment] = []
        for idx in range(6):
            frag = _build_fragment(
                frag_id=f"frag-rank-{idx}",
                title=f"Frag {idx}",
                register=VoiceRegister.CONVERSATIONAL,
                confidence=Confidence.SETTLED,
            )
            _write_fragment(vault, frag, body_words=400)
            fragments.append(frag)
        small = VoiceExemplarCollector(max_per_register=3, min_per_register=1)
        small.collect_exemplars(vault)
        ranked = small.rank_exemplars(fragments)
        assert len(ranked) == 3

    def test_empty_input_returns_empty(
        self,
        collector: VoiceExemplarCollector,
    ) -> None:
        """Empty input yields an empty ranked list."""
        assert collector.rank_exemplars([]) == []


# ---- save_exemplars ----


class TestSaveExemplars:
    """Persist top exemplars and per-register summary notes to the vault."""

    def test_copies_fragment_files_to_register_folder(
        self,
        vault: Path,
        collector: VoiceExemplarCollector,
    ) -> None:
        """Fragment markdown files land under the matching register folder."""
        frag = _build_fragment(
            frag_id="frag-save1",
            title="Saved",
            register=VoiceRegister.CONFESSIONAL,
            confidence=Confidence.SETTLED,
        )
        _write_fragment(vault, frag, body_words=400)
        exemplars = collector.collect_exemplars(vault)
        collector.save_exemplars(exemplars, vault)
        target = (
            vault / "07-Voice" / "Register-Samples" / "confessional" / "frag-save1.md"
        )
        assert target.exists()

    def test_summary_note_written_per_register(
        self,
        vault: Path,
        collector: VoiceExemplarCollector,
    ) -> None:
        """Each non-empty register gets a summary note with stats."""
        frag = _build_fragment(
            frag_id="frag-sum1",
            title="Summary fodder",
            register=VoiceRegister.PLAYFUL,
            confidence=Confidence.CONVICTION,
        )
        _write_fragment(vault, frag, body_words=400)
        exemplars = collector.collect_exemplars(vault)
        collector.save_exemplars(exemplars, vault)
        summary = vault / "07-Voice" / "Register-Samples" / "playful" / "_Summary.md"
        assert summary.exists()
        post = frontmatter.load(str(summary))
        assert post["voice_register"] == "playful"
        assert post["exemplar_count"] == 1
        assert post["type"] == "voice-register-summary"

    def test_summary_includes_per_confidence_breakdown(
        self,
        vault: Path,
        collector: VoiceExemplarCollector,
    ) -> None:
        """Summary frontmatter records the conviction/settled split."""
        a = _build_fragment(
            frag_id="frag-conv1",
            title="Conviction sample",
            register=VoiceRegister.PROPHETIC,
            confidence=Confidence.CONVICTION,
        )
        b = _build_fragment(
            frag_id="frag-settle1",
            title="Settled sample",
            register=VoiceRegister.PROPHETIC,
            confidence=Confidence.SETTLED,
        )
        _write_fragment(vault, a, body_words=400)
        _write_fragment(vault, b, body_words=400)
        exemplars = collector.collect_exemplars(vault)
        collector.save_exemplars(exemplars, vault)
        summary = vault / "07-Voice" / "Register-Samples" / "prophetic" / "_Summary.md"
        post = frontmatter.load(str(summary))
        assert post["conviction_count"] == 1
        assert post["settled_count"] == 1

    def test_skips_empty_registers(
        self,
        vault: Path,
        collector: VoiceExemplarCollector,
    ) -> None:
        """Empty register buckets do not create a folder or summary."""
        exemplars = collector.collect_exemplars(vault)
        collector.save_exemplars(exemplars, vault)
        for register in VOICE_REGISTERS:
            folder = vault / "07-Voice" / "Register-Samples" / register
            assert not folder.exists()

    def test_truncates_to_max_per_register(self, vault: Path) -> None:
        """save_exemplars writes only the top ``max_per_register`` files."""
        for idx in range(4):
            frag = _build_fragment(
                frag_id=f"frag-trunc-{idx}",
                title=f"Trunc {idx}",
                register=VoiceRegister.RAW,
                confidence=Confidence.SETTLED,
            )
            _write_fragment(vault, frag, body_words=400)
        small = VoiceExemplarCollector(max_per_register=2, min_per_register=1)
        exemplars = small.collect_exemplars(vault)
        small.save_exemplars(exemplars, vault)
        register_dir = vault / "07-Voice" / "Register-Samples" / "raw"
        copied = sorted(
            p.name for p in register_dir.glob("*.md") if p.name != "_Summary.md"
        )
        assert len(copied) == 2

    def test_returns_summary_paths(
        self,
        vault: Path,
        collector: VoiceExemplarCollector,
    ) -> None:
        """save_exemplars returns the per-register summary paths."""
        frag = _build_fragment(
            frag_id="frag-return1",
            title="Return path",
            register=VoiceRegister.INSTRUCTIONAL,
            confidence=Confidence.SETTLED,
        )
        _write_fragment(vault, frag, body_words=400)
        exemplars = collector.collect_exemplars(vault)
        result = collector.save_exemplars(exemplars, vault)
        assert "instructional" in result
        assert result["instructional"].name == "_Summary.md"

    def test_save_without_collect_falls_back_to_dump(
        self,
        vault: Path,
        collector: VoiceExemplarCollector,
    ) -> None:
        """Saving without a prior collect serialises fragments from memory."""
        frag = _build_fragment(
            frag_id="frag-mem1",
            title="In-memory only",
            register=VoiceRegister.ANALYTICAL,
            confidence=Confidence.CONVICTION,
        )
        result = collector.save_exemplars({"analytical": [frag]}, vault)
        target = vault / "07-Voice" / "Register-Samples" / "analytical" / "frag-mem1.md"
        assert target.exists()
        assert "analytical" in result

    def test_save_skips_unknown_register_keys(
        self,
        vault: Path,
        collector: VoiceExemplarCollector,
    ) -> None:
        """Non-canonical register keys are silently skipped."""
        frag = _build_fragment(
            frag_id="frag-bogus",
            title="Bogus register",
            register=VoiceRegister.ANALYTICAL,
            confidence=Confidence.SETTLED,
        )
        result = collector.save_exemplars({"made-up-register": [frag]}, vault)
        assert result == {}

    def test_summary_body_lists_zero_exemplars_when_empty(
        self,
    ) -> None:
        """The internal summary renderer handles the no-ranked-exemplars case."""
        body = VoiceExemplarCollector._render_summary_body("playful", [], 0, 0)
        assert "_No exemplars collected._" in body


# ---- Init validation ----


class TestInitValidation:
    """Constructor argument validation."""

    def test_max_per_register_must_be_positive(self) -> None:
        """Passing a non-positive ``max_per_register`` raises ``ValueError``."""
        with pytest.raises(ValueError, match="max_per_register"):
            VoiceExemplarCollector(max_per_register=0)

    def test_min_per_register_must_be_positive(self) -> None:
        """Passing a non-positive ``min_per_register`` raises ``ValueError``."""
        with pytest.raises(ValueError, match="min_per_register"):
            VoiceExemplarCollector(min_per_register=0)

    def test_min_greater_than_max_raises(self) -> None:
        """``min_per_register > max_per_register`` raises ``ValueError``."""
        with pytest.raises(ValueError, match=r"min_per_register.*> max_per_register"):
            VoiceExemplarCollector(min_per_register=20, max_per_register=5)


# ---- Classification completeness branches ----


def _partial_fragment(
    *,
    frag_id: str,
    frequency: FrequencyClassification,
    wavelength: WavelengthClassification,
    voice: VoiceClassification,
) -> Fragment:
    """Build a Fragment with caller-controlled classification axes."""
    return Fragment(
        id=frag_id,
        title=frag_id,
        source=FragmentSource(platform=SourcePlatform.JOURNAL),
        frequency=frequency,
        wavelength=wavelength,
        voice=voice,
    )


class TestClassificationCompleteness:
    """Each classification axis must be set for the bonus to apply."""

    def test_each_missing_axis_drops_classification_bonus(
        self,
        collector: VoiceExemplarCollector,
    ) -> None:
        """The complete fragment outranks every single-axis-missing partial."""
        complete = _build_fragment(
            frag_id="frag-zcomplete",
            title="Complete",
            register=VoiceRegister.ANALYTICAL,
            confidence=Confidence.SETTLED,
        )
        analytical_settled = VoiceClassification(
            voice_register=VoiceRegister.ANALYTICAL,
            confidence=Confidence.SETTLED,
        )
        full_wavelength = WavelengthClassification(
            phase=Phase.RISING,
            mode=Mode.EXPRESS,
        )
        full_frequency = FrequencyClassification(primary=Frequency.F5)
        partials = [
            _partial_fragment(
                frag_id="frag-nofreq",
                frequency=FrequencyClassification(),
                wavelength=full_wavelength,
                voice=analytical_settled,
            ),
            _partial_fragment(
                frag_id="frag-nophase",
                frequency=full_frequency,
                wavelength=WavelengthClassification(mode=Mode.EXPRESS),
                voice=analytical_settled,
            ),
            _partial_fragment(
                frag_id="frag-nomode",
                frequency=full_frequency,
                wavelength=WavelengthClassification(phase=Phase.RISING),
                voice=analytical_settled,
            ),
            _partial_fragment(
                frag_id="frag-novoice",
                frequency=full_frequency,
                wavelength=full_wavelength,
                voice=VoiceClassification(confidence=Confidence.SETTLED),
            ),
        ]
        ranked = collector.rank_exemplars([*partials, complete])
        assert ranked[0].id == "frag-zcomplete"


# ---- Loader resilience ----


class TestLoaderResilience:
    """The loader must skip malformed frontmatter without raising."""

    def test_skips_non_fragment_type(
        self,
        vault: Path,
        collector: VoiceExemplarCollector,
    ) -> None:
        """Markdown files whose ``type`` is not ``fragment`` are ignored."""
        path = vault / "01-Fragments" / "Journal" / "thread-like.md"
        path.write_text(
            "---\ntype: thread\nid: thread-x\ntitle: Not a fragment\n---\n",
            encoding="utf-8",
        )
        exemplars = collector.collect_exemplars(vault)
        assert all(value == [] for value in exemplars.values())

    def test_skips_invalid_fragment_metadata(
        self,
        vault: Path,
        collector: VoiceExemplarCollector,
    ) -> None:
        """Fragments whose frontmatter fails validation are skipped."""
        path = vault / "01-Fragments" / "Journal" / "bad.md"
        path.write_text(
            "---\ntype: fragment\nid: frag-bad\n---\n\nbody\n",
            encoding="utf-8",
        )
        exemplars = collector.collect_exemplars(vault)
        assert all(value == [] for value in exemplars.values())


# ---- Save fallback ----


class TestSaveFallback:
    """save_exemplars must handle a missing or moved source file."""

    def test_falls_back_when_cached_source_missing(
        self,
        vault: Path,
        collector: VoiceExemplarCollector,
    ) -> None:
        """If the cached source path is gone, save serialises in-memory."""
        frag = _build_fragment(
            frag_id="frag-gone",
            title="Will be removed",
            register=VoiceRegister.ANALYTICAL,
            confidence=Confidence.SETTLED,
        )
        source = _write_fragment(vault, frag, body_words=400)
        collector.collect_exemplars(vault)
        source.unlink()
        result = collector.save_exemplars({"analytical": [frag]}, vault)
        target = vault / "07-Voice" / "Register-Samples" / "analytical" / "frag-gone.md"
        assert target.exists()
        assert "analytical" in result

    def test_fallback_serialisation_produces_valid_frontmatter(
        self,
        vault: Path,
        collector: VoiceExemplarCollector,
    ) -> None:
        """In-memory fallback must produce a parseable frontmatter document."""
        frag = _build_fragment(
            frag_id="frag-mem2",
            title="In-memory fragment",
            register=VoiceRegister.PLAYFUL,
            confidence=Confidence.SETTLED,
        )
        collector.save_exemplars({"playful": [frag]}, vault)
        target = vault / "07-Voice" / "Register-Samples" / "playful" / "frag-mem2.md"
        post = frontmatter.load(str(target))
        assert post["type"] == "fragment"
        assert post["id"] == "frag-mem2"
        assert post["title"] == "In-memory fragment"


# ---- Tie-breaking ----


class TestTieBreaking:
    """Deterministic ordering when fragments have equal scores."""

    def test_equal_score_ordered_by_ascending_id(
        self,
        vault: Path,
    ) -> None:
        """Fragments with identical scores are ordered by ascending ID."""
        collector = VoiceExemplarCollector(max_per_register=10)
        frag_z = _build_fragment(
            frag_id="frag-zzz",
            title="Z fragment",
            register=VoiceRegister.ANALYTICAL,
            confidence=Confidence.SETTLED,
        )
        frag_a = _build_fragment(
            frag_id="frag-aaa",
            title="A fragment",
            register=VoiceRegister.ANALYTICAL,
            confidence=Confidence.SETTLED,
        )
        _write_fragment(vault, frag_z, body_words=400)
        _write_fragment(vault, frag_a, body_words=400)
        collector.collect_exemplars(vault)
        ranked = collector.rank_exemplars([frag_z, frag_a])
        assert [f.id for f in ranked] == ["frag-aaa", "frag-zzz"]


# ---- YAML error resilience ----


class TestYamlErrorResilience:
    """Malformed YAML must not abort the vault scan."""

    def test_skips_broken_yaml_frontmatter(
        self,
        vault: Path,
        collector: VoiceExemplarCollector,
    ) -> None:
        """Files with syntactically invalid YAML are silently skipped."""
        broken = vault / "01-Fragments" / "Journal" / "bad-yaml.md"
        broken.write_text(
            "---\nkey: [\n---\n\nbody\n",
            encoding="utf-8",
        )
        good = _build_fragment(
            frag_id="frag-yaml-ok",
            title="Good YAML",
            register=VoiceRegister.RAW,
            confidence=Confidence.SETTLED,
        )
        _write_fragment(vault, good, body_words=400)
        exemplars = collector.collect_exemplars(vault)
        assert [f.id for f in exemplars["raw"]] == ["frag-yaml-ok"]


# ---- Warning suppression for empty registers ----


class TestWarningBehavior:
    """Warning logic for registers below minimum threshold."""

    def test_no_warnings_for_empty_registers(
        self,
        vault: Path,
        collector: VoiceExemplarCollector,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Registers with zero exemplars must not emit warnings."""
        with caplog.at_level(logging.WARNING, logger="creek.generate.voice"):
            collector.collect_exemplars(vault)
        assert caplog.records == []

    def test_warns_when_register_has_some_but_below_minimum(
        self,
        vault: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Registers with 1..min-1 exemplars emit a warning."""
        collector = VoiceExemplarCollector(min_per_register=3)
        for idx in range(2):
            frag = _build_fragment(
                frag_id=f"frag-warn-{idx}",
                title=f"Warn frag {idx}",
                register=VoiceRegister.PLAYFUL,
                confidence=Confidence.SETTLED,
            )
            _write_fragment(vault, frag, body_words=400)
        with caplog.at_level(logging.WARNING, logger="creek.generate.voice"):
            collector.collect_exemplars(vault)
        playful_warnings = [r for r in caplog.records if "playful" in r.message]
        assert len(playful_warnings) == 1


# ---- Filename safety (Issue #879) ----


class TestPersistFilenameSafety:
    """``Fragment.id`` becomes a filename, so it is untrusted input.

    ``_persist_fragment`` composes ``register_dir / f"{fragment.id}.md"``.
    Nothing validates ``Fragment.id`` — the model declares a bare ``id:
    str`` — so an id carrying path separators steers the write out of the
    register folder entirely, and one equal to the summary's stem aims it
    at a file the collector is about to write itself.
    """

    def test_traversing_id_writes_nothing_outside_the_register_folder(
        self,
        vault: Path,
        collector: VoiceExemplarCollector,
    ) -> None:
        """An id containing ``..`` must not place a file outside its register."""
        frag = _build_fragment(
            frag_id="../../escape",
            title="Escaping fragment",
            register=VoiceRegister.ANALYTICAL,
            confidence=Confidence.SETTLED,
        )

        collector.save_exemplars({"analytical": [frag]}, vault)

        assert not (vault / "07-Voice" / "escape.md").exists()
        assert not (vault / "escape.md").exists()
        register_dir = vault / "07-Voice" / "Register-Samples" / "analytical"
        assert sorted(p.name for p in register_dir.glob("*.md")) == ["_Summary.md"]

    def test_summary_named_id_leaves_the_summary_note_intact(
        self,
        vault: Path,
        collector: VoiceExemplarCollector,
    ) -> None:
        """A fragment id of ``_Summary`` must not become the summary note.

        NOTE (regression pin, not a RED test): at the time of writing this
        passes, because ``save_exemplars`` persists fragments *before*
        ``_write_summary`` runs, so the summary overwrites the colliding
        copy and the final file is correct by accident of ordering. The
        invariant is worth stating anyway — it is the thing a filename
        guard, a re-ordering, or the #879 prune pass must not break — but
        it is honest about what it can and cannot catch: a guard confined
        to ``_persist_fragment`` produces no observable change here at all.
        """
        frag = _build_fragment(
            frag_id="_Summary",
            title="Colliding sample",
            register=VoiceRegister.ANALYTICAL,
            confidence=Confidence.CONVICTION,
        )
        _write_fragment(vault, frag, body_words=400)

        buckets = collector.collect_exemplars(vault)
        collector.save_exemplars(buckets, vault)

        register_dir = vault / "07-Voice" / "Register-Samples" / "analytical"
        post = frontmatter.load(str(register_dir / "_Summary.md"))
        assert post["type"] == "voice-register-summary"
        assert post["voice_register"] == "analytical"

    def test_rejects_an_id_whose_filename_exceeds_the_name_limit(self) -> None:
        """An id too long to be a filename is rejected before the copy.

        The guard bounds an id's *content* but not its *length*, so a
        300-character id sails through and reaches ``shutil.copy2``,
        which raises ``OSError``/``ENAMETOOLONG`` and takes the whole
        command down with it. ``NAME_MAX`` is 255 **bytes** on every
        filesystem creek runs on (APFS, ext4, XFS), and the name that has
        to fit is ``f"{id}.md"`` — three bytes more than the id itself.

        The accepted case is asserted alongside the rejected one and sits
        exactly on the boundary: a guard that simply rejects long ids
        somewhere short of the real limit would silently drop exemplars
        the filesystem would have accepted.
        """
        assert _is_safe_sample_stem("a" * 252) is True
        assert _is_safe_sample_stem("a" * 253) is False
        assert _is_safe_sample_stem("a" * 300) is False

    def test_measures_the_name_limit_in_bytes_not_characters(self) -> None:
        """The length bound is a byte bound, because ``NAME_MAX`` is.

        U+00E9 (LATIN SMALL LETTER E WITH ACUTE) encodes to two UTF-8
        bytes, so 127 of them plus ``.md`` is 257 bytes in a name of only
        130 characters. A ``len(filename) > 255`` check reads that as
        comfortably short and lets it through — straight back into the
        ``OSError`` the guard exists to prevent. Non-ASCII ids are not
        hypothetical: ids are carried over verbatim from exports.

        The 126-character case is the same boundary from below: 255
        bytes exactly, which the filesystem accepts and so must this.
        """
        # Built from its codepoint rather than typed literally: the source
        # stays ASCII, and the character under test cannot be mangled by an
        # editor, a diff tool, or a normalising paste.
        two_byte_char = chr(0xE9)  # LATIN SMALL LETTER E WITH ACUTE
        # Stated, not assumed: the whole point of the test is the 2:1 ratio.
        assert len(two_byte_char.encode("utf-8")) == 2
        assert _is_safe_sample_stem(two_byte_char * 126) is True
        assert _is_safe_sample_stem(two_byte_char * 127) is False

    def test_rejects_the_summary_filename_as_a_sample_stem(self) -> None:
        """An id of ``_Summary`` must not be usable as a sample stem.

        ``_persist_fragment`` would write the fragment's verbatim body to
        ``_Summary.md``; only the fact that ``_write_summary`` overwrites
        it moments later makes the end state correct. Kill the process in
        between — SIGKILL, OOM, a full disk — and ``_Summary.md`` holds a
        fragment body instead, which the prune then exempts **by name**
        and so never cleans up. Correct-by-ordering is not correct; the
        collision belongs to the guard.

        ``test_summary_named_id_leaves_the_summary_note_intact`` above
        documents itself as a regression pin for the end state and must
        keep passing once this guard lands: the summary is still written,
        it is just no longer racing a body copy to the same path.
        """
        assert _is_safe_sample_stem("_Summary") is False


# ---- generate_register_samples (Issue #879) ----


class TestGenerateRegisterSamples:
    """The module-level entry point the CLI report surface calls.

    Mirrors ``creek.generate.lexicon.generate_lexicon``: build **one**
    collector, ``collect_exemplars`` on it, then ``save_exemplars`` on
    that same instance. The single-instance part is load-bearing rather
    than stylistic — ``_persist_fragment`` copies the source file only
    when ``self._records[fragment.id]`` is populated, and only
    ``collect_exemplars`` populates it.
    """

    def test_returns_the_register_to_summary_path_mapping(
        self,
        vault: Path,
    ) -> None:
        """Every non-empty register maps to its written summary note."""
        from creek.generate.voice import generate_register_samples

        frag = _build_fragment(
            frag_id="frag-grs-1",
            title="Analytical sample",
            register=VoiceRegister.ANALYTICAL,
            confidence=Confidence.CONVICTION,
        )
        _write_fragment(vault, frag, body_words=400)

        result = generate_register_samples(vault)

        register_dir = vault / "07-Voice" / "Register-Samples" / "analytical"
        assert set(result) == {"analytical"}
        assert result["analytical"] == register_dir / "_Summary.md"
        assert result["analytical"].is_file()
        assert (register_dir / "frag-grs-1.md").is_file()

    def test_copies_the_source_file_verbatim(self, vault: Path) -> None:
        """The persisted sample is the source file, byte for byte.

        The single assertion that separates a correct implementation from
        one that builds a second collector (or skips ``collect_exemplars``
        on the instance it saves with): the fallback path serialises
        ``frontmatter.Post(content="", **fragment.model_dump())``, which
        is a *valid* fragment note with an **empty body**, and a voice
        corpus of empty bodies is silently useless.
        """
        from creek.generate.voice import generate_register_samples

        frag = _build_fragment(
            frag_id="frag-grs-body",
            title="Body carrier",
            register=VoiceRegister.PLAYFUL,
            confidence=Confidence.CONVICTION,
        )
        source = _write_fragment(vault, frag, body_words=400)

        generate_register_samples(vault)

        copy = vault / "07-Voice" / "Register-Samples" / "playful" / "frag-grs-body.md"
        assert copy.read_bytes() == source.read_bytes()
        assert frontmatter.load(str(copy)).content.strip() != ""

    def test_empty_vault_returns_an_empty_mapping_and_writes_nothing(
        self,
        vault: Path,
    ) -> None:
        """No qualifying exemplars means no mapping and no folders created."""
        from creek.generate.voice import generate_register_samples

        assert generate_register_samples(vault) == {}
        assert list((vault / "07-Voice" / "Register-Samples").iterdir()) == []

    def test_override_excludes_above_ceiling_fragments(self, vault: Path) -> None:
        """The declared ceiling narrows the corpus the samples are drawn from.

        Additive to — never a replacement for — the ``allow_intimate``
        consent gate: this is the caller's ceiling, applied to the raw
        frontmatter by ``within_ceiling``.
        """
        from creek.classify.privacy_filter import PrivacyTierOverride
        from creek.generate.voice import generate_register_samples

        _write_fragment(
            vault,
            _build_fragment(
                frag_id="frag-grs-open",
                title="Open sample",
                register=VoiceRegister.RAW,
                confidence=Confidence.CONVICTION,
                privacy=PrivacyTier.OPEN,
            ),
            body_words=400,
        )
        _write_fragment(
            vault,
            _build_fragment(
                frag_id="frag-grs-personal",
                title="Personal sample",
                register=VoiceRegister.RAW,
                confidence=Confidence.CONVICTION,
                privacy=PrivacyTier.PERSONAL,
            ),
            body_words=400,
        )

        generate_register_samples(vault, override=PrivacyTierOverride.OPEN)

        register_dir = vault / "07-Voice" / "Register-Samples" / "raw"
        copied = sorted(
            p.name for p in register_dir.glob("*.md") if p.name != "_Summary.md"
        )
        assert copied == ["frag-grs-open.md"]

    def test_is_exported_from_the_module(self) -> None:
        """The entry point is part of the module's public surface."""
        from creek.generate import voice as voice_module

        assert "generate_register_samples" in voice_module.__all__


# ---- Manifest corruption is fail-safe (Issue #879) ----


_MANIFEST_REGISTER_DIR = ("07-Voice", "Register-Samples", "analytical")
"""Vault-relative samples folder the manifest-corruption tests operate in."""


def _seed_pruneable_register(vault_path: Path) -> tuple[Path, Path]:
    """Leave a register in the exact state its stale copy is prune-eligible in.

    Runs one full save of two analytical exemplars — so both copies are on
    disk **and** both are fingerprinted in ``_Summary.md``'s manifest —
    then deletes one fragment's source file, dropping it out of the
    corpus. A second ``generate_register_samples`` therefore *would*
    delete ``frag-manifest-stale.md``, which is what makes "the copy
    survived" an assertion about the manifest read rather than about a
    prune that was never going to fire in this fixture anyway.

    Args:
        vault_path: Vault root, already scaffolded by the ``vault``
            fixture.

    Returns:
        The ``(register_dir, stale_copy)`` pair the callers assert on.
    """
    from creek.generate.voice import generate_register_samples

    for frag_id in ("frag-manifest-keep", "frag-manifest-stale"):
        _write_fragment(
            vault_path,
            _build_fragment(
                frag_id=frag_id,
                title=f"Manifest fixture {frag_id}",
                register=VoiceRegister.ANALYTICAL,
                confidence=Confidence.CONVICTION,
            ),
            body_words=400,
        )

    generate_register_samples(vault_path)

    register_dir = vault_path.joinpath(*_MANIFEST_REGISTER_DIR)
    stale_copy = register_dir / "frag-manifest-stale.md"
    assert stale_copy.is_file(), "the seeding run wrote no copy to go stale"
    (vault_path / "01-Fragments" / "Journal" / "frag-manifest-stale.md").unlink()
    return register_dir, stale_copy


def _rewrite_manifest(register_dir: Path, value: object) -> None:
    """Replace the summary's recorded manifest with *value*, keeping the rest.

    Rewriting through ``frontmatter`` rather than by hand keeps every
    other key the prune and the operator rely on intact, so the only
    variable in the test is the shape of the manifest itself.

    Args:
        register_dir: The register's samples folder.
        value: The hand-edited value to store under ``exemplar_digests``.
    """
    summary_path = register_dir / "_Summary.md"
    post = frontmatter.load(str(summary_path))
    post.metadata["exemplar_digests"] = value
    summary_path.write_text(frontmatter.dumps(post), encoding="utf-8")


class TestManifestCorruptionIsFailSafe:
    """A manifest the prune cannot trust must disarm it, not crash or over-delete.

    ``_read_sample_manifest`` answers the only question that entitles the
    collector to ``unlink`` a file in a folder the operator also keeps
    notes in: "did I write this?". Its two defensive branches — an
    unreadable ``_Summary.md`` and a recorded value that is not a list —
    both answer "I cannot tell", and the contract is that "I cannot tell"
    means *delete nothing*. The cost is a stale copy; the cost of the
    other direction is an operator's file.

    Both branches were reachable only through fixtures nothing exercised,
    which leaves two regressions invisible: a refactor that lets the read
    raise takes down the whole command — and ``creek fill`` runs it
    unattended — while one that drops the type check turns a hand-edited
    manifest into a deletion list.

    Every test here goes through the real save path, so what is pinned is
    the prune's observable safety rather than a private helper's return
    value.
    """

    def test_prunes_the_stale_copy_when_the_manifest_is_intact(
        self,
        vault: Path,
    ) -> None:
        """Baseline: an uncorrupted manifest DOES delete the stale copy.

        Not a duplicate of the CLI-level prune tests but the control that
        makes the three below non-vacuous: it establishes that in this
        exact fixture the prune is armed and reaches this file. Without
        it, "the copy survived" would be equally true of an implementation
        that never prunes anything at all.
        """
        from creek.generate.voice import generate_register_samples

        register_dir, stale_copy = _seed_pruneable_register(vault)

        generate_register_samples(vault)

        assert not stale_copy.exists()
        assert (register_dir / "frag-manifest-keep.md").is_file()

    def test_unreadable_summary_leaves_the_stale_copy_alone(
        self,
        vault: Path,
    ) -> None:
        """Malformed summary YAML degrades to a no-op prune, not a traceback.

        ``_Summary.md`` is an ordinary note inside the vault: an operator
        edit, a half-written file from an interrupted run, or a merge
        conflict marker all land here as YAML that ``frontmatter.load``
        refuses. Uncaught, that exception escapes ``save_exemplars`` and
        kills ``creek report --type voice`` and the unattended ``creek
        fill`` step with it — over a file whose only job is to say which
        copies are deletable.

        The rewritten summary is the control: it proves the run carried on
        past the unreadable manifest and completed the save, rather than
        the copy surviving because nothing ran.
        """
        from creek.generate.voice import generate_register_samples

        register_dir, stale_copy = _seed_pruneable_register(vault)
        before = stale_copy.read_bytes()
        (register_dir / "_Summary.md").write_text(
            "---\nexemplar_digests: [\n---\n\nbroken\n",
            encoding="utf-8",
        )

        generate_register_samples(vault)

        assert stale_copy.read_bytes() == before
        rewritten = frontmatter.load(str(register_dir / "_Summary.md"))
        assert rewritten["exemplar_count"] == 1
        assert (register_dir / "frag-manifest-keep.md").is_file()

    def test_a_scalar_manifest_leaves_the_stale_copy_alone(
        self,
        vault: Path,
    ) -> None:
        """A manifest that is a bare string prunes nothing.

        The shape a hand-edit produces when someone strips the YAML list
        dashes, or a note is authored by something that never read the
        schema. Nothing about it is a record of authorship, so the prune
        must treat it as no record at all — the file the collector cannot
        prove it wrote is the file it must not delete.
        """
        from creek.generate.voice import generate_register_samples

        register_dir, stale_copy = _seed_pruneable_register(vault)
        before = stale_copy.read_bytes()
        _rewrite_manifest(register_dir, "oops")

        generate_register_samples(vault)

        assert stale_copy.read_bytes() == before
        rewritten = frontmatter.load(str(register_dir / "_Summary.md"))
        assert rewritten["exemplar_count"] == 1
        assert (register_dir / "frag-manifest-keep.md").is_file()

    def test_a_mapping_manifest_leaves_the_stale_copy_alone(
        self,
        vault: Path,
    ) -> None:
        """A manifest that is a mapping prunes nothing — the guard's live case.

        The scalar case above cannot on its own prove the ``isinstance``
        check earns its keep: iterating a string yields single characters,
        which match no digest, so deleting the check would leave that test
        green. A mapping is the shape where the check is load-bearing —
        iterating it yields exactly the digest keys, so a prune that
        skipped the type test would read this as a perfectly good manifest
        and delete the copy. Keeping the real digests as the keys is the
        whole point: a placeholder would prove nothing.
        """
        from creek.generate.voice import generate_register_samples

        register_dir, stale_copy = _seed_pruneable_register(vault)
        before = stale_copy.read_bytes()
        seeded = frontmatter.load(str(register_dir / "_Summary.md"))
        recorded = seeded["exemplar_digests"]
        assert isinstance(recorded, list), "the seeding run recorded no manifest"
        _rewrite_manifest(register_dir, dict.fromkeys(recorded, True))

        generate_register_samples(vault)

        assert stale_copy.read_bytes() == before
        assert (register_dir / "frag-manifest-keep.md").is_file()
