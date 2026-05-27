"""Tests for creek.generate.skills — Voice Skill Tree generation.

Covers the ``SkillTreeGenerator`` class implementing Section 11.4 of
the Creek Ontology: a tree of ``SKILL.md`` files for frequencies,
phases, modes, registers, threads, eddies, and meta-skills.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

import frontmatter
import pytest

from creek.generate.skills import (
    DEFAULT_MAX_EXEMPLARS,
    DEFAULT_MIN_EDDY_FRAGMENTS,
    DEFAULT_MIN_THREAD_FRAGMENTS,
    FREQUENCY_KEYS,
    FREQUENCY_MEDICINE_VS_TOXIC,
    FREQUENCY_VOICE_TEXTURES,
    MODE_ORIENTATION_KEYS,
    PHASE_KEYS,
    REGISTER_KEYS,
    SkillExemplar,
    SkillTreeGenerator,
    VaultSnapshot,
    _build_exemplar,
    _extract_passage,
    _load_fragment,
    _load_typed_model,
    _load_vault_snapshot,
    _mode_orientation_key,
    _slugify,
    _unique_slug,
)
from creek.models import (
    Confidence,
    Eddy,
    Fragment,
    FragmentSource,
    Frequency,
    FrequencyClassification,
    Mode,
    Orientation,
    Phase,
    PrivacyTier,
    SourcePlatform,
    Thread,
    ThreadStatus,
    VoiceClassification,
    VoiceRegister,
    WavelengthClassification,
)

if TYPE_CHECKING:
    from pathlib import Path


# ---- Fixtures ----


@pytest.fixture()
def vault(tmp_path: Path) -> Path:
    """Create a vault with the folders the generator touches."""
    for folder in (
        "01-Fragments/Journal",
        "02-Threads/Active",
        "03-Eddies",
    ):
        (tmp_path / folder).mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture()
def output_dir(tmp_path: Path) -> Path:
    """Create an output directory for skill files."""
    out = tmp_path / "creek-skills"
    out.mkdir()
    return out


@pytest.fixture()
def generator() -> SkillTreeGenerator:
    """Create a default SkillTreeGenerator."""
    return SkillTreeGenerator()


def _build_fragment(
    *,
    frag_id: str,
    title: str,
    frequency: Frequency = Frequency.F3,
    phase: Phase = Phase.RISING,
    mode: Mode = Mode.EXPRESS,
    orientation: Orientation = Orientation.DO,
    register: VoiceRegister | None = VoiceRegister.CONFESSIONAL,
    confidence: Confidence | None = Confidence.SETTLED,
    privacy: PrivacyTier = PrivacyTier.PERSONAL,
    proxy_eligible: bool = True,
) -> Fragment:
    """Create a Fragment with the specified classification.

    ``proxy_eligible=False`` is honoured by routing through the
    privacy-tier signal: voice-proxy ineligibility is now a derived
    property (BUG-009) of ``privacy_tier`` and ``source.author``, so
    forcing ineligibility is equivalent to escalating to ``INTIMATE``.

    Mixing ``proxy_eligible=False`` with an explicit non-INTIMATE
    ``privacy`` is rejected up-front rather than silently overriding
    the caller's choice — the helper would otherwise hide a real
    misconfiguration in a future test.
    """
    if not proxy_eligible:
        if privacy not in (PrivacyTier.PERSONAL, PrivacyTier.INTIMATE):
            msg = (
                "_build_fragment(proxy_eligible=False, privacy=...) only accepts "
                "PERSONAL (default) or INTIMATE; ineligibility is now derived "
                "from privacy_tier so non-INTIMATE tiers cannot be forced "
                "ineligible without changing source.author."
            )
            raise ValueError(msg)
        privacy = PrivacyTier.INTIMATE
    return Fragment(
        id=frag_id,
        title=title,
        source=FragmentSource(platform=SourcePlatform.JOURNAL),
        created=datetime(2026, 1, 15, 12, 0, 0),
        ingested=datetime(2026, 1, 15, 12, 0, 0),
        frequency=FrequencyClassification(primary=frequency),
        wavelength=WavelengthClassification(
            phase=phase,
            mode=mode,
            orientation=orientation,
        ),
        voice=VoiceClassification(voice_register=register, confidence=confidence),
        privacy_tier=privacy,
    )


def _write_fragment(vault_path: Path, fragment: Fragment, body: str) -> Path:
    """Persist *fragment* under ``01-Fragments/Journal``."""
    data = fragment.model_dump(mode="json")
    post = frontmatter.Post(content=body, **data)
    target = vault_path / "01-Fragments" / "Journal" / f"{fragment.id}.md"
    target.write_text(frontmatter.dumps(post), encoding="utf-8")
    return target


def _write_thread(vault_path: Path, thread: Thread) -> Path:
    """Persist *thread* under ``02-Threads/Active/``."""
    data = thread.model_dump(mode="json")
    post = frontmatter.Post(content=thread.description, **data)
    target = vault_path / "02-Threads" / "Active" / f"{thread.id}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(frontmatter.dumps(post), encoding="utf-8")
    return target


def _write_eddy(vault_path: Path, eddy: Eddy) -> Path:
    """Persist *eddy* under ``03-Eddies/``."""
    data = eddy.model_dump(mode="json")
    post = frontmatter.Post(content=eddy.description, **data)
    target = vault_path / "03-Eddies" / f"{eddy.id}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(frontmatter.dumps(post), encoding="utf-8")
    return target


def _make_passage(sentence_count: int) -> str:
    """Produce a multi-sentence passage with enough words to qualify."""
    sentence = (
        "This is a complete sentence with enough words to register "
        "as substantive voice data in the passage extractor."
    )
    return " ".join([sentence] * sentence_count)


# ---- Module surface ----


class TestModuleSurface:
    """Canonical ontology keys must match Section 11.4 of the specification."""

    def test_frequency_keys_covers_ten_frequencies(self) -> None:
        """FREQUENCY_KEYS enumerates F1..F10 only."""
        assert tuple(f"F{i}" for i in range(1, 11)) == FREQUENCY_KEYS

    def test_phase_keys_covers_six_phases(self) -> None:
        """PHASE_KEYS enumerates the six ontology phases."""
        assert set(PHASE_KEYS) == {
            "rising",
            "peaking",
            "withdrawal",
            "diminishing",
            "bottoming_out",
            "restoration",
        }

    def test_register_keys_covers_seven_registers(self) -> None:
        """REGISTER_KEYS enumerates the seven voice registers."""
        assert set(REGISTER_KEYS) == {
            "confessional",
            "analytical",
            "playful",
            "prophetic",
            "instructional",
            "raw",
            "conversational",
        }

    def test_mode_orientation_keys_has_nine_combinations(self) -> None:
        """MODE_ORIENTATION_KEYS has exactly nine pairs per the ontology."""
        assert len(MODE_ORIENTATION_KEYS) == 9
        assert ("absorb", "do_feel") in MODE_ORIENTATION_KEYS

    def test_default_thresholds_follow_spec(self) -> None:
        """Default thresholds come straight from the issue specification."""
        assert DEFAULT_MIN_THREAD_FRAGMENTS == 10
        assert DEFAULT_MIN_EDDY_FRAGMENTS == 15
        assert DEFAULT_MAX_EXEMPLARS == 5

    def test_frequency_voice_textures_cover_all_frequencies(self) -> None:
        """Every frequency key must have a voice texture entry."""
        assert set(FREQUENCY_VOICE_TEXTURES) == set(FREQUENCY_KEYS)

    def test_frequency_medicine_toxic_entries_are_two_tuples(self) -> None:
        """Each frequency must have both medicine and toxic descriptors."""
        for key in FREQUENCY_KEYS:
            medicine, toxic = FREQUENCY_MEDICINE_VS_TOXIC[key]
            assert medicine
            assert toxic


# ---- Constructor validation ----


class TestConstructor:
    """``SkillTreeGenerator`` rejects bad configuration."""

    def test_rejects_negative_thread_threshold(self) -> None:
        """Negative thread thresholds should not be accepted."""
        with pytest.raises(ValueError, match="min_thread_fragments"):
            SkillTreeGenerator(min_thread_fragments=-1)

    def test_rejects_negative_eddy_threshold(self) -> None:
        """Negative eddy thresholds should not be accepted."""
        with pytest.raises(ValueError, match="min_eddy_fragments"):
            SkillTreeGenerator(min_eddy_fragments=-1)

    def test_rejects_zero_max_exemplars(self) -> None:
        """max_exemplars must be positive."""
        with pytest.raises(ValueError, match="max_exemplars"):
            SkillTreeGenerator(max_exemplars=0)


# ---- generate_frequency_skills ----


class TestFrequencySkills:
    """Frequency skills must cover all ten frequencies."""

    def test_generates_ten_frequency_skills(
        self,
        vault: Path,
        output_dir: Path,
        generator: SkillTreeGenerator,
    ) -> None:
        """Exactly ten frequency SKILL files should be created."""
        paths = generator.generate_frequency_skills(vault, output_dir)
        assert len(paths) == 10
        names = {p.stem.replace(".SKILL", "") for p in paths}
        assert names == set(FREQUENCY_KEYS)

    def test_frequency_skill_has_required_sections(
        self,
        vault: Path,
        output_dir: Path,
        generator: SkillTreeGenerator,
    ) -> None:
        """Each skill file must follow the Claude Code skill format."""
        generator.generate_frequency_skills(vault, output_dir)
        post = frontmatter.load(
            str(output_dir / "frequencies" / "F3.SKILL.md"),
        )
        assert post.metadata["type"] == "skill"
        assert post.metadata["category"] == "frequency"
        assert post.metadata["key"] == "F3"
        body = post.content
        assert "# Activation" in body
        assert "## Description" in body
        assert "## Exemplar Passages" in body
        assert "## Writing Instructions" in body
        assert "## Anti-Patterns" in body
        assert "## Combining With Other Skills" in body

    def test_frequency_skill_includes_combination_guidance(
        self,
        vault: Path,
        output_dir: Path,
        generator: SkillTreeGenerator,
    ) -> None:
        """Combination guidance must mention phase and register layering."""
        generator.generate_frequency_skills(vault, output_dir)
        body = (output_dir / "frequencies" / "F3.SKILL.md").read_text()
        assert "Phase skill" in body
        assert "Register skill" in body

    def test_frequency_skill_includes_exemplars_when_available(
        self,
        vault: Path,
        output_dir: Path,
        generator: SkillTreeGenerator,
    ) -> None:
        """Fragments classified to a frequency should appear as exemplars."""
        fragment = _build_fragment(
            frag_id="frag-freq1",
            title="Standing in my heat",
            frequency=Frequency.F3,
        )
        _write_fragment(vault, fragment, _make_passage(3))
        generator.generate_frequency_skills(vault, output_dir)
        body = (output_dir / "frequencies" / "F3.SKILL.md").read_text()
        assert "Standing in my heat" in body
        assert "frag-freq1" in body

    def test_frequency_skill_falls_back_when_no_exemplars(
        self,
        vault: Path,
        output_dir: Path,
        generator: SkillTreeGenerator,
    ) -> None:
        """An empty vault still produces a well-formed placeholder section."""
        generator.generate_frequency_skills(vault, output_dir)
        body = (output_dir / "frequencies" / "F1.SKILL.md").read_text()
        assert "No qualifying exemplars" in body


# ---- generate_phase_skills ----


class TestPhaseSkills:
    """Phase skills must cover the six wavelength phases."""

    def test_generates_six_phase_skills(
        self,
        vault: Path,
        output_dir: Path,
        generator: SkillTreeGenerator,
    ) -> None:
        """Exactly six phase SKILL files should be created."""
        paths = generator.generate_phase_skills(vault, output_dir)
        assert len(paths) == 6
        names = {p.stem.replace(".SKILL", "") for p in paths}
        assert names == set(PHASE_KEYS)

    def test_phase_skill_has_required_sections(
        self,
        vault: Path,
        output_dir: Path,
        generator: SkillTreeGenerator,
    ) -> None:
        """Phase skills must follow the Claude Code skill format."""
        generator.generate_phase_skills(vault, output_dir)
        post = frontmatter.load(
            str(output_dir / "phases" / "withdrawal.SKILL.md"),
        )
        body = post.content
        assert post.metadata["category"] == "phase"
        assert "# Activation" in body
        assert "## Description" in body
        assert "## Exemplar Passages" in body
        assert "## Writing Instructions" in body
        assert "## Anti-Patterns" in body
        assert "## Combining With Other Skills" in body

    def test_phase_skill_harvests_phase_exemplars(
        self,
        vault: Path,
        output_dir: Path,
        generator: SkillTreeGenerator,
    ) -> None:
        """Fragments in a phase should show up in that phase's skill file."""
        frag = _build_fragment(
            frag_id="frag-phase1",
            title="Letting the fire cool",
            phase=Phase.WITHDRAWAL,
        )
        _write_fragment(vault, frag, _make_passage(3))
        generator.generate_phase_skills(vault, output_dir)
        body = (output_dir / "phases" / "withdrawal.SKILL.md").read_text()
        assert "Letting the fire cool" in body


