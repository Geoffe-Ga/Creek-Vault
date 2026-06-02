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
    _is_other_authors_path,
)
from creek.models import (
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
    voice_weight: float = 1.0,
) -> Fragment:
    """Build a Fragment with the desired voice / privacy / classification state."""
    if fully_classified:
        frequency = FrequencyClassification(primary=Frequency.F5)
        wavelength = WavelengthClassification(
            phase=Phase.RISING,
            mode=Mode.EXPRESS,
        )
    else:
        frequency = FrequencyClassification()
        wavelength = WavelengthClassification()
    return Fragment(
        id=frag_id,
        title=title,
        source=FragmentSource(platform=SourcePlatform.JOURNAL),
        created=datetime(2026, 1, 15, 12, 0, 0),
        ingested=datetime(2026, 1, 15, 12, 0, 0),
        frequency=frequency,
        wavelength=wavelength,
        voice=VoiceClassification(voice_register=register, confidence=confidence),
        privacy_tier=privacy,
        voice_weight=voice_weight,
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

    Two additive gates protect voice fidelity, mirroring the privacy
    fail-closed rule:

    1. ``voice_weight > 0`` — a fragment with ``voice_weight <= 0`` (the
       ``ai-as-user`` analogue with ``voice_weight=0.0``) is ineligible.
    2. ``11-Other-Authors/`` path exclusion — any fragment whose source
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