# ---- generate_mode_skills ----


class TestModeSkills:
    """Mode/Orientation skills must cover nine combinations."""

    def test_generates_nine_mode_skills(
        self,
        vault: Path,
        output_dir: Path,
        generator: SkillTreeGenerator,
    ) -> None:
        """Exactly nine mode SKILL files should be created."""
        paths = generator.generate_mode_skills(vault, output_dir)
        assert len(paths) == 9

    def test_absorb_skill_uses_do_feel_key(
        self,
        vault: Path,
        output_dir: Path,
        generator: SkillTreeGenerator,
    ) -> None:
        """The Absorb mode collapses orientations into a single skill file."""
        generator.generate_mode_skills(vault, output_dir)
        target = output_dir / "modes" / "absorb-do-feel.SKILL.md"
        assert target.exists()

    def test_express_do_skill_has_expected_title(
        self,
        vault: Path,
        output_dir: Path,
        generator: SkillTreeGenerator,
    ) -> None:
        """Mode skills should have human-readable titles."""
        generator.generate_mode_skills(vault, output_dir)
        post = frontmatter.load(
            str(output_dir / "modes" / "express-do.SKILL.md"),
        )
        assert post.metadata["title"] == "Express-Do"

    def test_absorb_mode_groups_all_orientations(
        self,
        vault: Path,
        output_dir: Path,
        generator: SkillTreeGenerator,
    ) -> None:
        """Absorb exemplars with DO orientation land in the absorb/do_feel file."""
        frag = _build_fragment(
            frag_id="frag-abs1",
            title="Receiving the frequency",
            mode=Mode.ABSORB,
            orientation=Orientation.DO,
        )
        _write_fragment(vault, frag, _make_passage(3))
        generator.generate_mode_skills(vault, output_dir)
        body = (output_dir / "modes" / "absorb-do-feel.SKILL.md").read_text()
        assert "Receiving the frequency" in body


# ---- generate_register_skills ----


class TestRegisterSkills:
    """Register skills must cover the seven voice registers."""

    def test_generates_seven_register_skills(
        self,
        vault: Path,
        output_dir: Path,
        generator: SkillTreeGenerator,
    ) -> None:
        """Exactly seven register SKILL files should be created."""
        paths = generator.generate_register_skills(vault, output_dir)
        assert len(paths) == 7

    def test_register_skill_includes_voice_prompt(
        self,
        vault: Path,
        output_dir: Path,
        generator: SkillTreeGenerator,
    ) -> None:
        """Register skills must include the Section 11.2 voice prompt."""
        generator.generate_register_skills(vault, output_dir)
        body = (output_dir / "registers" / "confessional.SKILL.md").read_text()
        assert "Write in a confessional register" in body
        assert "first person" in body

    def test_register_skill_includes_anti_patterns(
        self,
        vault: Path,
        output_dir: Path,
        generator: SkillTreeGenerator,
    ) -> None:
        """Register skills must include register-specific anti-patterns."""
        generator.generate_register_skills(vault, output_dir)
        body = (output_dir / "registers" / "prophetic.SKILL.md").read_text()
        assert "Do not hedge" in body


# ---- generate_thread_skills ----


class TestThreadSkills:
    """Thread skills qualify when ``fragment_count > min_thread_fragments``."""

    def test_qualifying_threads_produce_skills(
        self,
        vault: Path,
        output_dir: Path,
        generator: SkillTreeGenerator,
    ) -> None:
        """Threads with more than the threshold fragments earn a skill."""
        thread = Thread(
            id="thread-major",
            title="The Ongoing Grief",
            status=ThreadStatus.ACTIVE,
            fragment_count=25,
            description="A thread that recurs across seasons.",
        )
        _write_thread(vault, thread)
        paths = generator.generate_thread_skills(vault, output_dir)
        assert len(paths) == 1
        assert paths[0].stem == "the-ongoing-grief.SKILL"

    def test_threads_below_threshold_are_skipped(
        self,
        vault: Path,
        output_dir: Path,
        generator: SkillTreeGenerator,
    ) -> None:
        """Threads at or below ``min_thread_fragments`` do not earn a skill."""
        thread = Thread(
            id="thread-tiny",
            title="Barely a thread",
            status=ThreadStatus.ACTIVE,
            fragment_count=DEFAULT_MIN_THREAD_FRAGMENTS,
            description="Just at the threshold.",
        )
        _write_thread(vault, thread)
        paths = generator.generate_thread_skills(vault, output_dir)
        assert not paths

    def test_thread_slug_collisions_preserve_both_files(
        self,
        vault: Path,
        output_dir: Path,
        generator: SkillTreeGenerator,
    ) -> None:
        """Two threads with titles that slugify identically keep distinct files."""
        _write_thread(
            vault,
            Thread(
                id="thread-aaa",
                title="Foo Bar",
                status=ThreadStatus.ACTIVE,
                fragment_count=20,
                description="First.",
            ),
        )
        _write_thread(
            vault,
            Thread(
                id="thread-bbb",
                title="foo-bar",
                status=ThreadStatus.ACTIVE,
                fragment_count=20,
                description="Second.",
            ),
        )
        paths = generator.generate_thread_skills(vault, output_dir)
        assert len(paths) == 2
        assert len({p.name for p in paths}) == 2


# ---- generate_eddy_skills ----


class TestEddySkills:
    """Eddy skills qualify when ``fragment_count > min_eddy_fragments``."""

    def test_qualifying_eddies_produce_skills(
        self,
        vault: Path,
        output_dir: Path,
        generator: SkillTreeGenerator,
    ) -> None:
        """Eddies with more than the threshold fragments earn a skill."""
        eddy = Eddy(
            id="eddy-major",
            title="Family Of Origin",
            fragment_count=42,
            threads=["thread-abc"],
            description="The gravitational centre of the vault.",
        )
        _write_eddy(vault, eddy)
        paths = generator.generate_eddy_skills(vault, output_dir)
        assert len(paths) == 1
        body = paths[0].read_text()
        assert "Family Of Origin" in body
        assert "thread-abc" in body

    def test_eddies_below_threshold_are_skipped(
        self,
        vault: Path,
        output_dir: Path,
        generator: SkillTreeGenerator,
    ) -> None:
        """Eddies below the threshold do not earn a skill."""
        eddy = Eddy(
            id="eddy-tiny",
            title="Too small to count",
            fragment_count=DEFAULT_MIN_EDDY_FRAGMENTS,
            description="At the threshold.",
        )
        _write_eddy(vault, eddy)
        paths = generator.generate_eddy_skills(vault, output_dir)
        assert not paths

    def test_eddy_slug_collisions_preserve_both_files(
        self,
        vault: Path,
        output_dir: Path,
        generator: SkillTreeGenerator,
    ) -> None:
        """Two eddies with titles that slugify identically keep distinct files."""
        _write_eddy(
            vault,
            Eddy(
                id="eddy-aaa",
                title="The Work",
                fragment_count=25,
                threads=[],
                description="First.",
            ),
        )
        _write_eddy(
            vault,
            Eddy(
                id="eddy-bbb",
                title="The Work!",
                fragment_count=25,
                threads=[],
                description="Second.",
            ),
        )
        paths = generator.generate_eddy_skills(vault, output_dir)
        assert len(paths) == 2
        assert len({p.name for p in paths}) == 2


# ---- generate_meta_skills ----


class TestMetaSkills:
    """Meta skills produce the master voice profile and activation guide."""

    def test_generates_two_meta_skills(
        self,
        output_dir: Path,
        generator: SkillTreeGenerator,
    ) -> None:
        """Exactly two meta SKILL files should be created."""
        paths = generator.generate_meta_skills(output_dir)
        assert len(paths) == 2
        names = {p.stem.replace(".SKILL", "") for p in paths}
        assert names == {"voice-core", "skill-activation-guide"}

    def test_voice_core_skill_layers_guidance(
        self,
        output_dir: Path,
        generator: SkillTreeGenerator,
    ) -> None:
        """The voice-core skill documents the layering order."""
        generator.generate_meta_skills(output_dir)
        body = (output_dir / "meta" / "voice-core.SKILL.md").read_text()
        assert "Frequency" in body
        assert "Phase" in body
        assert "Register" in body

    def test_activation_guide_has_recipe_examples(
        self,
        output_dir: Path,
        generator: SkillTreeGenerator,
    ) -> None:
        """The activation guide lists concrete layering recipes."""
        generator.generate_meta_skills(output_dir)
        body = (output_dir / "meta" / "skill-activation-guide.SKILL.md").read_text()
        assert "F5" in body or "F6" in body
        assert "bottoming_out" in body or "peaking" in body


# ---- generate_all_skills ----


class TestGenerateAll:
    """The end-to-end generator produces the full tree."""

    def test_empty_vault_produces_minimum_thirty_four_skills(
        self,
        vault: Path,
        output_dir: Path,
        generator: SkillTreeGenerator,
    ) -> None:
        """An empty vault still produces 10 + 6 + 9 + 7 + 2 = 34 skill files."""
        paths = generator.generate_all_skills(vault, output_dir)
        assert len(paths) == 34

    def test_populated_vault_includes_thread_and_eddy_skills(
        self,
        vault: Path,
        output_dir: Path,
        generator: SkillTreeGenerator,
    ) -> None:
        """Major threads and eddies add to the base count."""
        _write_thread(
            vault,
            Thread(
                id="thread-xyz",
                title="The Long Thread",
                status=ThreadStatus.ACTIVE,
                fragment_count=20,
                description="Spans many seasons.",
            ),
        )
        _write_eddy(
            vault,
            Eddy(
                id="eddy-xyz",
                title="Parent Eddy",
                fragment_count=20,
                threads=["thread-xyz"],
                description="A core topic cluster.",
            ),
        )
        paths = generator.generate_all_skills(vault, output_dir)
        categories = {p.parent.name for p in paths}
        assert "threads" in categories
        assert "eddies" in categories
        assert len(paths) == 34 + 2


# ---- Optional snapshot parameter ----


class TestOptionalSnapshot:
    """Public per-category methods accept a pre-loaded ``VaultSnapshot``."""

    def test_frequency_skills_accepts_preloaded_snapshot(
        self,
        vault: Path,
        output_dir: Path,
        generator: SkillTreeGenerator,
    ) -> None:
        """Passing a snapshot should skip the vault scan and still render."""
        snapshot = _load_vault_snapshot(vault, allow_intimate=False)
        paths = generator.generate_frequency_skills(
            vault,
            output_dir,
            snapshot=snapshot,
        )
        assert len(paths) == 10

    def test_phase_skills_accepts_preloaded_snapshot(
        self,
        vault: Path,
        output_dir: Path,
        generator: SkillTreeGenerator,
    ) -> None:
        """Phase skills accept a pre-loaded snapshot."""
        snapshot = _load_vault_snapshot(vault, allow_intimate=False)
        paths = generator.generate_phase_skills(
            vault,
            output_dir,
            snapshot=snapshot,
        )
        assert len(paths) == 6

    def test_mode_skills_accepts_preloaded_snapshot(
        self,
        vault: Path,
        output_dir: Path,
        generator: SkillTreeGenerator,
    ) -> None:
        """Mode skills accept a pre-loaded snapshot."""
        snapshot = _load_vault_snapshot(vault, allow_intimate=False)
        paths = generator.generate_mode_skills(
            vault,
            output_dir,
            snapshot=snapshot,
        )
        assert len(paths) == 9

    def test_register_skills_accepts_preloaded_snapshot(
        self,
        vault: Path,
        output_dir: Path,
        generator: SkillTreeGenerator,
    ) -> None:
        """Register skills accept a pre-loaded snapshot."""
        snapshot = _load_vault_snapshot(vault, allow_intimate=False)
        paths = generator.generate_register_skills(
            vault,
            output_dir,
            snapshot=snapshot,
        )
        assert len(paths) == 7

    def test_thread_skills_accepts_preloaded_snapshot(
        self,
        vault: Path,
        output_dir: Path,
        generator: SkillTreeGenerator,
    ) -> None:
        """Thread skills accept a pre-loaded snapshot."""
        _write_thread(
            vault,
            Thread(
                id="thread-snap",
                title="Snapshot Thread",
                status=ThreadStatus.ACTIVE,
                fragment_count=20,
                description="Many fragments.",
            ),
        )
        snapshot = _load_vault_snapshot(vault, allow_intimate=False)
        paths = generator.generate_thread_skills(
            vault,
            output_dir,
            snapshot=snapshot,
        )
        assert len(paths) == 1

    def test_eddy_skills_accepts_preloaded_snapshot(
        self,
        vault: Path,
        output_dir: Path,
        generator: SkillTreeGenerator,
    ) -> None:
        """Eddy skills accept a pre-loaded snapshot."""
        _write_eddy(
            vault,
            Eddy(
                id="eddy-snap",
                title="Snapshot Eddy",
                fragment_count=20,
                threads=[],
                description="Many fragments.",
            ),
        )
        snapshot = _load_vault_snapshot(vault, allow_intimate=False)
        paths = generator.generate_eddy_skills(
            vault,
            output_dir,
            snapshot=snapshot,
        )
        assert len(paths) == 1

    def test_snapshot_skips_vault_scan(
        self,
        vault: Path,
        output_dir: Path,
        generator: SkillTreeGenerator,
    ) -> None:
        """When snapshot is provided, ``vault_path`` is not re-read."""
        empty_snapshot = VaultSnapshot(fragments=(), threads=(), eddies=())
        fragment = _build_fragment(
            frag_id="frag-should-be-ignored",
            title="Should Not Appear",
        )
        _write_fragment(vault, fragment, _make_passage(3))
        generator.generate_frequency_skills(
            vault,
            output_dir,
            snapshot=empty_snapshot,
        )
        body = (output_dir / "frequencies" / "F3.SKILL.md").read_text()
        assert "Should Not Appear" not in body


# ---- Privacy enforcement ----


class TestPrivacy:
    """Intimate fragments are excluded by default from exemplar harvesting."""

    def test_intimate_fragments_excluded_by_default(
        self,
        vault: Path,
        output_dir: Path,
        generator: SkillTreeGenerator,
    ) -> None:
        """Intimate fragments should not leak into skill exemplars."""
        fragment = _build_fragment(
            frag_id="frag-intimate",
            title="A private moment",
            privacy=PrivacyTier.INTIMATE,
        )
        _write_fragment(vault, fragment, _make_passage(3))
        generator.generate_frequency_skills(vault, output_dir)
        body = (output_dir / "frequencies" / "F3.SKILL.md").read_text()
        assert "A private moment" not in body

    def test_intimate_fragments_included_when_opted_in(
        self,
        vault: Path,
        output_dir: Path,
    ) -> None:
        """Explicit opt-in allows intimate fragments in exemplars."""
        generator = SkillTreeGenerator(allow_intimate=True)
        fragment = _build_fragment(
            frag_id="frag-intimate2",
            title="Intimate yet shared",
            privacy=PrivacyTier.INTIMATE,
        )
        _write_fragment(vault, fragment, _make_passage(3))
        generator.generate_frequency_skills(vault, output_dir)
        body = (output_dir / "frequencies" / "F3.SKILL.md").read_text()
        assert "Intimate yet shared" in body

    def test_ineligible_fragments_excluded(
        self,
        vault: Path,
        output_dir: Path,
        generator: SkillTreeGenerator,
    ) -> None:
        """Fragments marked ``voice_proxy_eligible=False`` never contribute."""
        fragment = _build_fragment(
            frag_id="frag-ineligible",
            title="Do not use my voice",
            proxy_eligible=False,
        )
        _write_fragment(vault, fragment, _make_passage(3))
        generator.generate_frequency_skills(vault, output_dir)
        body = (output_dir / "frequencies" / "F3.SKILL.md").read_text()
        assert "Do not use my voice" not in body


# ---- Exemplar selection ----


class TestExemplarSelection:
    """Exemplar selection caps and extraction rules work correctly."""

    def test_max_exemplars_caps_count(
        self,
        vault: Path,
        output_dir: Path,
    ) -> None:
        """max_exemplars limits how many passages appear in one skill file."""
        generator = SkillTreeGenerator(max_exemplars=2)
        for i in range(5):
            frag = _build_fragment(
                frag_id=f"frag-cap{i}",
                title=f"Fragment number {i}",
            )
            _write_fragment(vault, frag, _make_passage(3))
        generator.generate_frequency_skills(vault, output_dir)
        body = (output_dir / "frequencies" / "F3.SKILL.md").read_text()
        included = sum(1 for i in range(5) if f"Fragment number {i}" in body)
        assert included == 2

    def test_extract_passage_rejects_short_body(self) -> None:
        """Passages below the minimum word count are rejected."""
        assert _extract_passage("Short text.") is None

    def test_extract_passage_rejects_empty_body(self) -> None:
        """Empty bodies yield no passage."""
        assert _extract_passage("   \n\n   ") is None

    def test_extract_passage_caps_at_max_words(self) -> None:
        """Passages stop accumulating once the max word budget is reached."""
        long_text = "This is a tight sentence. " * 60
        passage = _extract_passage(long_text)
        assert passage is not None
        assert len(passage.split()) <= 180

    def test_build_exemplar_returns_none_on_short_body(self) -> None:
        """_build_exemplar gracefully handles un-extractable bodies."""
        frag = _build_fragment(frag_id="frag-none", title="Too thin")
        assert _build_exemplar(frag, "Too short.") is None


# ---- Loader behaviour ----


class TestLoaders:
    """Fragment/Thread/Eddy loaders silently skip broken files."""

    def test_load_fragment_rejects_non_fragment(self, tmp_path: Path) -> None:
        """Files whose ``type`` is not ``fragment`` return None."""
        target = tmp_path / "not-a-fragment.md"
        target.write_text("---\ntype: thread\n---\nbody", encoding="utf-8")
        assert _load_fragment(target) is None

    def test_load_fragment_rejects_invalid_yaml(self, tmp_path: Path) -> None:
        """Unreadable YAML frontmatter is skipped, not raised."""
        target = tmp_path / "broken.md"
        target.write_text(
            "---\ntype: fragment\nfrequency: [unclosed\n---\nbody",
            encoding="utf-8",
        )
        assert _load_fragment(target) is None

    def test_load_typed_model_rejects_wrong_type(self, tmp_path: Path) -> None:
        """``_load_typed_model`` enforces the expected type tag."""
        target = tmp_path / "wrong.md"
        target.write_text(
            "---\ntype: fragment\ntitle: Fragment\n---\nbody",
            encoding="utf-8",
        )
        assert (
            _load_typed_model(
                target,
                expected_type="thread",
                model_cls=Thread,
            )
            is None
        )

    def test_load_typed_model_handles_invalid_frontmatter(
        self,
        tmp_path: Path,
    ) -> None:
        """Invalid frontmatter for threads returns None."""
        target = tmp_path / "invalid-thread.md"
        target.write_text(
            "---\ntype: thread\nfragment_count: not-a-number\n---\nbody",
            encoding="utf-8",
        )
        assert (
            _load_typed_model(
                target,
                expected_type="thread",
                model_cls=Thread,
            )
            is None
        )

    def test_load_vault_snapshot_gathers_fragments_threads_and_eddies(
        self,
        vault: Path,
    ) -> None:
        """The snapshot helper composes fragments, threads, and eddies."""
        frag = _build_fragment(frag_id="frag-snap1", title="Snap")
        _write_fragment(vault, frag, _make_passage(3))
        thread = Thread(
            id="thread-snap",
            title="Snap thread",
            status=ThreadStatus.ACTIVE,
            fragment_count=20,
        )
        _write_thread(vault, thread)
        eddy = Eddy(
            id="eddy-snap",
            title="Snap eddy",
            fragment_count=20,
            description="an eddy",
        )
        _write_eddy(vault, eddy)
        snapshot = _load_vault_snapshot(vault, allow_intimate=False)
        assert len(snapshot.fragments) == 1
        assert len(snapshot.threads) == 1
        assert len(snapshot.eddies) == 1


# ---- Utility helpers ----


class TestUtilities:
    """Small helpers backing the generator."""

    def test_slugify_handles_punctuation(self) -> None:
        """Slugification strips punctuation and lowercases."""
        assert _slugify("Hello, World!") == "hello-world"

    def test_slugify_defaults_empty_input_to_untitled(self) -> None:
        """An empty slug falls back to 'untitled'."""
        assert _slugify("!!!") == "untitled"

    def test_mode_orientation_key_swaps_underscore(self) -> None:
        """The composed key is lowercase and hyphen-separated."""
        assert _mode_orientation_key("absorb", "do_feel") == "absorb-do-feel"

    def test_skill_exemplar_is_hashable(self) -> None:
        """Frozen dataclasses must be hashable for set membership."""
        exemplar = SkillExemplar(
            fragment_id="frag-1",
            fragment_title="title",
            passage="passage",
        )
        assert isinstance(hash(exemplar), int)

    def test_unique_slug_returns_base_when_no_collision(self) -> None:
        """A fresh base slug is used unchanged."""
        assert _unique_slug("foo", "bar", set()) == "foo"

    def test_unique_slug_appends_fallback_on_base_collision(self) -> None:
        """When the base collides, the fallback id is appended."""
        used = {"foo"}
        assert _unique_slug("foo", "thread-abc", used) == "foo-thread-abc"

    def test_unique_slug_suffixes_counter_on_fallback_collision(self) -> None:
        """When both base and fallback collide, a numeric suffix is added."""
        used = {"foo", "foo-bar"}
        result = _unique_slug("foo", "bar", used)
        assert result not in used
        assert result.startswith("foo-bar-")

    def test_unique_slug_increments_counter_through_conflicts(self) -> None:
        """The counter skips any already-used variant."""
        used = {"foo", "foo-bar", "foo-bar-2"}
        result = _unique_slug("foo", "bar", used)
        assert result == "foo-bar-3"


# ---- Classification filtering ----


class TestClassificationFiltering:
    """Grouping helpers must skip unclassified / unregistered fragments."""

    def test_unclassified_frequency_fragments_not_used_as_exemplars(
        self,
        vault: Path,
        output_dir: Path,
        generator: SkillTreeGenerator,
    ) -> None:
        """Fragments without a primary frequency stay out of frequency files."""
        frag = _build_fragment(
            frag_id="frag-unclass",
            title="No frequency",
            frequency=Frequency.UNCLASSIFIED,
        )
        _write_fragment(vault, frag, _make_passage(3))
        generator.generate_frequency_skills(vault, output_dir)
        for freq_key in FREQUENCY_KEYS:
            body = (output_dir / "frequencies" / f"{freq_key}.SKILL.md").read_text()
            assert "No frequency" not in body

    def test_unclassified_phase_fragments_not_used_as_exemplars(
        self,
        vault: Path,
        output_dir: Path,
        generator: SkillTreeGenerator,
    ) -> None:
        """Fragments without a phase stay out of phase files."""
        frag = _build_fragment(
            frag_id="frag-no-phase",
            title="No phase here",
            phase=Phase.UNCLASSIFIED,
        )
        _write_fragment(vault, frag, _make_passage(3))
        generator.generate_phase_skills(vault, output_dir)
        for phase_key in PHASE_KEYS:
            body = (output_dir / "phases" / f"{phase_key}.SKILL.md").read_text()
            assert "No phase here" not in body

    def test_unclassified_mode_fragments_not_used_as_exemplars(
        self,
        vault: Path,
        output_dir: Path,
        generator: SkillTreeGenerator,
    ) -> None:
        """Fragments without a mode stay out of mode files."""
        frag = _build_fragment(
            frag_id="frag-no-mode",
            title="No mode set",
            mode=Mode.UNCLASSIFIED,
        )
        _write_fragment(vault, frag, _make_passage(3))
        generator.generate_mode_skills(vault, output_dir)
        for mode, orientation in MODE_ORIENTATION_KEYS:
            filename = f"{mode}-{orientation.replace('_', '-')}.SKILL.md"
            body = (output_dir / "modes" / filename).read_text()
            assert "No mode set" not in body

    def test_unclassified_orientation_fragments_not_used_as_exemplars(
        self,
        vault: Path,
        output_dir: Path,
        generator: SkillTreeGenerator,
    ) -> None:
        """Non-Absorb fragments without orientation stay out of mode files."""
        frag = _build_fragment(
            frag_id="frag-no-orient",
            title="No orientation",
            orientation=Orientation.UNCLASSIFIED,
        )
        _write_fragment(vault, frag, _make_passage(3))
        generator.generate_mode_skills(vault, output_dir)
        for mode, orientation in MODE_ORIENTATION_KEYS:
            filename = f"{mode}-{orientation.replace('_', '-')}.SKILL.md"
            body = (output_dir / "modes" / filename).read_text()
            assert "No orientation" not in body

    def test_fragments_without_register_not_used_as_exemplars(
        self,
        vault: Path,
        output_dir: Path,
        generator: SkillTreeGenerator,
    ) -> None:
        """Fragments without a register stay out of register files."""
        frag = _build_fragment(
            frag_id="frag-no-register",
            title="Untagged register",
            register=None,
        )
        _write_fragment(vault, frag, _make_passage(3))
        generator.generate_register_skills(vault, output_dir)
        for register_key in REGISTER_KEYS:
            body = (output_dir / "registers" / f"{register_key}.SKILL.md").read_text()
            assert "Untagged register" not in body


# ---- Passage extraction edge cases ----


class TestExtractPassageEdgeCases:
    """Additional branch coverage for the sentence-level passage extractor."""

    def test_empty_sentences_are_skipped(self) -> None:
        """Whitespace-only sentences do not halt the loop."""
        body = "   .   This is a first proper sentence with enough words here. " + (
            "And this is the second proper sentence with the rest of the budget. "
        )
        passage = _extract_passage(body * 2)
        assert passage is not None

    def test_long_first_sentence_returns_none(self) -> None:
        """A single sentence exceeding the max word budget is rejected."""
        huge = " ".join(["word"] * 300) + "."
        assert _extract_passage(huge) is None

    def test_second_sentence_dropped_when_it_overflows_budget(self) -> None:
        """Once the first sentence is in, oversize follow-ups are cut."""
        first = " ".join(["word"] * 35) + "."
        second = " ".join(["word"] * 200) + "."
        passage = _extract_passage(f"{first} {second}")
        assert passage is not None
        assert passage == first

    def test_overflowing_follow_up_below_minimum_returns_none(self) -> None:
        """If the first sentence is under minimum and the next overflows, None."""
        first = " ".join(["word"] * 20) + "."
        second = " ".join(["word"] * 170) + "."
        assert _extract_passage(f"{first} {second}") is None


class TestMissingSubdirs:
    """Generation works even when vault subdirectories are absent."""

    def test_missing_subdirs_produce_snapshots_without_errors(
        self,
        tmp_path: Path,
        generator: SkillTreeGenerator,
    ) -> None:
        """A vault without 01-Fragments, 02-Threads, or 03-Eddies still works."""
        bare_vault = tmp_path / "bare"
        bare_vault.mkdir()
        output_dir = tmp_path / "out"
        paths = generator.generate_all_skills(bare_vault, output_dir)
        assert len(paths) == 34


# ---- Invalid fragment handling in snapshot ----


class TestSnapshotInvalidFragments:
    """Invalid fragment files must be silently skipped."""

    def test_invalid_fragment_in_vault_is_skipped(
        self,
        vault: Path,
        output_dir: Path,
        generator: SkillTreeGenerator,
    ) -> None:
        """Fragments that fail model validation should not crash generation."""
        bogus = vault / "01-Fragments" / "Journal" / "bogus.md"
        bogus.parent.mkdir(parents=True, exist_ok=True)
        bogus.write_text(
            "---\ntype: fragment\nfrequency: not-a-dict\n---\nbody",
            encoding="utf-8",
        )
        paths = generator.generate_frequency_skills(vault, output_dir)
        assert len(paths) == 10

    def test_short_body_fragment_contributes_nothing_to_exemplars(
        self,
        vault: Path,
        output_dir: Path,
        generator: SkillTreeGenerator,
    ) -> None:
        """Passage extraction failures don't prevent later exemplars."""
        short = _build_fragment(frag_id="frag-short", title="Short")
        usable = _build_fragment(frag_id="frag-good", title="Usable")
        _write_fragment(vault, short, "Hi.")
        _write_fragment(vault, usable, _make_passage(3))
        generator.generate_frequency_skills(vault, output_dir)
        body = (output_dir / "frequencies" / "F3.SKILL.md").read_text()
        assert "Usable" in body
        assert "Short" not in body

    def test_invalid_thread_yaml_is_skipped(
        self,
        vault: Path,
        output_dir: Path,
        generator: SkillTreeGenerator,
    ) -> None:
        """Thread files with unreadable YAML do not crash generation."""
        target = vault / "02-Threads" / "Active" / "broken.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            "---\ntype: thread\ntitle: [unclosed\n---\nbody",
            encoding="utf-8",
        )
        assert not generator.generate_thread_skills(vault, output_dir)

    def test_non_eddy_file_under_eddies_dir_is_skipped(
        self,
        vault: Path,
        output_dir: Path,
        generator: SkillTreeGenerator,
    ) -> None:
        """Markdown files under 03-Eddies that aren't eddies are ignored."""
        target = vault / "03-Eddies" / "note.md"
        target.write_text(
            "---\ntype: fragment\ntitle: Not an eddy\n---\nbody",
            encoding="utf-8",
        )
        assert not generator.generate_eddy_skills(vault, output_dir)

    def test_non_absorb_fragment_populates_specific_mode_skill(
        self,
        vault: Path,
        output_dir: Path,
        generator: SkillTreeGenerator,
    ) -> None:
        """Non-absorb fragments land in their specific mode/orientation file."""
        frag = _build_fragment(
            frag_id="frag-mode-specific",
            title="A collaborate-feel moment",
            mode=Mode.COLLABORATE,
            orientation=Orientation.FEEL,
        )
        _write_fragment(vault, frag, _make_passage(3))
        generator.generate_mode_skills(vault, output_dir)
        body = (output_dir / "modes" / "collaborate-feel.SKILL.md").read_text()
        assert "A collaborate-feel moment" in body

    def test_register_classified_fragment_populates_register_skill(
        self,
        vault: Path,
        output_dir: Path,
        generator: SkillTreeGenerator,
    ) -> None:
        """Fragments with a register land in that register's skill file."""
        frag = _build_fragment(
            frag_id="frag-proph",
            title="Prophetic line",
            register=VoiceRegister.PROPHETIC,
        )
        _write_fragment(vault, frag, _make_passage(3))
        generator.generate_register_skills(vault, output_dir)
        body = (output_dir / "registers" / "prophetic.SKILL.md").read_text()
        assert "Prophetic line" in body


# ---- Signature-only variant (issue #353) ----


SIGNATURE_PASSAGE_MARKER = "Verbatim user prose: standing in my heat alone"


def _populated_vault_for_signature_tests(vault_path: Path) -> Fragment:
    """Write a single proxy-eligible fragment whose body is recognisable.

    Used by signature-only tests to guarantee that the ``SKILL`` variant
    contains a quoted exemplar while the ``SIGNATURE`` variant does not.
    """
    fragment = _build_fragment(
        frag_id="frag-sig-marker",
        title="Standing in my heat",
        frequency=Frequency.F3,
        phase=Phase.RISING,
        mode=Mode.EXPRESS,
        orientation=Orientation.DO,
        register=VoiceRegister.CONFESSIONAL,
    )
    body = (
        f"{SIGNATURE_PASSAGE_MARKER} for the very first sentence, with "
        "more than the minimum number of words required to register as "
        "substantive voice data inside the passage extractor. "
        f"{SIGNATURE_PASSAGE_MARKER} once more in a second sentence so the "
        "extractor has multiple sentences to choose between."
    )
    _write_fragment(vault_path, fragment, body)
    return fragment


class TestSignatureOnlyConstructor:
    """``signature_only`` is exposed as a constructor flag."""

    def test_default_signature_only_is_false(self) -> None:
        """The default mode keeps backwards-compatible SKILL behaviour."""
        generator = SkillTreeGenerator()
        assert not generator.signature_only

    def test_signature_only_flag_is_accepted(self) -> None:
        """Passing ``signature_only=True`` is preserved on the instance."""
        generator = SkillTreeGenerator(signature_only=True)
        assert generator.signature_only


class TestSignatureOnlyVariant:
    """Signature-only generation strips quoted exemplars and fragment IDs."""

    def test_signature_only_uses_signature_suffix(
        self,
        vault: Path,
        output_dir: Path,
    ) -> None:
        """Signature files are written under ``F*.SIGNATURE.md``."""
        paths = SkillTreeGenerator(
            signature_only=True,
        ).generate_frequency_skills(vault, output_dir)
        assert all(p.name.endswith(".SIGNATURE.md") for p in paths)
        assert not any(p.name.endswith(".SKILL.md") for p in paths)

    def test_signature_only_skips_exemplar_section(
        self,
        vault: Path,
        output_dir: Path,
    ) -> None:
        """Signature-only output omits the ``## Exemplar Passages`` heading."""
        _populated_vault_for_signature_tests(vault)
        generator = SkillTreeGenerator(signature_only=True)
        generator.generate_frequency_skills(vault, output_dir)
        body = (output_dir / "frequencies" / "F3.SIGNATURE.md").read_text()
        assert "## Exemplar Passages" not in body

    def test_signature_only_excludes_quoted_passages(
        self,
        vault: Path,
        output_dir: Path,
    ) -> None:
        """No quoted prose from any fragment should reach the signature output."""
        _populated_vault_for_signature_tests(vault)
        generator = SkillTreeGenerator(signature_only=True)
        generator.generate_frequency_skills(vault, output_dir)
        body = (output_dir / "frequencies" / "F3.SIGNATURE.md").read_text()
        assert SIGNATURE_PASSAGE_MARKER not in body

    def test_signature_only_excludes_fragment_ids(
        self,
        vault: Path,
        output_dir: Path,
    ) -> None:
        """Fragment IDs never appear in signature-only files."""
        fragment = _populated_vault_for_signature_tests(vault)
        generator = SkillTreeGenerator(signature_only=True)
        generator.generate_frequency_skills(vault, output_dir)
        body = (output_dir / "frequencies" / "F3.SIGNATURE.md").read_text()
        assert fragment.id not in body

    def test_signature_only_preserves_voice_texture(
        self,
        vault: Path,
        output_dir: Path,
    ) -> None:
        """All non-exemplar dimension fields remain populated."""
        generator = SkillTreeGenerator(signature_only=True)
        generator.generate_frequency_skills(vault, output_dir)
        body = (output_dir / "frequencies" / "F3.SIGNATURE.md").read_text()
        # The non-exemplar sections must still be present.
        assert "# Activation" in body
        assert "## Description" in body
        assert "## Writing Instructions" in body
        assert "## Anti-Patterns" in body
        assert "## Combining With Other Skills" in body
        # Voice texture content (from FREQUENCY_VOICE_TEXTURES["F3"])
        # must still drive the body so the LLM keeps signature guidance.
        assert "Declarative first-person" in body

    def test_signature_only_announces_variant_in_header(
        self,
        vault: Path,
        output_dir: Path,
    ) -> None:
        """Each signature file begins with a variant declaration header."""
        generator = SkillTreeGenerator(signature_only=True)
        generator.generate_frequency_skills(vault, output_dir)
        body = (output_dir / "frequencies" / "F3.SIGNATURE.md").read_text()
        assert "Variant: SIGNATURE" in body
        assert "no quoted user content" in body.lower()

    def test_default_variant_announces_skill_header(
        self,
        vault: Path,
        output_dir: Path,
        generator: SkillTreeGenerator,
    ) -> None:
        """The legacy SKILL variant declares itself in its own header."""
        generator.generate_frequency_skills(vault, output_dir)
        body = (output_dir / "frequencies" / "F3.SKILL.md").read_text()
        assert "Variant: SKILL" in body

    def test_signature_only_frontmatter_records_variant(
        self,
        vault: Path,
        output_dir: Path,
    ) -> None:
        """The frontmatter ``variant`` field distinguishes the two variants."""
        generator = SkillTreeGenerator(signature_only=True)
        generator.generate_frequency_skills(vault, output_dir)
        post = frontmatter.load(
            str(output_dir / "frequencies" / "F3.SIGNATURE.md"),
        )
        assert post.metadata["variant"] == "signature"

    def test_default_variant_frontmatter_records_skill(
        self,
        vault: Path,
        output_dir: Path,
        generator: SkillTreeGenerator,
    ) -> None:
        """The default variant records ``variant: skill`` in frontmatter."""
        generator.generate_frequency_skills(vault, output_dir)
        post = frontmatter.load(
            str(output_dir / "frequencies" / "F3.SKILL.md"),
        )
        assert post.metadata["variant"] == "skill"

    def test_default_variant_tags_do_not_duplicate_skill(
        self,
        vault: Path,
        output_dir: Path,
        generator: SkillTreeGenerator,
    ) -> None:
        """Legacy SKILL frontmatter ``tags`` list contains ``skill`` exactly once.

        Regression for PR #357 review: the previous tag list expanded
        ``[\"skill\", category, variant, ...]`` with ``variant == \"skill\"``,
        yielding two identical ``\"skill\"`` entries that confuse any
        downstream consumer that parses the raw frontmatter.
        """
        generator.generate_frequency_skills(vault, output_dir)
        post = frontmatter.load(
            str(output_dir / "frequencies" / "F3.SKILL.md"),
        )
        tags = post.metadata["tags"]
        assert tags.count("skill") == 1
        assert "frequency" in tags

    def test_signature_variant_tags_include_signature_marker(
        self,
        vault: Path,
        output_dir: Path,
    ) -> None:
        """The SIGNATURE variant gets a distinguishing ``signature`` tag.

        The tag is what lets a downstream loader filter for the new
        variant; dropping it would erase the only frontmatter-level
        difference between the two trees beyond the filename suffix.
        """
        generator = SkillTreeGenerator(signature_only=True)
        generator.generate_frequency_skills(vault, output_dir)
        post = frontmatter.load(
            str(output_dir / "frequencies" / "F3.SIGNATURE.md"),
        )
        tags = post.metadata["tags"]
        assert "signature" in tags
        assert tags.count("skill") == 1

    def test_skill_header_does_not_reference_dead_signature_file(
        self,
        vault: Path,
        output_dir: Path,
        generator: SkillTreeGenerator,
    ) -> None:
        """Default SKILL banner does not point at a possibly-missing SIGNATURE file.

        Regression for PR #357 review: the previous banner read \"see the
        matching ``.SIGNATURE.md`` file\", which is a dead reference for
        any operator who has only ever run the default command. The
        replacement banner names the CLI invocation that produces the
        alternative variant instead.
        """
        generator.generate_frequency_skills(vault, output_dir)
        body = (output_dir / "frequencies" / "F3.SKILL.md").read_text()
        assert "matching" not in body
        assert "see the matching" not in body
        assert "--signature-only" in body


class TestSignatureOnlyCovering:
    """Signature-only generation covers every category of skill."""

    def test_signature_only_covers_phases(
        self,
        vault: Path,
        output_dir: Path,
    ) -> None:
        """Phase skill files appear as ``<phase>.SIGNATURE.md``."""
        paths = SkillTreeGenerator(
            signature_only=True,
        ).generate_phase_skills(vault, output_dir)
        assert len(paths) == 6
        assert all(p.name.endswith(".SIGNATURE.md") for p in paths)

    def test_signature_only_covers_modes(
        self,
        vault: Path,
        output_dir: Path,
    ) -> None:
        """Mode skill files appear with the SIGNATURE suffix."""
        paths = SkillTreeGenerator(
            signature_only=True,
        ).generate_mode_skills(vault, output_dir)
        assert len(paths) == 9
        assert all(p.name.endswith(".SIGNATURE.md") for p in paths)

    def test_signature_only_covers_registers(
        self,
        vault: Path,
        output_dir: Path,
    ) -> None:
        """Register skill files appear with the SIGNATURE suffix."""
        paths = SkillTreeGenerator(
            signature_only=True,
        ).generate_register_skills(vault, output_dir)
        assert len(paths) == 7
        assert all(p.name.endswith(".SIGNATURE.md") for p in paths)

    def test_signature_only_covers_threads(
        self,
        vault: Path,
        output_dir: Path,
    ) -> None:
        """Qualifying threads earn a SIGNATURE file (threads have no exemplars)."""
        thread = Thread(
            id="thread-sig",
            title="Signature Thread",
            status=ThreadStatus.ACTIVE,
            fragment_count=25,
            description="Spans many seasons.",
        )
        _write_thread(vault, thread)
        paths = SkillTreeGenerator(
            signature_only=True,
        ).generate_thread_skills(vault, output_dir)
        assert len(paths) == 1
        assert paths[0].name.endswith(".SIGNATURE.md")
        body = paths[0].read_text()
        assert "Variant: SIGNATURE" in body

    def test_signature_only_covers_eddies(
        self,
        vault: Path,
        output_dir: Path,
    ) -> None:
        """Qualifying eddies earn a SIGNATURE file."""
        eddy = Eddy(
            id="eddy-sig",
            title="Signature Eddy",
            fragment_count=42,
            threads=["thread-sig"],
            description="Major cluster.",
        )
        _write_eddy(vault, eddy)
        paths = SkillTreeGenerator(
            signature_only=True,
        ).generate_eddy_skills(vault, output_dir)
        assert len(paths) == 1
        assert paths[0].name.endswith(".SIGNATURE.md")

    def test_signature_only_covers_meta(
        self,
        output_dir: Path,
    ) -> None:
        """Meta skills carry the SIGNATURE suffix in signature mode."""
        paths = SkillTreeGenerator(
            signature_only=True,
        ).generate_meta_skills(output_dir)
        assert len(paths) == 2
        assert all(p.name.endswith(".SIGNATURE.md") for p in paths)
        body = paths[0].read_text()
        assert "Variant: SIGNATURE" in body

    def test_signature_only_generate_all_writes_signature_tree(
        self,
        vault: Path,
        output_dir: Path,
    ) -> None:
        """``generate_all_skills`` produces only SIGNATURE files when enabled."""
        paths = SkillTreeGenerator(
            signature_only=True,
        ).generate_all_skills(vault, output_dir)
        assert len(paths) == 34
        assert all(p.name.endswith(".SIGNATURE.md") for p in paths)


class TestSignatureAndSkillCoexist:
    """Both variants live side-by-side in the same output directory."""

    def test_both_variants_coexist_without_overwrite(
        self,
        vault: Path,
        output_dir: Path,
    ) -> None:
        """Running both generators writes distinct files for each variant."""
        SkillTreeGenerator().generate_frequency_skills(vault, output_dir)
        SkillTreeGenerator(signature_only=True).generate_frequency_skills(
            vault,
            output_dir,
        )
        freq_dir = output_dir / "frequencies"
        assert (freq_dir / "F3.SKILL.md").exists()
        assert (freq_dir / "F3.SIGNATURE.md").exists()
        skill_files = sorted(p.name for p in freq_dir.glob("F*.SKILL.md"))
        signature_files = sorted(p.name for p in freq_dir.glob("F*.SIGNATURE.md"))
        assert len(skill_files) == 10
        assert len(signature_files) == 10

    def test_skill_variant_contains_exemplars_when_available(
        self,
        vault: Path,
        output_dir: Path,
    ) -> None:
        """The SKILL variant still quotes the user's prose."""
        _populated_vault_for_signature_tests(vault)
        SkillTreeGenerator().generate_frequency_skills(vault, output_dir)
        skill_body = (output_dir / "frequencies" / "F3.SKILL.md").read_text()
        assert SIGNATURE_PASSAGE_MARKER in skill_body
        assert "## Exemplar Passages" in skill_body

    def test_signature_variant_remains_clean_alongside_skill(
        self,
        vault: Path,
        output_dir: Path,
    ) -> None:
        """The SIGNATURE variant must stay quote-free even when SKILL ran first."""
        _populated_vault_for_signature_tests(vault)
        SkillTreeGenerator().generate_frequency_skills(vault, output_dir)
        SkillTreeGenerator(signature_only=True).generate_frequency_skills(
            vault,
            output_dir,
        )
        signature_body = (output_dir / "frequencies" / "F3.SIGNATURE.md").read_text()
        assert SIGNATURE_PASSAGE_MARKER not in signature_body
        assert "## Exemplar Passages" not in signature_body
        # The legacy file remained intact across the second generation pass.
        skill_body = (output_dir / "frequencies" / "F3.SKILL.md").read_text()
        assert SIGNATURE_PASSAGE_MARKER in skill_body
