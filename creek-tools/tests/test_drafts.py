"""Tests for the draft generation workflow (issue #53)."""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

import frontmatter
import pytest
import yaml

from creek.config import AIStyleConfig
from creek.generate.ai_style.model import FeatureStat, VoiceFingerprint
from creek.generate.drafts import (
    DRAFTS_SUBDIR,
    Draft,
    DraftGenerator,
    SeedResolutionError,
    SeedSpec,
)
from creek.generate.grounding import GroundingThresholds
from creek.generate.mining import IdeaSeed, MiningStrategy
from creek.generate.outline import OutlineSection, build_stitch_prompt
from creek.models import (
    Confidence,
    Dosage,
    Eddy,
    Fragment,
    FragmentSource,
    Frequency,
    FrequencyClassification,
    Mode,
    Orientation,
    Phase,
    PraxisPotential,
    SourcePlatform,
    Thread,
    ThreadStatus,
    VoiceClassification,
    VoiceRegister,
    WavelengthClassification,
)
from tests.factories.compiled import (
    write_compiled_eddy_page as _write_compiled_eddy_page,
)
from tests.factories.compiled import (
    write_compiled_thread_page as _write_compiled_thread_page,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


def _build_fragment(
    *,
    frag_id: str = "frag-001",
    title: str = "A moment",
    primary: Frequency = Frequency.F1,
    mode: Mode = Mode.UNCLASSIFIED,
    orientation: Orientation = Orientation.UNCLASSIFIED,
    register: VoiceRegister | None = None,
    phase: Phase = Phase.UNCLASSIFIED,
    dosage: Dosage = Dosage.UNCLASSIFIED,
    threads: tuple[str, ...] = (),
    eddies: tuple[str, ...] = (),
) -> Fragment:
    """Return a deterministic Fragment for tests."""
    return Fragment(
        id=frag_id,
        title=title,
        source=FragmentSource(
            platform=SourcePlatform.CLAUDE,
            original_file="scratch.md",
        ),
        created=datetime(2026, 3, 1, tzinfo=UTC),
        ingested=datetime(2026, 3, 1, tzinfo=UTC),
        frequency=FrequencyClassification(primary=primary),
        wavelength=WavelengthClassification(
            mode=mode,
            orientation=orientation,
            phase=phase,
            dosage=dosage,
        ),
        voice=VoiceClassification(
            voice_register=register,
            confidence=Confidence.SETTLED if register else None,
        ),
        praxis_potential=PraxisPotential.LATENT,
        threads=list(threads),
        eddies=list(eddies),
    )


def _write_fragment(vault: Path, fragment: Fragment, body: str) -> Path:
    """Write *fragment* to 01-Fragments/{id}.md and return the path."""
    root = vault / "01-Fragments"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{fragment.id}.md"
    post = frontmatter.Post(content=body, **fragment.model_dump(mode="json"))
    path.write_text(frontmatter.dumps(post), encoding="utf-8")
    return path


def _write_thread(vault: Path, thread: Thread) -> Path:
    """Write *thread* to 02-Threads/{id}.md and return the path."""
    root = vault / "02-Threads"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{thread.id}.md"
    post = frontmatter.Post(content="", **thread.model_dump(mode="json"))
    path.write_text(frontmatter.dumps(post), encoding="utf-8")
    return path


def _write_eddy(vault: Path, eddy: Eddy) -> Path:
    """Write *eddy* to 03-Eddies/{id}.md and return the path."""
    root = vault / "03-Eddies"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{eddy.id}.md"
    post = frontmatter.Post(content="", **eddy.model_dump(mode="json"))
    path.write_text(frontmatter.dumps(post), encoding="utf-8")
    return path


def _build_seed(
    *,
    title: str = "Naming what orbits",
    strategy: MiningStrategy = MiningStrategy.LIMINAL_CROSS_EDDY,
    source_fragments: tuple[str, ...] = ("frag-001",),
    threads: tuple[str, ...] = (),
    eddies: tuple[str, ...] = (),
    frequency_affinity: tuple[Frequency, ...] = (Frequency.F1,),
    description: str = "An essay waits here.",
    score: float = 0.8,
) -> IdeaSeed:
    """Return a deterministic IdeaSeed for tests."""
    return IdeaSeed(
        strategy=strategy,
        title=title,
        source_fragments=source_fragments,
        threads=threads,
        eddies=eddies,
        frequency_affinity=frequency_affinity,
        brief_description=description,
        score=score,
    )


def _seed_skill_tree(skills_root: Path, *names: str) -> None:
    """Create empty SKILL.md files at ``skills_root/<name>`` for each *name*."""
    for name in names:
        target = skills_root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# skill\n", encoding="utf-8")


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    """Create a minimal vault layout under ``tmp_path``."""
    for sub in ("01-Fragments", "02-Threads", "03-Eddies", "07-Voice/Drafts"):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture
def skills_root(tmp_path: Path) -> Path:
    """Create the skill tree directories under ``tmp_path/skills``."""
    root = tmp_path / "skills"
    for sub in ("frequencies", "phases", "modes", "registers"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def llm_echo() -> Callable[[str], str]:
    """An LLM stub that echoes a marker plus prompt length."""

    def _call(prompt: str) -> str:
        return f"DRAFT({len(prompt)} chars)\n\nGenerated body."

    return _call


# ---- Draft dataclass ---------------------------------------------------


class TestDraft:
    """Invariants on the :class:`Draft` value object."""

    def test_draft_is_frozen(self) -> None:
        """Drafts are immutable value objects."""
        draft = Draft(
            title="x",
            body="y",
            idea_strategy="thread_terminus",
            source_fragments=(),
            threads=(),
            eddies=(),
            skill_stack=(),
            prompt="p",
            generated_date=datetime(2026, 4, 20, tzinfo=UTC),
        )
        with pytest.raises(AttributeError):
            draft.title = "changed"  # type: ignore[misc]

    def test_draft_requires_title(self) -> None:
        """Empty titles are rejected."""
        with pytest.raises(ValueError, match="title"):
            Draft(
                title="   ",
                body="body",
                idea_strategy="thread_terminus",
                source_fragments=(),
                threads=(),
                eddies=(),
                skill_stack=(),
                prompt="p",
                generated_date=datetime(2026, 4, 20, tzinfo=UTC),
            )

    def test_draft_requires_body(self) -> None:
        """Empty bodies are rejected."""
        with pytest.raises(ValueError, match="body"):
            Draft(
                title="title",
                body="\n",
                idea_strategy="thread_terminus",
                source_fragments=(),
                threads=(),
                eddies=(),
                skill_stack=(),
                prompt="p",
                generated_date=datetime(2026, 4, 20, tzinfo=UTC),
            )


# ---- present_idea ------------------------------------------------------


class TestPresentIdea:
    """``present_idea`` formats seeds as invitations, not imperatives."""

    def test_contains_title_and_description(
        self,
        skills_root: Path,
        llm_echo: Callable[[str], str],
    ) -> None:
        """The invitation text surfaces the seed's title and description."""
        gen = DraftGenerator(llm=llm_echo, skills_root=skills_root)
        seed = _build_seed(
            title="What the creek teaches",
            description="A three-section meditation on listening.",
        )
        text = gen.present_idea(seed)
        assert "What the creek teaches" in text
        assert "three-section meditation" in text

    def test_mentions_strategy_human_readable(
        self,
        skills_root: Path,
        llm_echo: Callable[[str], str],
    ) -> None:
        """The strategy underscores are softened for readability."""
        gen = DraftGenerator(llm=llm_echo, skills_root=skills_root)
        seed = _build_seed(strategy=MiningStrategy.RESONANCE_CHAIN)
        text = gen.present_idea(seed)
        assert "resonance chain" in text

    def test_is_invitational_not_imperative(
        self,
        skills_root: Path,
        llm_echo: Callable[[str], str],
    ) -> None:
        """The presentation uses invitation language rather than commands."""
        gen = DraftGenerator(llm=llm_echo, skills_root=skills_root)
        seed = _build_seed()
        text = gen.present_idea(seed).lower()
        assert any(word in text for word in ("may", "consider", "invitation"))
        assert "you must" not in text


# ---- select_skill_stack -----------------------------------------------


class TestSelectSkillStack:
    """``select_skill_stack`` assembles the activated SKILL.md files."""

    def test_selects_frequency_skill_files(
        self,
        vault: Path,
        skills_root: Path,
        llm_echo: Callable[[str], str],
    ) -> None:
        """Frequency affinities map to frequencies/<F>.SKILL.md."""
        _seed_skill_tree(skills_root, "frequencies/F1.SKILL.md")
        gen = DraftGenerator(llm=llm_echo, skills_root=skills_root)
        seed = _build_seed(frequency_affinity=(Frequency.F1,))
        stack = gen.select_skill_stack(seed, vault_path=vault)
        assert skills_root / "frequencies" / "F1.SKILL.md" in stack

    def test_selects_phase_skill_when_provided(
        self,
        vault: Path,
        skills_root: Path,
        llm_echo: Callable[[str], str],
    ) -> None:
        """Known phases produce phases/<phase>.SKILL.md."""
        _seed_skill_tree(skills_root, "phases/rising.SKILL.md")
        gen = DraftGenerator(llm=llm_echo, skills_root=skills_root)
        seed = _build_seed()
        stack = gen.select_skill_stack(
            seed,
            vault_path=vault,
            current_phase=Phase.RISING,
        )
        assert skills_root / "phases" / "rising.SKILL.md" in stack

    def test_skips_unclassified_phase(
        self,
        vault: Path,
        skills_root: Path,
        llm_echo: Callable[[str], str],
    ) -> None:
        """Unclassified phase produces no phase skill."""
        _seed_skill_tree(skills_root, "phases/unclassified.SKILL.md")
        gen = DraftGenerator(llm=llm_echo, skills_root=skills_root)
        stack = gen.select_skill_stack(
            _build_seed(),
            vault_path=vault,
            current_phase=Phase.UNCLASSIFIED,
        )
        assert skills_root / "phases" / "unclassified.SKILL.md" not in stack

    def test_selects_mode_and_register_from_source_fragments(
        self,
        vault: Path,
        skills_root: Path,
        llm_echo: Callable[[str], str],
    ) -> None:
        """Dominant mode+orientation and register are looked up from source frags."""
        fragment = _build_fragment(
            mode=Mode.EXPRESS,
            orientation=Orientation.FEEL,
            register=VoiceRegister.CONFESSIONAL,
        )
        _write_fragment(vault, fragment, "body text")
        _seed_skill_tree(
            skills_root,
            "modes/express-feel.SKILL.md",
            "registers/confessional.SKILL.md",
        )
        gen = DraftGenerator(llm=llm_echo, skills_root=skills_root)
        seed = _build_seed(source_fragments=(fragment.id,))
        stack = gen.select_skill_stack(seed, vault_path=vault)
        assert skills_root / "modes" / "express-feel.SKILL.md" in stack
        assert skills_root / "registers" / "confessional.SKILL.md" in stack

    def test_skips_missing_skill_files(
        self,
        vault: Path,
        skills_root: Path,
        llm_echo: Callable[[str], str],
    ) -> None:
        """Missing SKILL.md files are silently dropped."""
        gen = DraftGenerator(llm=llm_echo, skills_root=skills_root)
        seed = _build_seed(frequency_affinity=(Frequency.F2,))
        stack = gen.select_skill_stack(seed, vault_path=vault)
        assert stack == []


# ---- gather_source_material -------------------------------------------


class TestGatherSourceMaterial:
    """``gather_source_material`` stitches fragment/thread/eddy text."""

    def test_includes_fragment_body_and_title(
        self,
        vault: Path,
        skills_root: Path,
        llm_echo: Callable[[str], str],
    ) -> None:
        """Source fragments contribute title + body to the block."""
        fragment = _build_fragment(frag_id="frag-A", title="At the edge")
        _write_fragment(vault, fragment, "Water slips through cupped hands.")
        gen = DraftGenerator(llm=llm_echo, skills_root=skills_root)
        seed = _build_seed(source_fragments=("frag-A",))
        block = gen.gather_source_material(seed, vault_path=vault)
        assert "## Source fragments" in block
        assert "frag-A" in block
        assert "At the edge" in block
        assert "Water slips through cupped hands." in block

    def test_includes_thread_and_eddy_descriptions(
        self,
        vault: Path,
        skills_root: Path,
        llm_echo: Callable[[str], str],
    ) -> None:
        """Thread and eddy descriptions flow into their own sections."""
        thread = Thread(
            id="thread-A",
            title="Listening practice",
            status=ThreadStatus.ACTIVE,
            first_seen=date(2026, 1, 1),
            last_seen=date(2026, 4, 1),
            description="A recurring return to listening as practice.",
        )
        _write_thread(vault, thread)
        eddy = Eddy(
            id="eddy-A",
            title="Water as teacher",
            formed=date(2026, 1, 1),
            description="Water metaphors cluster here.",
        )
        _write_eddy(vault, eddy)
        gen = DraftGenerator(llm=llm_echo, skills_root=skills_root)
        seed = _build_seed(threads=("thread-A",), eddies=("eddy-A",))
        block = gen.gather_source_material(seed, vault_path=vault)
        assert "## Threads" in block
        assert "Listening practice" in block
        assert "## Eddies" in block
        assert "Water as teacher" in block

    def test_empty_when_no_ids_match(
        self,
        vault: Path,
        skills_root: Path,
        llm_echo: Callable[[str], str],
    ) -> None:
        """Unknown IDs produce an empty block, not a crash."""
        gen = DraftGenerator(llm=llm_echo, skills_root=skills_root)
        seed = _build_seed(source_fragments=("missing",))
        assert gen.gather_source_material(seed, vault_path=vault) == ""


# ---- generate_draft ---------------------------------------------------


class TestGenerateDraft:
    """``generate_draft`` composes a prompt and calls the LLM."""

    def test_returns_populated_draft(
        self,
        vault: Path,
        skills_root: Path,
    ) -> None:
        """The returned Draft carries provenance and the LLM's body."""
        fragment = _build_fragment()
        _write_fragment(vault, fragment, "A line.")
        _seed_skill_tree(skills_root, "frequencies/F1.SKILL.md")

        captured: dict[str, str] = {}

        def llm(prompt: str) -> str:
            captured["prompt"] = prompt
            return "Draft body."

        gen = DraftGenerator(llm=llm, skills_root=skills_root, voice_core="Core voice.")
        seed = _build_seed(
            source_fragments=(fragment.id,),
            frequency_affinity=(Frequency.F1,),
        )
        draft = gen.generate_draft(seed, vault_path=vault)
        assert draft.title == seed.title
        assert draft.body == "Draft body."
        assert draft.idea_strategy == seed.strategy.value
        assert draft.source_fragments == (fragment.id,)
        assert "frequencies/F1.SKILL.md" in draft.skill_stack
        assert "Core voice." in captured["prompt"]
        assert "A line." in captured["prompt"]
        assert seed.title in captured["prompt"]

    def test_raises_when_llm_returns_empty(
        self,
        vault: Path,
        skills_root: Path,
    ) -> None:
        """An empty LLM response is a hard error — better than silent bad drafts."""
        gen = DraftGenerator(llm=lambda _p: "   ", skills_root=skills_root)
        seed = _build_seed()
        with pytest.raises(RuntimeError, match="empty draft body"):
            gen.generate_draft(seed, vault_path=vault)

    def test_prompt_inlines_skill_file_contents(
        self,
        vault: Path,
        skills_root: Path,
    ) -> None:
        """The LLM prompt carries the SKILL.md body, not just the filename.

        Regression test for the bug where only skill names reached the LLM —
        the model cannot honour a skill it cannot read.
        """
        skill_body = "Frequency F1 guidance: stay close to sensory ground."
        freq_path = skills_root / "frequencies" / "F1.SKILL.md"
        freq_path.parent.mkdir(parents=True, exist_ok=True)
        freq_path.write_text(skill_body, encoding="utf-8")

        captured: dict[str, str] = {}

        def llm(prompt: str) -> str:
            captured["prompt"] = prompt
            return "Draft body."

        gen = DraftGenerator(llm=llm, skills_root=skills_root)
        seed = _build_seed(frequency_affinity=(Frequency.F1,))
        gen.generate_draft(seed, vault_path=vault)

        assert "## Activated skills" in captured["prompt"]
        assert "### F1.SKILL.md" in captured["prompt"]
        assert skill_body in captured["prompt"]

    def test_unreadable_skill_falls_back_to_name_only(
        self,
        vault: Path,
        skills_root: Path,
    ) -> None:
        """If a SKILL.md cannot be read, the prompt records the heading only.

        A skill path that resolves to a directory triggers ``IsADirectoryError``
        (an :class:`OSError`) inside :func:`_render_skill_section`; the
        fallback should still record the activation in the prompt.
        """
        # Make F1.SKILL.md a directory rather than a file.
        freq_path = skills_root / "frequencies" / "F1.SKILL.md"
        freq_path.mkdir(parents=True, exist_ok=True)

        captured: dict[str, str] = {}

        def llm(prompt: str) -> str:
            captured["prompt"] = prompt
            return "Draft body."

        gen = DraftGenerator(llm=llm, skills_root=skills_root)
        seed = _build_seed(frequency_affinity=(Frequency.F1,))
        gen.generate_draft(seed, vault_path=vault)

        assert "### F1.SKILL.md" in captured["prompt"]


# ---- save_draft -------------------------------------------------------


class TestSaveDraft:
    """``save_draft`` persists drafts with full provenance frontmatter."""

    def test_writes_to_drafts_subdir_with_date_slug_filename(
        self,
        vault: Path,
        skills_root: Path,
        llm_echo: Callable[[str], str],
    ) -> None:
        """Files land at ``07-Voice/Drafts/YYYY-MM-DD-{slug}.md``."""
        gen = DraftGenerator(llm=llm_echo, skills_root=skills_root)
        draft = Draft(
            title="What the creek teaches!",
            body="Body text.",
            idea_strategy=MiningStrategy.THREAD_TERMINUS.value,
            source_fragments=("frag-A",),
            threads=("thread-A",),
            eddies=(),
            skill_stack=("frequencies/F1.SKILL.md",),
            prompt="full prompt",
            generated_date=datetime(2026, 4, 20, tzinfo=UTC),
        )
        path = gen.save_draft(draft, vault)
        assert path.parent == vault / DRAFTS_SUBDIR
        assert path.name == "2026-04-20-what-the-creek-teaches.md"

    def test_frontmatter_carries_provenance(
        self,
        vault: Path,
        skills_root: Path,
        llm_echo: Callable[[str], str],
    ) -> None:
        """Frontmatter stores strategy, IDs, skills, prompt, and dates."""
        gen = DraftGenerator(llm=llm_echo, skills_root=skills_root)
        draft = Draft(
            title="Essay on listening",
            body="Body.",
            idea_strategy=MiningStrategy.RESONANCE_CHAIN.value,
            source_fragments=("frag-A", "frag-B"),
            threads=("thread-A",),
            eddies=("eddy-A",),
            skill_stack=(
                "frequencies/F6.SKILL.md",
                "registers/confessional.SKILL.md",
            ),
            prompt="full prompt",
            generated_date=datetime(2026, 4, 20, 15, 30, tzinfo=UTC),
        )
        path = gen.save_draft(draft, vault)
        post = frontmatter.load(str(path))
        meta = post.metadata
        assert meta["type"] == "draft"
        assert meta["status"] == "draft"
        assert meta["idea_strategy"] == "resonance_chain"
        assert meta["source_fragments"] == ["frag-A", "frag-B"]
        assert meta["threads"] == ["thread-A"]
        assert meta["eddies"] == ["eddy-A"]
        assert meta["activated_skills"] == [
            "frequencies/F6.SKILL.md",
            "registers/confessional.SKILL.md",
        ]
        assert meta["prompt"] == "full prompt"
        assert str(meta["generated_date"]).startswith("2026-04-20")

    def test_body_is_written_as_markdown(
        self,
        vault: Path,
        skills_root: Path,
        llm_echo: Callable[[str], str],
    ) -> None:
        """The draft body is preserved verbatim as the markdown content."""
        gen = DraftGenerator(llm=llm_echo, skills_root=skills_root)
        body = "## Section one\n\nThe creek speaks."
        draft = Draft(
            title="tt",
            body=body,
            idea_strategy=MiningStrategy.WAVELENGTH_WINDOW.value,
            source_fragments=(),
            threads=(),
            eddies=(),
            skill_stack=(),
            prompt="p",
            generated_date=datetime(2026, 4, 20, tzinfo=UTC),
        )
        path = gen.save_draft(draft, vault)
        post = frontmatter.load(str(path))
        assert post.content.strip() == body.strip()

    def test_same_day_same_title_does_not_overwrite(
        self,
        vault: Path,
        skills_root: Path,
        llm_echo: Callable[[str], str],
    ) -> None:
        """Two drafts with the same date+slug write to distinct files."""
        gen = DraftGenerator(llm=llm_echo, skills_root=skills_root)

        def _mk(body: str) -> Draft:
            return Draft(
                title="Same title",
                body=body,
                idea_strategy=MiningStrategy.THREAD_TERMINUS.value,
                source_fragments=(),
                threads=(),
                eddies=(),
                skill_stack=(),
                prompt="p",
                generated_date=datetime(2026, 4, 20, tzinfo=UTC),
            )

        first = gen.save_draft(_mk("First."), vault)
        second = gen.save_draft(_mk("Second."), vault)
        third = gen.save_draft(_mk("Third."), vault)

        assert first != second != third
        assert first.name == "2026-04-20-same-title.md"
        assert second.name == "2026-04-20-same-title-2.md"
        assert third.name == "2026-04-20-same-title-3.md"
        assert frontmatter.load(str(first)).content.strip() == "First."
        assert frontmatter.load(str(second)).content.strip() == "Second."
        assert frontmatter.load(str(third)).content.strip() == "Third."


# ---- FEAT-004 compiled-layer routing ---------------------------------


class TestGatherSourceMaterialCompileFirst:
    """``gather_source_material`` reads the compiled layer before fragments."""

    def test_compiled_thread_body_is_used_when_present(
        self,
        vault: Path,
        skills_root: Path,
        llm_echo: Callable[[str], str],
    ) -> None:
        """The compiled thread synthesis text replaces the description."""
        thread = Thread(
            id="thread-A",
            title="Listening practice",
            status=ThreadStatus.ACTIVE,
            first_seen=date(2026, 1, 1),
            last_seen=date(2026, 4, 1),
            description="Bare frontmatter description.",
        )
        _write_thread(vault, thread)
        _write_compiled_thread_page(
            vault,
            target_id="thread-A",
            title="Listening practice",
            body="# Listening practice\nThe synthesised body says listening matters.\n",
        )
        gen = DraftGenerator(llm=llm_echo, skills_root=skills_root)
        seed = _build_seed(threads=("thread-A",))

        block = gen.gather_source_material(seed, vault_path=vault)

        assert "synthesised body says listening matters" in block
        assert "Bare frontmatter description" not in block
        gaps_log = vault / "00-Creek-Meta/Processing-Log/compile-gaps.jsonl"
        assert not gaps_log.exists()

    def test_missing_compiled_thread_falls_back_and_logs_gap(
        self,
        vault: Path,
        skills_root: Path,
        llm_echo: Callable[[str], str],
    ) -> None:
        """Without a compiled page, frontmatter description appears + gap entry."""
        thread = Thread(
            id="thread-A",
            title="Listening practice",
            status=ThreadStatus.ACTIVE,
            first_seen=date(2026, 1, 1),
            last_seen=date(2026, 4, 1),
            description="Frontmatter fallback text.",
        )
        _write_thread(vault, thread)
        gen = DraftGenerator(llm=llm_echo, skills_root=skills_root)
        seed = _build_seed(threads=("thread-A",))

        block = gen.gather_source_material(seed, vault_path=vault)

        assert "Frontmatter fallback text" in block
        gaps_log = vault / "00-Creek-Meta/Processing-Log/compile-gaps.jsonl"
        assert gaps_log.exists()
        contents = gaps_log.read_text(encoding="utf-8")
        assert "thread-A" in contents
        assert '"surfaced_by": "draft"' in contents

    def test_compiled_eddy_body_replaces_description(
        self,
        vault: Path,
        skills_root: Path,
        llm_echo: Callable[[str], str],
    ) -> None:
        """Same compile-first behaviour for eddies."""
        eddy = Eddy(
            id="eddy-A",
            title="Water as teacher",
            formed=date(2026, 1, 1),
            description="Stale frontmatter description.",
        )
        _write_eddy(vault, eddy)
        _write_compiled_eddy_page(
            vault,
            target_id="eddy-A",
            title="Water as teacher",
            body="# Water as teacher\nWater is the teacher we keep returning to.\n",
        )
        gen = DraftGenerator(llm=llm_echo, skills_root=skills_root)
        seed = _build_seed(eddies=("eddy-A",))

        block = gen.gather_source_material(seed, vault_path=vault)

        assert "Water is the teacher" in block
        assert "Stale frontmatter description" not in block

    def test_bypass_compiled_pulls_descriptions_and_skips_log(
        self,
        vault: Path,
        skills_root: Path,
        llm_echo: Callable[[str], str],
    ) -> None:
        """``bypass_compiled=True`` reads frontmatter only and never logs gaps."""
        thread = Thread(
            id="thread-A",
            title="Listening practice",
            status=ThreadStatus.ACTIVE,
            first_seen=date(2026, 1, 1),
            last_seen=date(2026, 4, 1),
            description="Frontmatter description.",
        )
        _write_thread(vault, thread)
        _write_compiled_thread_page(
            vault,
            target_id="thread-A",
            title="Listening practice",
            body="# Listening practice\nCompiled body that bypass should NOT see.\n",
        )
        gen = DraftGenerator(
            llm=llm_echo,
            skills_root=skills_root,
            bypass_compiled=True,
        )
        seed = _build_seed(threads=("thread-A",))

        block = gen.gather_source_material(seed, vault_path=vault)

        assert "Frontmatter description" in block
        assert "bypass should NOT see" not in block
        gaps_log = vault / "00-Creek-Meta/Processing-Log/compile-gaps.jsonl"
        assert not gaps_log.exists()


class TestRenderCompiledEntry:
    """Pin the heading-strip heuristic in :func:`_render_compiled_entry`."""

    def test_strips_matching_title_header(self) -> None:
        """A leading ``# {title}`` line that matches ``page.title`` is stripped."""
        from creek.generate.drafts import _render_compiled_entry
        from creek.models import CompiledPage

        page = CompiledPage(
            target_kind="thread",
            target_id="thread-A",
            title="Listening practice",
            body="# Listening practice\nThe body proper begins here.\n",
        )

        rendered = _render_compiled_entry("thread-A", page)

        expected = "### thread-A: Listening practice\nThe body proper begins here."
        assert rendered == expected

    def test_preserves_non_matching_first_line(self) -> None:
        """A non-title first line is kept verbatim."""
        from creek.generate.drafts import _render_compiled_entry
        from creek.models import CompiledPage

        page = CompiledPage(
            target_kind="eddy",
            target_id="eddy-B",
            title="Water as teacher",
            body="A reflective lede paragraph.\nFollowed by more.\n",
        )

        rendered = _render_compiled_entry("eddy-B", page)

        expected = (
            "### eddy-B: Water as teacher\n"
            "A reflective lede paragraph.\n"
            "Followed by more."
        )
        assert rendered == expected

    def test_empty_body_after_strip_uses_placeholder(self) -> None:
        """When the body collapses to nothing, the fallback placeholder appears."""
        from creek.generate.drafts import _render_compiled_entry
        from creek.models import CompiledPage

        page = CompiledPage(
            target_kind="thread",
            target_id="thread-empty",
            title="Empty",
            body="# Empty\n",
        )

        rendered = _render_compiled_entry("thread-empty", page)

        assert rendered == "### thread-empty: Empty\n(compiled page has no body)"


class TestComposeSectionLazyLoad:
    """Full compiled-cache hits skip the frontmatter directory scan."""

    def test_compose_thread_section_skips_load_when_all_hits(
        self,
        vault: Path,
        skills_root: Path,
        llm_echo: Callable[[str], str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A fully-compiled vault never reads ``02-Threads`` frontmatter."""
        from creek.generate import drafts as drafts_module

        _write_compiled_thread_page(
            vault,
            target_id="thread-A",
            title="Listening practice",
            body="# Listening practice\nCompiled body.\n",
        )

        scans: list[Path] = []

        def _spy(root: Path) -> dict[str, Thread]:
            scans.append(root)
            return {}

        monkeypatch.setattr(drafts_module, "_load_threads_by_id", _spy)

        gen = DraftGenerator(llm=llm_echo, skills_root=skills_root)
        seed = _build_seed(threads=("thread-A",))

        block = gen.gather_source_material(seed, vault_path=vault)

        assert "Compiled body" in block
        assert scans == []

    def test_compose_thread_section_loads_on_first_miss(
        self,
        vault: Path,
        skills_root: Path,
        llm_echo: Callable[[str], str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A single missing ID triggers exactly one frontmatter scan, reused."""
        from creek.generate import drafts as drafts_module

        _write_compiled_thread_page(
            vault,
            target_id="thread-A",
            title="Listening practice",
            body="# Listening practice\nCompiled body.\n",
        )

        scans: list[Path] = []
        original = drafts_module._load_threads_by_id

        def _spy(root: Path) -> dict[str, Thread]:
            scans.append(root)
            return original(root)

        monkeypatch.setattr(drafts_module, "_load_threads_by_id", _spy)

        gen = DraftGenerator(llm=llm_echo, skills_root=skills_root)
        seed = _build_seed(threads=("thread-A", "thread-missing-1", "thread-missing-2"))

        gen.gather_source_material(seed, vault_path=vault)

        assert len(scans) == 1

    def test_compose_eddy_section_skips_load_when_all_hits(
        self,
        vault: Path,
        skills_root: Path,
        llm_echo: Callable[[str], str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A fully-compiled vault never reads ``03-Eddies`` frontmatter."""
        from creek.generate import drafts as drafts_module

        _write_compiled_eddy_page(
            vault,
            target_id="eddy-A",
            title="Water as teacher",
            body="# Water as teacher\nWater is the teacher.\n",
        )

        scans: list[Path] = []

        def _spy(root: Path) -> dict[str, Eddy]:
            scans.append(root)
            return {}

        monkeypatch.setattr(drafts_module, "_load_eddies_by_id", _spy)

        gen = DraftGenerator(llm=llm_echo, skills_root=skills_root)
        seed = _build_seed(eddies=("eddy-A",))

        block = gen.gather_source_material(seed, vault_path=vault)

        assert "Water is the teacher" in block
        assert scans == []

    def test_compose_eddy_section_loads_on_first_miss(
        self,
        vault: Path,
        skills_root: Path,
        llm_echo: Callable[[str], str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A single missing eddy ID triggers exactly one frontmatter scan, reused."""
        from creek.generate import drafts as drafts_module

        _write_compiled_eddy_page(
            vault,
            target_id="eddy-A",
            title="Water as teacher",
            body="# Water as teacher\nWater is the teacher.\n",
        )

        scans: list[Path] = []
        original = drafts_module._load_eddies_by_id

        def _spy(root: Path) -> dict[str, Eddy]:
            scans.append(root)
            return original(root)

        monkeypatch.setattr(drafts_module, "_load_eddies_by_id", _spy)

        gen = DraftGenerator(llm=llm_echo, skills_root=skills_root)
        seed = _build_seed(eddies=("eddy-A", "eddy-missing-1", "eddy-missing-2"))

        gen.gather_source_material(seed, vault_path=vault)

        assert len(scans) == 1


# ---- FEAT-032 manual seeding -----------------------------------------


class TestSeedSpec:
    """``SeedSpec`` enforces the FEAT-032 mutual-exclusion rule on construction."""

    def test_empty_spec_reports_is_empty(self) -> None:
        """A spec with no fields set is reported as empty."""
        assert SeedSpec().is_empty is True

    def test_populated_spec_reports_not_empty(self) -> None:
        """Any populated field flips ``is_empty`` to ``False``."""
        assert SeedSpec(topic="listening").is_empty is False
        assert SeedSpec(frequencies=(Frequency.F1,)).is_empty is False
        assert SeedSpec(fragment_id="frag-A").is_empty is False

    def test_dimensional_filter_detection(self) -> None:
        """Topic alone does NOT count as a dimensional filter."""
        assert SeedSpec(topic="x").has_dimensional_filter is False
        assert SeedSpec(frequencies=(Frequency.F1,)).has_dimensional_filter is True
        assert SeedSpec(phases=(Phase.RISING,)).has_dimensional_filter is True
        assert SeedSpec(modes=(Mode.INHABIT,)).has_dimensional_filter is True

    def test_fragment_id_blocks_topic(self) -> None:
        """``--seed-fragment`` cannot combine with ``--seed-topic``."""
        with pytest.raises(ValueError, match="mutually exclusive"):
            SeedSpec(fragment_id="frag-A", topic="x")

    def test_fragment_id_blocks_dimensional_filters(self) -> None:
        """``--seed-fragment`` cannot combine with frequency/phase/mode filters."""
        with pytest.raises(ValueError, match="mutually exclusive"):
            SeedSpec(fragment_id="frag-A", frequencies=(Frequency.F1,))
        with pytest.raises(ValueError, match="mutually exclusive"):
            SeedSpec(fragment_id="frag-A", phases=(Phase.RISING,))
        with pytest.raises(ValueError, match="mutually exclusive"):
            SeedSpec(fragment_id="frag-A", modes=(Mode.INHABIT,))

    def test_empty_topic_is_normalised_to_none(self) -> None:
        """An empty / whitespace-only ``topic`` is treated as unset."""
        assert SeedSpec(topic="").topic is None
        assert SeedSpec(topic="   ").topic is None
        assert SeedSpec(topic="").is_empty is True
        assert SeedSpec(topic="\t\n").is_empty is True

    def test_whitespace_topic_with_filter_keeps_filter(self) -> None:
        """A whitespace topic alongside a real filter is silently dropped."""
        spec = SeedSpec(topic=" ", frequencies=(Frequency.F1,))
        assert spec.topic is None
        assert spec.frequencies == (Frequency.F1,)
        assert spec.is_empty is False


class TestResolveSeedFragment:
    """``resolve_seed`` with ``--seed-fragment`` yields a single-source IdeaSeed."""

    def test_returns_seed_keyed_to_named_fragment(
        self,
        vault: Path,
        skills_root: Path,
        llm_echo: Callable[[str], str],
    ) -> None:
        """The synthetic seed carries the fragment's id, title, and frequency."""
        fragment = _build_fragment(
            frag_id="frag-keep",
            title="Naming what orbits",
            primary=Frequency.F6,
            threads=("thread-A",),
            eddies=("eddy-Z",),
        )
        _write_fragment(vault, fragment, "Body text.")
        gen = DraftGenerator(llm=llm_echo, skills_root=skills_root)

        idea = gen.resolve_seed(SeedSpec(fragment_id="frag-keep"), vault_path=vault)

        assert idea.title == "Naming what orbits"
        assert idea.source_fragments == ("frag-keep",)
        assert idea.threads == ("thread-A",)
        assert idea.eddies == ("eddy-Z",)
        assert idea.frequency_affinity == (Frequency.F6,)

    def test_unknown_fragment_id_raises(
        self,
        vault: Path,
        skills_root: Path,
        llm_echo: Callable[[str], str],
    ) -> None:
        """An ID not present in the vault is a hard error."""
        gen = DraftGenerator(llm=llm_echo, skills_root=skills_root)
        with pytest.raises(SeedResolutionError, match="not found"):
            gen.resolve_seed(SeedSpec(fragment_id="missing"), vault_path=vault)

    def test_unclassified_frequency_yields_empty_affinity(
        self,
        vault: Path,
        skills_root: Path,
        llm_echo: Callable[[str], str],
    ) -> None:
        """Following up on an unclassified fragment leaves frequency_affinity empty."""
        fragment = _build_fragment(
            frag_id="frag-unc",
            primary=Frequency.UNCLASSIFIED,
        )
        _write_fragment(vault, fragment, "Body.")
        gen = DraftGenerator(llm=llm_echo, skills_root=skills_root)
        idea = gen.resolve_seed(SeedSpec(fragment_id="frag-unc"), vault_path=vault)
        assert idea.frequency_affinity == ()


class TestResolveSeedDimensional:
    """``resolve_seed`` with dimensional filters selects matching fragments."""

    def test_filters_by_frequency_only(
        self,
        vault: Path,
        skills_root: Path,
        llm_echo: Callable[[str], str],
    ) -> None:
        """Only fragments whose primary frequency matches survive the filter."""
        _write_fragment(
            vault,
            _build_fragment(frag_id="frag-A", primary=Frequency.F1),
            "agency body",
        )
        _write_fragment(
            vault,
            _build_fragment(frag_id="frag-B", primary=Frequency.F6),
            "pluralism body",
        )
        gen = DraftGenerator(llm=llm_echo, skills_root=skills_root)
        idea = gen.resolve_seed(
            SeedSpec(frequencies=(Frequency.F6,)),
            vault_path=vault,
        )
        assert idea.source_fragments == ("frag-B",)
        assert idea.frequency_affinity == (Frequency.F6,)

    def test_filters_by_phase_and_mode_union(
        self,
        vault: Path,
        skills_root: Path,
        llm_echo: Callable[[str], str],
    ) -> None:
        """Phase and mode OR together (issue #351): the union of slices survives.

        Previously these dimensions ANDed and only the intersection
        survived; per #351 each dimension fetches its own corpus slice
        independently and the slices union. A fragment matching either
        phase or mode now appears in the seed.
        """
        _write_fragment(
            vault,
            _build_fragment(
                frag_id="frag-A",
                phase=Phase.RISING,
                mode=Mode.INHABIT,
            ),
            "body",
        )
        _write_fragment(
            vault,
            _build_fragment(
                frag_id="frag-B",
                phase=Phase.RISING,
                mode=Mode.EXPRESS,
            ),
            "body",
        )
        _write_fragment(
            vault,
            _build_fragment(
                frag_id="frag-C",
                phase=Phase.PEAKING,
                mode=Mode.INHABIT,
            ),
            "body",
        )
        gen = DraftGenerator(llm=llm_echo, skills_root=skills_root)
        idea = gen.resolve_seed(
            SeedSpec(phases=(Phase.RISING,), modes=(Mode.INHABIT,)),
            vault_path=vault,
        )
        # All three fragments match at least one of the active dimensions.
        assert set(idea.source_fragments) == {"frag-A", "frag-B", "frag-C"}

    def test_zero_matches_raises_honest_error(
        self,
        vault: Path,
        skills_root: Path,
        llm_echo: Callable[[str], str],
    ) -> None:
        """No silent fallback when every dimension's slice is empty (issue #351).

        The wording shifted from "No source material matches ..." to
        "No source material in any attempted dimension: ..." so the
        operator sees which dimensions were attempted and which to
        widen, per the #351 acceptance criterion.
        """
        _write_fragment(
            vault,
            _build_fragment(frag_id="frag-A", primary=Frequency.F1),
            "body",
        )
        gen = DraftGenerator(llm=llm_echo, skills_root=skills_root)
        with pytest.raises(
            SeedResolutionError,
            match="No source material in any attempted dimension",
        ):
            gen.resolve_seed(
                SeedSpec(frequencies=(Frequency.F10,)),
                vault_path=vault,
            )

    def test_empty_spec_raises(
        self,
        vault: Path,
        skills_root: Path,
        llm_echo: Callable[[str], str],
    ) -> None:
        """An empty spec is rejected — the caller must request *something*."""
        gen = DraftGenerator(llm=llm_echo, skills_root=skills_root)
        with pytest.raises(SeedResolutionError, match="empty"):
            gen.resolve_seed(SeedSpec(), vault_path=vault)

    def test_topic_filter_combines_with_frequency(
        self,
        vault: Path,
        skills_root: Path,
        llm_echo: Callable[[str], str],
    ) -> None:
        """``--seed-topic`` plus ``--seed-frequency`` ANDs both filters."""
        _write_fragment(
            vault,
            _build_fragment(
                frag_id="frag-A",
                title="Belonging at the edge",
                primary=Frequency.F2,
            ),
            "On belonging to a place.",
        )
        _write_fragment(
            vault,
            _build_fragment(
                frag_id="frag-B",
                title="Solo travel",
                primary=Frequency.F2,
            ),
            "On going alone.",
        )
        gen = DraftGenerator(llm=llm_echo, skills_root=skills_root)
        idea = gen.resolve_seed(
            SeedSpec(topic="belonging", frequencies=(Frequency.F2,)),
            vault_path=vault,
        )
        assert idea.source_fragments == ("frag-A",)
        assert idea.title == "belonging"

    def test_topic_alone_does_substring_match(
        self,
        vault: Path,
        skills_root: Path,
        llm_echo: Callable[[str], str],
    ) -> None:
        """``--seed-topic`` alone matches title or body substrings."""
        _write_fragment(
            vault,
            _build_fragment(frag_id="frag-A", title="A walk by the creek"),
            "Body unrelated.",
        )
        _write_fragment(
            vault,
            _build_fragment(frag_id="frag-B", title="Anything"),
            "An essay on belonging in community.",
        )
        gen = DraftGenerator(llm=llm_echo, skills_root=skills_root)
        idea = gen.resolve_seed(SeedSpec(topic="belonging"), vault_path=vault)
        assert idea.source_fragments == ("frag-B",)
        assert idea.title == "belonging"

    def test_topic_matching_is_case_insensitive_across_body(
        self,
        vault: Path,
        skills_root: Path,
        llm_echo: Callable[[str], str],
    ) -> None:
        """Mixed-case body text still matches a lowercase topic needle."""
        _write_fragment(
            vault,
            _build_fragment(frag_id="frag-A", title="Untitled"),
            "Belonging is the name of the longing we carry.",
        )
        _write_fragment(
            vault,
            _build_fragment(frag_id="frag-B", title="UNRELATED"),
            "Nothing matching here.",
        )
        gen = DraftGenerator(llm=llm_echo, skills_root=skills_root)
        idea = gen.resolve_seed(SeedSpec(topic="belonging"), vault_path=vault)
        assert idea.source_fragments == ("frag-A",)

    def test_dimensional_only_title_describes_corner(
        self,
        vault: Path,
        skills_root: Path,
        llm_echo: Callable[[str], str],
    ) -> None:
        """Pure dimensional seeding produces an "From the X corner" title."""
        _write_fragment(
            vault,
            _build_fragment(
                frag_id="frag-A",
                primary=Frequency.F1,
                phase=Phase.RISING,
                mode=Mode.INTEGRATE,
            ),
            "body",
        )
        gen = DraftGenerator(llm=llm_echo, skills_root=skills_root)
        idea = gen.resolve_seed(
            SeedSpec(
                frequencies=(Frequency.F1,),
                phases=(Phase.RISING,),
                modes=(Mode.INTEGRATE,),
            ),
            vault_path=vault,
        )
        assert "From the" in idea.title
        assert "F1" in idea.title
        assert "rising" in idea.title
        assert "integrate" in idea.title

    def test_threads_and_eddies_unioned_across_matches(
        self,
        vault: Path,
        skills_root: Path,
        llm_echo: Callable[[str], str],
    ) -> None:
        """Synthetic IdeaSeed gathers thread/eddy IDs from all matched fragments."""
        _write_fragment(
            vault,
            _build_fragment(
                frag_id="frag-A",
                primary=Frequency.F1,
                threads=("thread-1",),
                eddies=("eddy-1",),
            ),
            "body",
        )
        _write_fragment(
            vault,
            _build_fragment(
                frag_id="frag-B",
                primary=Frequency.F1,
                threads=("thread-2", "thread-1"),
                eddies=("eddy-2",),
            ),
            "body",
        )
        gen = DraftGenerator(llm=llm_echo, skills_root=skills_root)
        idea = gen.resolve_seed(
            SeedSpec(frequencies=(Frequency.F1,)),
            vault_path=vault,
        )
        assert set(idea.threads) == {"thread-1", "thread-2"}
        assert set(idea.eddies) == {"eddy-1", "eddy-2"}


class TestGenerateSeededDraft:
    """``generate_seeded_draft`` runs the full pipeline from a SeedSpec."""

    def test_returns_draft_with_seed_provenance(
        self,
        vault: Path,
        skills_root: Path,
    ) -> None:
        """The returned Draft carries the originating ``SeedSpec``."""
        fragment = _build_fragment(frag_id="frag-A", primary=Frequency.F6)
        _write_fragment(vault, fragment, "A note on listening to others.")
        _seed_skill_tree(skills_root, "frequencies/F6.SKILL.md")

        captured: dict[str, str] = {}

        def llm(prompt: str) -> str:
            captured["prompt"] = prompt
            return "Generated body."

        gen = DraftGenerator(llm=llm, skills_root=skills_root)
        spec = SeedSpec(topic="listening", frequencies=(Frequency.F6,))
        draft = gen.generate_seeded_draft(spec, vault_path=vault)

        assert draft.seed_spec == spec
        assert draft.source_fragments == ("frag-A",)
        assert "frequencies/F6.SKILL.md" in draft.skill_stack
        assert "listening" in draft.prompt.lower()

    def test_seed_phase_drives_phase_skill_when_no_override(
        self,
        vault: Path,
        skills_root: Path,
    ) -> None:
        """``--seed-phase`` selects the phase skill when no explicit phase is passed."""
        fragment = _build_fragment(
            frag_id="frag-A",
            primary=Frequency.F1,
            phase=Phase.RISING,
        )
        _write_fragment(vault, fragment, "Body.")
        _seed_skill_tree(skills_root, "phases/rising.SKILL.md")

        gen = DraftGenerator(llm=lambda _p: "Body.", skills_root=skills_root)
        spec = SeedSpec(phases=(Phase.RISING,))
        draft = gen.generate_seeded_draft(spec, vault_path=vault)

        assert "phases/rising.SKILL.md" in draft.skill_stack

    def test_explicit_current_phase_overrides_spec(
        self,
        vault: Path,
        skills_root: Path,
    ) -> None:
        """Caller-supplied ``current_phase`` wins over the spec's phase."""
        fragment = _build_fragment(
            frag_id="frag-A",
            primary=Frequency.F1,
            phase=Phase.RISING,
        )
        _write_fragment(vault, fragment, "Body.")
        _seed_skill_tree(skills_root, "phases/peaking.SKILL.md")

        gen = DraftGenerator(llm=lambda _p: "Body.", skills_root=skills_root)
        spec = SeedSpec(phases=(Phase.RISING,))
        draft = gen.generate_seeded_draft(
            spec,
            vault_path=vault,
            current_phase=Phase.PEAKING,
        )

        assert "phases/peaking.SKILL.md" in draft.skill_stack

    def test_zero_matches_propagates_resolution_error(
        self,
        vault: Path,
        skills_root: Path,
    ) -> None:
        """The error from ``resolve_seed`` is propagated to the caller."""
        gen = DraftGenerator(llm=lambda _p: "Body.", skills_root=skills_root)
        spec = SeedSpec(frequencies=(Frequency.F10,))
        with pytest.raises(SeedResolutionError):
            gen.generate_seeded_draft(spec, vault_path=vault)

    def test_empty_llm_response_raises(
        self,
        vault: Path,
        skills_root: Path,
    ) -> None:
        """An empty LLM body is rejected (parity with ``generate_draft``)."""
        fragment = _build_fragment(frag_id="frag-A", primary=Frequency.F1)
        _write_fragment(vault, fragment, "Body.")
        gen = DraftGenerator(llm=lambda _p: "   ", skills_root=skills_root)
        spec = SeedSpec(frequencies=(Frequency.F1,))
        with pytest.raises(RuntimeError, match="empty draft body"):
            gen.generate_seeded_draft(spec, vault_path=vault)


class TestSaveDraftWithSeedSpec:
    """``save_draft`` writes ``seed:`` provenance to frontmatter when present."""

    def _make_draft(self, *, seed_spec: SeedSpec | None) -> Draft:
        """Return a Draft instance for save_draft frontmatter tests."""
        return Draft(
            title="Seeded essay",
            body="Body.",
            idea_strategy=MiningStrategy.RESONANCE_CHAIN.value,
            source_fragments=("frag-A",),
            threads=(),
            eddies=(),
            skill_stack=(),
            prompt="prompt",
            generated_date=datetime(2026, 5, 1, tzinfo=UTC),
            seed_spec=seed_spec,
        )

    def test_seed_field_omitted_for_mined_drafts(
        self,
        vault: Path,
        skills_root: Path,
        llm_echo: Callable[[str], str],
    ) -> None:
        """A Draft without ``seed_spec`` carries no ``seed:`` frontmatter."""
        gen = DraftGenerator(llm=llm_echo, skills_root=skills_root)
        path = gen.save_draft(self._make_draft(seed_spec=None), vault)
        post = frontmatter.load(str(path))
        assert "seed" not in post.metadata

    def test_seed_field_records_all_populated_flags(
        self,
        vault: Path,
        skills_root: Path,
        llm_echo: Callable[[str], str],
    ) -> None:
        """``seed:`` frontmatter mirrors every populated SeedSpec field."""
        gen = DraftGenerator(llm=llm_echo, skills_root=skills_root)
        spec = SeedSpec(
            topic="belonging",
            frequencies=(Frequency.F2,),
            phases=(Phase.RESTORATION,),
            modes=(Mode.INHABIT,),
        )
        path = gen.save_draft(self._make_draft(seed_spec=spec), vault)
        post = frontmatter.load(str(path))
        seed_meta = post.metadata["seed"]
        assert seed_meta == {
            "topic": "belonging",
            "frequencies": ["F2"],
            "phases": ["restoration"],
            "modes": ["inhabit"],
        }

    def test_seed_field_omits_unset_fields(
        self,
        vault: Path,
        skills_root: Path,
        llm_echo: Callable[[str], str],
    ) -> None:
        """Only populated fields land in the ``seed:`` mapping."""
        gen = DraftGenerator(llm=llm_echo, skills_root=skills_root)
        spec = SeedSpec(fragment_id="frag-keep")
        path = gen.save_draft(self._make_draft(seed_spec=spec), vault)
        post = frontmatter.load(str(path))
        assert post.metadata["seed"] == {"fragment_id": "frag-keep"}


# ---- Issue #351: AND→OR per-dimension retrieval + composition-time weaving --


class TestPerDimensionRetrievalAcceptance:
    """Acceptance criteria from issue #351 (load-bearing change).

    These tests assert the four bulleted criteria in the issue body:
    multi-dimension success on a vault where no single fragment
    matches all dimensions, per-dimension frontmatter attribution,
    per-dimension warning for one empty dimension while proceeding,
    and an all-empty diagnostic.
    """

    def test_multi_dim_succeeds_when_no_single_fragment_matches_all(
        self,
        vault: Path,
        skills_root: Path,
    ) -> None:
        """``--seed-phase`` + ``--seed-mode`` together succeed via OR semantics.

        Acceptance criterion 1: the prior AND-intersection failure mode
        is gone. Each dimension contributes its own slice; the union
        survives even when no single fragment matches both filters.
        """
        # frag-A matches the phase only; frag-B matches the mode only.
        # Under legacy AND, the corpus was empty and drafting failed.
        _write_fragment(
            vault,
            _build_fragment(
                frag_id="frag-A",
                phase=Phase.PEAKING,
                mode=Mode.INHABIT,
            ),
            "Peaking body about agency.",
        )
        _write_fragment(
            vault,
            _build_fragment(
                frag_id="frag-B",
                phase=Phase.RISING,
                mode=Mode.EXPRESS,
            ),
            "Expressive body about voice.",
        )
        gen = DraftGenerator(
            llm=lambda _p: "Drafted body.",
            skills_root=skills_root,
        )
        # Multiple dimensions, no topic filter: each dimension contributes
        # its slice independently and the slices union.
        spec = SeedSpec(
            phases=(Phase.PEAKING,),
            modes=(Mode.EXPRESS,),
        )

        draft = gen.generate_seeded_draft(spec, vault_path=vault)

        # Both fragments enter the draft; previously the AND would have
        # yielded zero matches and raised SeedResolutionError.
        assert set(draft.source_fragments) == {"frag-A", "frag-B"}

    def test_frontmatter_records_per_dimension_attribution(
        self,
        vault: Path,
        skills_root: Path,
    ) -> None:
        """Acceptance criterion 2: per-dimension fragment lineage in frontmatter."""
        _write_fragment(
            vault,
            _build_fragment(
                frag_id="frag-A",
                phase=Phase.PEAKING,
                mode=Mode.INHABIT,
            ),
            "A body.",
        )
        _write_fragment(
            vault,
            _build_fragment(
                frag_id="frag-B",
                phase=Phase.RISING,
                mode=Mode.EXPRESS,
            ),
            "B body.",
        )
        gen = DraftGenerator(
            llm=lambda _p: "Drafted body.",
            skills_root=skills_root,
        )
        spec = SeedSpec(
            phases=(Phase.PEAKING,),
            modes=(Mode.EXPRESS,),
        )

        draft = gen.generate_seeded_draft(spec, vault_path=vault)
        path = gen.save_draft(draft, vault)

        post = frontmatter.load(str(path))
        per_dim = post.metadata["per_dimension_sources"]
        assert per_dim == {
            "phase:peaking": ["frag-A"],
            "mode:express": ["frag-B"],
        }

    def test_warns_on_empty_dimension_but_proceeds(
        self,
        vault: Path,
        skills_root: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Acceptance criterion 3: per-dimension warning + proceed.

        When one dimension has zero matches but others have matches,
        a warning names the empty dimension specifically and drafting
        continues.
        """
        # Only the phase dimension matches; the mode dimension is empty.
        _write_fragment(
            vault,
            _build_fragment(
                frag_id="frag-A",
                phase=Phase.PEAKING,
                mode=Mode.INHABIT,
            ),
            "Body.",
        )
        gen = DraftGenerator(
            llm=lambda _p: "Drafted body.",
            skills_root=skills_root,
        )
        spec = SeedSpec(
            phases=(Phase.PEAKING,),
            modes=(Mode.EXPRESS,),
        )

        with caplog.at_level("WARNING", logger="creek.generate.drafts"):
            draft = gen.generate_seeded_draft(spec, vault_path=vault)

        assert draft.source_fragments == ("frag-A",)
        # Warning names the express stance specifically.
        assert any("the express stance" in record.message for record in caplog.records)

    def test_all_empty_dimensions_raises_with_clear_diagnostic(
        self,
        vault: Path,
        skills_root: Path,
    ) -> None:
        """Acceptance criterion 4: all-empty raises naming every attempted dim."""
        _write_fragment(
            vault,
            _build_fragment(
                frag_id="frag-A",
                phase=Phase.RISING,
                mode=Mode.INHABIT,
            ),
            "Body.",
        )
        gen = DraftGenerator(
            llm=lambda _p: "Drafted body.",
            skills_root=skills_root,
        )
        spec = SeedSpec(
            phases=(Phase.PEAKING,),
            modes=(Mode.EXPRESS,),
        )

        with pytest.raises(SeedResolutionError) as exc:
            gen.generate_seeded_draft(spec, vault_path=vault)

        message = str(exc.value)
        assert "the peaking phase" in message
        assert "the express stance" in message

    def test_prompt_has_labelled_per_dimension_sections(
        self,
        vault: Path,
        skills_root: Path,
    ) -> None:
        """The LLM prompt exposes one labelled ``## Material for ...`` per dimension."""
        _write_fragment(
            vault,
            _build_fragment(
                frag_id="frag-A",
                phase=Phase.PEAKING,
                mode=Mode.INHABIT,
            ),
            "Peaking body.",
        )
        _write_fragment(
            vault,
            _build_fragment(
                frag_id="frag-B",
                phase=Phase.RISING,
                mode=Mode.EXPRESS,
            ),
            "Express body.",
        )

        captured: dict[str, str] = {}

        def llm(prompt: str) -> str:
            """Capture the prompt for assertion and return a fixed body."""
            captured["prompt"] = prompt
            return "Drafted body."

        gen = DraftGenerator(llm=llm, skills_root=skills_root)
        spec = SeedSpec(
            phases=(Phase.PEAKING,),
            modes=(Mode.EXPRESS,),
        )

        gen.generate_seeded_draft(spec, vault_path=vault)

        prompt = captured["prompt"]
        assert "## Material for the peaking phase" in prompt
        assert "## Material for the express stance" in prompt
        # The labelled blocks carry the actual fragment bodies.
        assert "Peaking body." in prompt
        assert "Express body." in prompt
        # The "weave them" hint replaces the legacy single-corpus ask.
        assert "weave them" in prompt.lower()

    def test_prompt_preserves_legacy_blocks_for_single_dimension(
        self,
        vault: Path,
        skills_root: Path,
    ) -> None:
        """Single-dimension specs flow through the per-dimension path too.

        Even one dimension activates per-dimension retrieval — the
        labelled section is the only fragment block, and the legacy
        ``## Source fragments`` heading does not appear (so we avoid
        rendering the same fragments twice).
        """
        _write_fragment(
            vault,
            _build_fragment(frag_id="frag-A", phase=Phase.PEAKING),
            "Peaking body.",
        )

        captured: dict[str, str] = {}

        def llm(prompt: str) -> str:
            """Capture the prompt for assertion and return a fixed body."""
            captured["prompt"] = prompt
            return "Drafted body."

        gen = DraftGenerator(llm=llm, skills_root=skills_root)
        spec = SeedSpec(phases=(Phase.PEAKING,))

        gen.generate_seeded_draft(spec, vault_path=vault)

        prompt = captured["prompt"]
        assert "## Material for the peaking phase" in prompt
        # No duplicate Source fragments block.
        assert "## Source fragments" not in prompt

    def test_topic_only_spec_still_uses_legacy_path(
        self,
        vault: Path,
        skills_root: Path,
    ) -> None:
        """A spec with only a topic (no dimensions) uses the legacy single block."""
        _write_fragment(
            vault,
            _build_fragment(frag_id="frag-A", title="On belonging"),
            "Body about belonging.",
        )

        captured: dict[str, str] = {}

        def llm(prompt: str) -> str:
            """Capture the prompt for assertion and return a fixed body."""
            captured["prompt"] = prompt
            return "Drafted body."

        gen = DraftGenerator(llm=llm, skills_root=skills_root)
        spec = SeedSpec(topic="belonging")

        draft = gen.generate_seeded_draft(spec, vault_path=vault)

        prompt = captured["prompt"]
        assert "## Source fragments" in prompt
        assert "## Material for the" not in prompt
        assert draft.per_dimension_sources == ()

    def test_ontology_drives_per_dimension_path(
        self,
        vault: Path,
        skills_root: Path,
    ) -> None:
        """A PromptOntology with above-threshold weights triggers per-dim retrieval."""
        from creek.classify.prompt import PromptOntology, WeightedDimension

        _write_fragment(
            vault,
            _build_fragment(frag_id="frag-A", phase=Phase.PEAKING),
            "A body.",
        )

        captured: dict[str, str] = {}

        def llm(prompt: str) -> str:
            """Capture the prompt for assertion and return a fixed body."""
            captured["prompt"] = prompt
            return "Drafted body."

        gen = DraftGenerator(llm=llm, skills_root=skills_root)
        ontology = PromptOntology(
            prompt="x",
            phases=(WeightedDimension(value=Phase.PEAKING, weight=0.7),),
        )
        spec = SeedSpec(ontology=ontology)

        draft = gen.generate_seeded_draft(spec, vault_path=vault)

        assert draft.source_fragments == ("frag-A",)
        assert "## Material for the peaking phase (weight 0.70)" in captured["prompt"]

    def test_per_dimension_sources_omitted_for_legacy_path(
        self,
        vault: Path,
        skills_root: Path,
        llm_echo: Callable[[str], str],
    ) -> None:
        """Frontmatter omits ``per_dimension_sources`` for legacy single-block path."""
        gen = DraftGenerator(llm=llm_echo, skills_root=skills_root)
        draft = Draft(
            title="Topic only",
            body="Body.",
            idea_strategy=MiningStrategy.RESONANCE_CHAIN.value,
            source_fragments=(),
            threads=(),
            eddies=(),
            skill_stack=(),
            prompt="prompt",
            generated_date=datetime(2026, 5, 27, tzinfo=UTC),
        )
        path = gen.save_draft(draft, vault)
        post = frontmatter.load(str(path))
        assert "per_dimension_sources" not in post.metadata

    def test_ontology_below_threshold_dropped(
        self,
        vault: Path,
        skills_root: Path,
    ) -> None:
        """Ontology entries below the spec's confidence threshold are filtered out."""
        from creek.classify.prompt import PromptOntology, WeightedDimension

        _write_fragment(
            vault,
            _build_fragment(frag_id="frag-A", phase=Phase.PEAKING),
            "A body.",
        )
        _write_fragment(
            vault,
            _build_fragment(frag_id="frag-B", phase=Phase.RISING),
            "B body.",
        )
        gen = DraftGenerator(
            llm=lambda _p: "Drafted.",
            skills_root=skills_root,
        )
        ontology = PromptOntology(
            prompt="x",
            phases=(
                WeightedDimension(value=Phase.PEAKING, weight=0.8),
                WeightedDimension(value=Phase.RISING, weight=0.1),
            ),
        )
        spec = SeedSpec(ontology=ontology, ontology_confidence_threshold=0.5)

        draft = gen.generate_seeded_draft(spec, vault_path=vault)

        # Only the peaking slice survives the threshold.
        assert draft.source_fragments == ("frag-A",)


class TestSeedSpecOntologyField:
    """``SeedSpec.ontology`` plays nicely with the existing invariants."""

    def test_ontology_alone_is_not_empty(self) -> None:
        """A spec carrying only an ontology is non-empty."""
        from creek.classify.prompt import PromptOntology, WeightedDimension

        ontology = PromptOntology(
            prompt="x",
            phases=(WeightedDimension(value=Phase.RISING, weight=0.7),),
        )
        assert SeedSpec(ontology=ontology).is_empty is False

    def test_empty_ontology_does_not_flip_empty(self) -> None:
        """An ontology with no detected dimensions is treated as no signal."""
        from creek.classify.prompt import PromptOntology

        ontology = PromptOntology(prompt="x")
        # is_empty checks the ``or`` chain; an empty PromptOntology has no
        # truthy dimensions but the object itself is truthy. We explicitly
        # short-circuit on ontology truthiness, so a populated-but-empty
        # ontology still counts as "set" — that's a useful signal that
        # detection ran (and returned nothing) versus never ran.
        # This test pins the documented behaviour.
        assert SeedSpec(ontology=ontology).is_empty is False

    def test_ontology_dimensions_flip_has_dimensional_filter(self) -> None:
        """An ontology with dimensions counts as a dimensional filter."""
        from creek.classify.prompt import PromptOntology, WeightedDimension

        ontology = PromptOntology(
            prompt="x",
            phases=(WeightedDimension(value=Phase.RISING, weight=0.7),),
        )
        spec = SeedSpec(ontology=ontology)
        assert spec.has_dimensional_filter is True

    def test_ontology_and_fragment_id_are_mutually_exclusive(self) -> None:
        """``--seed-fragment`` blocks ontology to preserve the FEAT-032 contract."""
        from creek.classify.prompt import PromptOntology, WeightedDimension

        ontology = PromptOntology(
            prompt="x",
            phases=(WeightedDimension(value=Phase.RISING, weight=0.7),),
        )
        with pytest.raises(ValueError, match="mutually exclusive"):
            SeedSpec(fragment_id="frag-A", ontology=ontology)


# ---- Issue #352: ontology-twist composition --------------------------------


class TestOntologyTwistHelpers:
    """Pure functions in :mod:`creek.generate.twist`."""

    def test_source_profile_picks_dominant_value_per_dimension(self) -> None:
        """The dominant non-unclassified value wins for each dimension."""
        from creek.generate.twist import (
            UNSPECIFIED,
            source_profile_from_fragments,
        )

        fragments = [
            _build_fragment(
                frag_id="f1",
                primary=Frequency.F7,
                phase=Phase.PEAKING,
                mode=Mode.INHABIT,
                dosage=Dosage.MEDICINE,
                register=VoiceRegister.ANALYTICAL,
            ),
            _build_fragment(
                frag_id="f2",
                primary=Frequency.F7,
                phase=Phase.PEAKING,
                mode=Mode.INHABIT,
                dosage=Dosage.MEDICINE,
                register=VoiceRegister.ANALYTICAL,
            ),
            _build_fragment(
                frag_id="f3",
                primary=Frequency.F3,
                phase=Phase.RISING,
                mode=Mode.EXPRESS,
                dosage=Dosage.TOXIC,
                register=VoiceRegister.PROPHETIC,
            ),
        ]

        profile = source_profile_from_fragments(fragments)

        assert profile.phase == "peaking"
        assert profile.mode == "inhabit"
        assert profile.frequency == "F7"
        assert profile.dosage == "medicine"
        assert profile.voice_register == "analytical"
        assert UNSPECIFIED == "unspecified"

    def test_source_profile_skips_unclassified_values(self) -> None:
        """``unclassified`` is filtered before the dominance vote."""
        from creek.generate.twist import (
            UNSPECIFIED,
            source_profile_from_fragments,
        )

        fragments = [
            # First two fragments are unclassified on phase; only the third
            # carries a real signal. The dominant pick should be that one
            # rather than ``unclassified``.
            _build_fragment(frag_id="f1"),
            _build_fragment(frag_id="f2"),
            _build_fragment(frag_id="f3", phase=Phase.WITHDRAWAL),
        ]

        profile = source_profile_from_fragments(fragments)

        assert profile.phase == "withdrawal"
        # Frequency is F1 across all three (default), so it wins.
        assert profile.frequency == "F1"
        # No fragment carried a mode, dosage, or register — leave them open.
        assert profile.mode == UNSPECIFIED
        assert profile.dosage == UNSPECIFIED
        assert profile.voice_register == UNSPECIFIED

    def test_source_profile_empty_fragments_yields_all_unspecified(self) -> None:
        """An empty corpus leaves every dimension as :data:`UNSPECIFIED`."""
        from creek.generate.twist import UNSPECIFIED, source_profile_from_fragments

        profile = source_profile_from_fragments([])

        assert profile.phase == UNSPECIFIED
        assert profile.mode == UNSPECIFIED
        assert profile.frequency == UNSPECIFIED
        assert profile.dosage == UNSPECIFIED
        assert profile.voice_register == UNSPECIFIED

    def test_target_profile_explicit_flags_win(self) -> None:
        """``SeedSpec.phases`` / ``modes`` / ``frequencies`` outrank ontology."""
        from creek.classify.prompt import PromptOntology, WeightedDimension
        from creek.generate.twist import target_profile_from_spec

        # Ontology asks for rising/express/F2 but the operator's flags
        # pin peaking/inhabit/F7. Explicit picks should win the duel.
        ontology = PromptOntology(
            prompt="x",
            phases=(WeightedDimension(value=Phase.RISING, weight=0.9),),
            modes=(WeightedDimension(value=Mode.EXPRESS, weight=0.8),),
            frequencies=(WeightedDimension(value=Frequency.F2, weight=0.7),),
        )
        spec = SeedSpec(
            phases=(Phase.PEAKING,),
            modes=(Mode.INHABIT,),
            frequencies=(Frequency.F7,),
            ontology=ontology,
        )

        profile = target_profile_from_spec(spec)

        assert profile.phase == "peaking"
        assert profile.mode == "inhabit"
        assert profile.frequency == "F7"

    def test_target_profile_includes_dosage_and_register_from_ontology(self) -> None:
        """Dosage and voice register come from the ontology heaviest pick."""
        from creek.classify.prompt import PromptOntology, WeightedDimension
        from creek.generate.twist import target_profile_from_spec

        ontology = PromptOntology(
            prompt="x",
            dosages=(WeightedDimension(value=Dosage.TOXIC, weight=0.7),),
            voice_registers=(
                WeightedDimension(value=VoiceRegister.PROPHETIC, weight=0.8),
            ),
        )
        spec = SeedSpec(ontology=ontology)

        profile = target_profile_from_spec(spec)

        assert profile.dosage == "toxic"
        assert profile.voice_register == "prophetic"

    def test_target_profile_no_signal_yields_unspecified(self) -> None:
        """No flags and no ontology means every dimension is unspecified."""
        from creek.generate.twist import UNSPECIFIED, target_profile_from_spec

        spec = SeedSpec(topic="x")
        profile = target_profile_from_spec(spec)

        assert profile.phase == UNSPECIFIED
        assert profile.mode == UNSPECIFIED
        assert profile.frequency == UNSPECIFIED
        assert profile.dosage == UNSPECIFIED
        assert profile.voice_register == UNSPECIFIED

    def test_twist_dimensions_lists_only_concrete_diffs(self) -> None:
        """Divergence requires both sides to be specified and different."""
        from creek.generate.twist import OntologyProfile, twist_dimensions

        source = OntologyProfile(phase="peaking", mode="inhabit", frequency="F7")
        target = OntologyProfile(
            phase="withdrawal",
            mode="inhabit",
            frequency="F3",
        )

        diff = twist_dimensions(source, target)

        # phase and frequency diverge; mode matches and is therefore omitted.
        # dosage / voice_register are unspecified on both sides, so they too
        # are omitted (we don't invent divergences the operator didn't ask
        # for).
        assert diff == ["phase", "frequency"]

    def test_twist_dimensions_empty_when_profiles_match(self) -> None:
        """source == target collapses to an empty divergence list."""
        from creek.generate.twist import OntologyProfile, twist_dimensions

        profile = OntologyProfile(phase="peaking", mode="inhabit", frequency="F7")
        assert twist_dimensions(profile, profile) == []

    def test_format_twist_directive_names_diverging_dimensions(self) -> None:
        """The directive lists divergent dimensions with arrows."""
        from creek.generate.twist import (
            OntologyProfile,
            format_twist_directive,
        )

        source = OntologyProfile(phase="peaking", frequency="F7")
        target = OntologyProfile(phase="withdrawal", frequency="F3")

        directive = format_twist_directive(
            source,
            target,
            ["phase", "frequency"],
        )

        assert "## Twist directive" in directive
        assert "phase: peaking -> withdrawal" in directive
        assert "frequency: F7 -> F3" in directive
        # Verbatim-paraphrase clause from the issue body must appear.
        assert "Do not paraphrase any single source verbatim" in directive
        assert "Recombine across the corpus" in directive

    def test_format_twist_directive_no_twist_collapses_to_noop(self) -> None:
        """No divergent dimensions yields a benign single-line directive."""
        from creek.generate.twist import (
            OntologyProfile,
            format_twist_directive,
        )

        profile = OntologyProfile(phase="peaking")

        directive = format_twist_directive(profile, profile, [])

        assert "## Twist directive" in directive
        # No "diverge" wording and no arrow lines.
        assert "->" not in directive
        assert "paraphrase" not in directive.lower()

    def test_require_plural_sources_passes_with_two_or_more(self) -> None:
        """Two or more sources is enough to recombine across."""
        from creek.generate.twist import require_plural_sources

        require_plural_sources(("frag-A", "frag-B"))
        require_plural_sources(("frag-A", "frag-B", "frag-C"))

    def test_require_plural_sources_raises_on_singleton(self) -> None:
        """A single source fails loudly because recombination needs two."""
        from creek.generate.twist import (
            PluralitySourceError,
            require_plural_sources,
        )

        with pytest.raises(PluralitySourceError, match="at least two"):
            require_plural_sources(("frag-A",))

    def test_require_plural_sources_raises_on_empty(self) -> None:
        """Zero sources also fails — the message names the actual count."""
        from creek.generate.twist import (
            PluralitySourceError,
            require_plural_sources,
        )

        with pytest.raises(PluralitySourceError, match="got 0"):
            require_plural_sources(())


class TestOntologyTwistComposition:
    """End-to-end behaviour: ``DraftGenerator(ontology_twist=True)``."""

    def _seed_two_peaking_inhabit_fragments(self, vault: Path) -> None:
        """Seed two source fragments at ``phase=peaking, mode=inhabit, F7``.

        Used by tests that need a plural source set with a known source
        profile so the twist delta is predictable.
        """
        _write_fragment(
            vault,
            _build_fragment(
                frag_id="frag-A",
                primary=Frequency.F7,
                phase=Phase.PEAKING,
                mode=Mode.INHABIT,
                dosage=Dosage.MEDICINE,
            ),
            "A body about integration.",
        )
        _write_fragment(
            vault,
            _build_fragment(
                frag_id="frag-B",
                primary=Frequency.F7,
                phase=Phase.PEAKING,
                mode=Mode.INHABIT,
                dosage=Dosage.MEDICINE,
            ),
            "B body about empathy.",
        )

    def test_target_equals_source_collapses_to_baseline_prompt(
        self,
        vault: Path,
        skills_root: Path,
    ) -> None:
        """When target==source the twist directive is a no-op (regression baseline).

        Acceptance criterion: "when target == source, composition
        behaves like today's draft." The prompt still carries the
        ``## Twist directive`` header for transparency but does not
        instruct any re-framing.
        """
        self._seed_two_peaking_inhabit_fragments(vault)

        captured: dict[str, str] = {}

        def llm(prompt: str) -> str:
            """Capture the prompt for assertion and return a fixed body."""
            captured["prompt"] = prompt
            return "Drafted body."

        generator = DraftGenerator(
            llm=llm,
            skills_root=skills_root,
            ontology_twist=True,
        )
        # Operator targets peaking/inhabit — same as the source corpus.
        spec = SeedSpec(
            phases=(Phase.PEAKING,),
            modes=(Mode.INHABIT,),
        )

        draft = generator.generate_seeded_draft(spec, vault_path=vault)

        prompt = captured["prompt"]
        assert "## Twist directive" in prompt
        # No diff arrows and no paraphrase clause when profiles match.
        assert "->" not in prompt
        assert "Do not paraphrase" not in prompt
        # Draft's twist_dimensions is empty because nothing diverged.
        assert draft.twist_dimensions == ()
        # Both profiles are recorded for transparency.
        assert draft.source_profile is not None
        assert draft.target_profile is not None
        assert draft.source_profile.phase == "peaking"
        assert draft.target_profile.phase == "peaking"

    def test_partial_twist_names_diverging_dimensions(
        self,
        vault: Path,
        skills_root: Path,
    ) -> None:
        """A 1-2 dimension twist names exactly the divergent dimensions.

        Acceptance criterion: "when target differs from source, the
        prompt explicitly names the differing dimensions and instructs
        the LLM to apply the target's style to the source's content."
        """
        self._seed_two_peaking_inhabit_fragments(vault)
        # Also add a fragment in the WITHDRAWAL phase so the dimensional
        # retrieval has material in that slice; the source profile will
        # still be dominated by the peaking pair.
        _write_fragment(
            vault,
            _build_fragment(
                frag_id="frag-C",
                primary=Frequency.F7,
                phase=Phase.WITHDRAWAL,
                mode=Mode.INHABIT,
                dosage=Dosage.MEDICINE,
            ),
            "Withdrawal body about shadow.",
        )

        captured: dict[str, str] = {}

        def llm(prompt: str) -> str:
            """Capture the prompt for assertion and return a fixed body."""
            captured["prompt"] = prompt
            return "Drafted body."

        generator = DraftGenerator(
            llm=llm,
            skills_root=skills_root,
            ontology_twist=True,
        )
        # Target asks for withdrawal+inhabit; source corpus is dominantly
        # peaking+inhabit so only the phase dimension diverges.
        spec = SeedSpec(
            phases=(Phase.WITHDRAWAL,),
            modes=(Mode.INHABIT,),
        )

        draft = generator.generate_seeded_draft(spec, vault_path=vault)

        prompt = captured["prompt"]
        assert "## Twist directive" in prompt
        # The phase dimension diverges (peaking -> withdrawal) and that
        # exact line is in the prompt.
        assert "phase: peaking -> withdrawal" in prompt
        assert "Do not paraphrase any single source verbatim" in prompt
        # Mode matches on both sides so it is NOT named as divergent.
        assert "mode:" not in prompt.split("## Twist directive")[1]
        # Draft records the divergent dimensions.
        assert draft.twist_dimensions == ("phase",)
        assert draft.source_profile is not None
        assert draft.target_profile is not None
        assert draft.source_profile.phase == "peaking"
        assert draft.target_profile.phase == "withdrawal"

    def test_full_twist_names_every_diverging_dimension(
        self,
        vault: Path,
        skills_root: Path,
    ) -> None:
        """A full twist surfaces every divergent dimension in the prompt.

        Acceptance criterion: "tests covering ... full twist (every
        dimension diverges)."

        Setup: source corpus is F7 / peaking / inhabit / medicine /
        analytical. The vault also carries one out-of-profile fragment
        on every divergent dimension so the per-dimension retrieval
        finds material on each requested slice. The target profile is
        driven entirely by the ontology so phase/mode/frequency can
        diverge from the explicit retrieval flags.
        """
        from creek.classify.prompt import PromptOntology, WeightedDimension

        # Two source-profile fragments — these drive the dominant source
        # profile across every dimension.
        for frag_id in ("frag-src-1", "frag-src-2"):
            _write_fragment(
                vault,
                _build_fragment(
                    frag_id=frag_id,
                    primary=Frequency.F7,
                    phase=Phase.PEAKING,
                    mode=Mode.INHABIT,
                    dosage=Dosage.MEDICINE,
                    register=VoiceRegister.ANALYTICAL,
                ),
                f"{frag_id} body.",
            )
        # One off-profile fragment per divergent retrieval slice. These
        # let the per-dimension OR retrieval find material on each
        # requested dimension; they're outvoted in the source profile
        # by the two source-profile fragments above.
        _write_fragment(
            vault,
            _build_fragment(
                frag_id="frag-withdrawal",
                primary=Frequency.F7,
                phase=Phase.WITHDRAWAL,
                mode=Mode.INHABIT,
                dosage=Dosage.MEDICINE,
                register=VoiceRegister.ANALYTICAL,
            ),
            "Withdrawal body.",
        )
        _write_fragment(
            vault,
            _build_fragment(
                frag_id="frag-express",
                primary=Frequency.F7,
                phase=Phase.PEAKING,
                mode=Mode.EXPRESS,
                dosage=Dosage.MEDICINE,
                register=VoiceRegister.ANALYTICAL,
            ),
            "Express body.",
        )
        _write_fragment(
            vault,
            _build_fragment(
                frag_id="frag-f3",
                primary=Frequency.F3,
                phase=Phase.PEAKING,
                mode=Mode.INHABIT,
                dosage=Dosage.MEDICINE,
                register=VoiceRegister.ANALYTICAL,
            ),
            "F3 body.",
        )

        captured: dict[str, str] = {}

        def llm(prompt: str) -> str:
            """Capture the prompt for assertion and return a fixed body."""
            captured["prompt"] = prompt
            return "Drafted body."

        generator = DraftGenerator(
            llm=llm,
            skills_root=skills_root,
            ontology_twist=True,
        )
        ontology = PromptOntology(
            prompt="x",
            dosages=(WeightedDimension(value=Dosage.TOXIC, weight=0.8),),
            voice_registers=(
                WeightedDimension(value=VoiceRegister.PROPHETIC, weight=0.8),
            ),
        )
        # Explicit flags drive both retrieval AND the target profile on
        # phase/mode/frequency; the ontology supplies dosage + register
        # for the target. All five target picks differ from the source
        # corpus's dominant profile.
        spec = SeedSpec(
            phases=(Phase.WITHDRAWAL,),
            modes=(Mode.EXPRESS,),
            frequencies=(Frequency.F3,),
            ontology=ontology,
        )

        draft = generator.generate_seeded_draft(spec, vault_path=vault)

        prompt = captured["prompt"]
        # All five dimensions diverge from source to target.
        assert "phase: peaking -> withdrawal" in prompt
        assert "stance: inhabit -> express" in prompt
        assert "frequency: F7 -> F3" in prompt
        assert "dosage: medicine -> toxic" in prompt
        assert "voice register: analytical -> prophetic" in prompt
        assert set(draft.twist_dimensions) == {
            "phase",
            "mode",
            "frequency",
            "dosage",
            "voice_register",
        }

    def test_plurality_failure_raises_loudly(
        self,
        vault: Path,
        skills_root: Path,
    ) -> None:
        """Single-source retrieval fails fast under twist composition.

        Acceptance criterion: "Composition requires plurality of source
        fragments — fails loudly if only one source matches."
        """
        from creek.generate.twist import PluralitySourceError

        # Only one fragment matches the peaking phase.
        _write_fragment(
            vault,
            _build_fragment(
                frag_id="frag-only",
                primary=Frequency.F7,
                phase=Phase.PEAKING,
                mode=Mode.INHABIT,
            ),
            "The only body.",
        )

        generator = DraftGenerator(
            llm=lambda _p: "Drafted.",
            skills_root=skills_root,
            ontology_twist=True,
        )
        spec = SeedSpec(phases=(Phase.PEAKING,))

        with pytest.raises(PluralitySourceError, match="at least two"):
            generator.generate_seeded_draft(spec, vault_path=vault)

    def test_frontmatter_records_source_target_and_twist_dimensions(
        self,
        vault: Path,
        skills_root: Path,
    ) -> None:
        """Acceptance criterion: frontmatter records source/target profiles."""
        self._seed_two_peaking_inhabit_fragments(vault)
        _write_fragment(
            vault,
            _build_fragment(
                frag_id="frag-C",
                primary=Frequency.F7,
                phase=Phase.WITHDRAWAL,
                mode=Mode.INHABIT,
                dosage=Dosage.MEDICINE,
            ),
            "Withdrawal body.",
        )

        generator = DraftGenerator(
            llm=lambda _p: "Drafted body.",
            skills_root=skills_root,
            ontology_twist=True,
        )
        spec = SeedSpec(
            phases=(Phase.WITHDRAWAL,),
            modes=(Mode.INHABIT,),
        )

        draft = generator.generate_seeded_draft(spec, vault_path=vault)
        path = generator.save_draft(draft, vault)

        post = frontmatter.load(str(path))
        meta = post.metadata
        assert meta["source_profile"]["phase"] == "peaking"
        assert meta["target_profile"]["phase"] == "withdrawal"
        assert meta["twist_dimensions"] == ["phase"]

    def test_frontmatter_omits_twist_fields_when_flag_disabled(
        self,
        vault: Path,
        skills_root: Path,
    ) -> None:
        """Default ``DraftGenerator`` (flag off) writes no twist fields.

        The flag is gated while in development; opting out must leave
        the existing frontmatter shape untouched.
        """
        self._seed_two_peaking_inhabit_fragments(vault)

        generator = DraftGenerator(
            llm=lambda _p: "Drafted body.",
            skills_root=skills_root,
        )
        spec = SeedSpec(phases=(Phase.PEAKING,))

        draft = generator.generate_seeded_draft(spec, vault_path=vault)
        path = generator.save_draft(draft, vault)

        post = frontmatter.load(str(path))
        assert "source_profile" not in post.metadata
        assert "target_profile" not in post.metadata
        assert "twist_dimensions" not in post.metadata
        # And the Draft itself carries the empty defaults.
        assert draft.source_profile is None
        assert draft.target_profile is None
        assert draft.twist_dimensions == ()

    def test_prompt_forbids_verbatim_paraphrase_under_twist(
        self,
        vault: Path,
        skills_root: Path,
    ) -> None:
        """Acceptance criterion: the prompt forbids verbatim paraphrase.

        The directive sentence from the issue body must appear in the
        prompt whenever the twist composition path is active and any
        dimension diverges.
        """
        self._seed_two_peaking_inhabit_fragments(vault)
        _write_fragment(
            vault,
            _build_fragment(
                frag_id="frag-C",
                primary=Frequency.F7,
                phase=Phase.WITHDRAWAL,
                mode=Mode.INHABIT,
                dosage=Dosage.MEDICINE,
            ),
            "Withdrawal body.",
        )

        captured: dict[str, str] = {}

        def llm(prompt: str) -> str:
            """Capture the prompt for assertion and return a fixed body."""
            captured["prompt"] = prompt
            return "Drafted body."

        generator = DraftGenerator(
            llm=llm,
            skills_root=skills_root,
            ontology_twist=True,
        )
        spec = SeedSpec(
            phases=(Phase.WITHDRAWAL,),
            modes=(Mode.INHABIT,),
        )

        generator.generate_seeded_draft(spec, vault_path=vault)

        prompt = captured["prompt"]
        assert "Use the provided source musings" in prompt
        assert "Do not paraphrase any single source verbatim" in prompt
        assert "Recombine across the corpus" in prompt


# ---- Issue #354: --seed-outline multi-section composition -------------------


def _ontology_detector_by_phase(
    phase_by_keyword: dict[str, Phase],
    *,
    default_phase: Phase = Phase.PEAKING,
    weight: float = 0.9,
) -> Callable[[str], object]:
    """Return a deterministic detector keyed on a section's text.

    Maps a substring keyword to a detected :class:`Phase` so a test can
    give different sections different ontologies without any LLM. Every
    other dimension is left empty so the target profile is driven by the
    phase pick alone.
    """
    from creek.classify.prompt import PromptOntology, WeightedDimension

    def _detect(prompt: str) -> object:
        lowered = prompt.lower()
        chosen = default_phase
        for keyword, phase in phase_by_keyword.items():
            if keyword.lower() in lowered:
                chosen = phase
                break
        return PromptOntology(
            prompt=prompt,
            phases=(WeightedDimension(value=chosen, weight=weight),),
        )

    return _detect


def _empty_detector() -> Callable[[str], object]:
    """Return a detector that always reports an empty ontology."""
    from creek.classify.prompt import PromptOntology

    def _detect(prompt: str) -> object:
        return PromptOntology(prompt=prompt)

    return _detect


class TestGenerateOutlineDraft:
    """``generate_outline_draft`` composes each section, then stitches."""

    def test_two_section_outline_sections_appear_in_order(
        self,
        vault: Path,
        skills_root: Path,
    ) -> None:
        """Acceptance: each outline section appears in the draft, in order."""
        outline = (
            "## Alpha claim\nThe first movement.\n\n"
            "## Omega claim\nThe closing movement.\n"
        )

        def llm(prompt: str) -> str:
            # Section composition echoes the heading so we can assert
            # order; the stitch pass returns its input unchanged.
            if "Alpha claim" in prompt and "Omega claim" in prompt:
                return prompt  # stitch pass — keep both bodies
            if "Alpha claim" in prompt:
                return "BODY-ALPHA"
            return "BODY-OMEGA"

        gen = DraftGenerator(llm=llm, skills_root=skills_root)
        draft = gen.generate_outline_draft(
            outline,
            vault_path=vault,
            detect_ontology=_empty_detector(),
        )

        assert len(draft.sections) == 2
        assert [s.heading for s in draft.sections] == ["Alpha claim", "Omega claim"]
        # Both composed bodies survive into the stitched draft, in order.
        alpha_at = draft.body.index("BODY-ALPHA")
        omega_at = draft.body.index("BODY-OMEGA")
        assert alpha_at < omega_at

    def test_five_section_outline(
        self,
        vault: Path,
        skills_root: Path,
    ) -> None:
        """Acceptance: a five-section outline yields five ordered sections."""
        outline = "\n\n".join(f"## Section {n}\nBody {n}." for n in range(1, 6))

        def llm(prompt: str) -> str:
            return prompt  # echo so each body and the stitch survive

        gen = DraftGenerator(llm=llm, skills_root=skills_root)
        draft = gen.generate_outline_draft(
            outline,
            vault_path=vault,
            detect_ontology=_empty_detector(),
        )

        assert len(draft.sections) == 5
        assert [s.heading for s in draft.sections] == [
            f"Section {n}" for n in range(1, 6)
        ]

    def test_mixed_ontologies_recorded_per_section(
        self,
        vault: Path,
        skills_root: Path,
    ) -> None:
        """Acceptance: each section records its own detected ontology profile.

        Two sections detect different phases; the per-section target
        profiles must differ accordingly.
        """
        # Source material so each section can retrieve real fragments.
        for frag_id, phase in (
            ("frag-rise-1", Phase.RISING),
            ("frag-rise-2", Phase.RISING),
            ("frag-with-1", Phase.WITHDRAWAL),
            ("frag-with-2", Phase.WITHDRAWAL),
        ):
            _write_fragment(
                vault,
                _build_fragment(
                    frag_id=frag_id,
                    primary=Frequency.F7,
                    phase=phase,
                    mode=Mode.INHABIT,
                ),
                f"{frag_id} body about the work.",
            )
        outline = (
            "## The ascent\nThe rising movement of the work.\n\n"
            "## The retreat\nThe withdrawal that follows.\n"
        )
        detector = _ontology_detector_by_phase(
            {"ascent": Phase.RISING, "retreat": Phase.WITHDRAWAL},
        )

        gen = DraftGenerator(llm=lambda p: p, skills_root=skills_root)
        draft = gen.generate_outline_draft(
            outline,
            vault_path=vault,
            detect_ontology=detector,
        )

        assert draft.sections[0].target_profile is not None
        assert draft.sections[1].target_profile is not None
        assert draft.sections[0].target_profile.phase == "rising"
        assert draft.sections[1].target_profile.phase == "withdrawal"

    def test_section_with_no_source_material_still_appears(
        self,
        vault: Path,
        skills_root: Path,
    ) -> None:
        """A section that matches no fragments is still composed and kept."""
        outline = "## Lonely section\nNothing in the vault matches this.\n"

        gen = DraftGenerator(llm=lambda _p: "FALLBACK BODY", skills_root=skills_root)
        draft = gen.generate_outline_draft(
            outline,
            vault_path=vault,
            detect_ontology=_empty_detector(),
        )

        assert len(draft.sections) == 1
        assert draft.sections[0].heading == "Lonely section"
        assert "FALLBACK BODY" in draft.body

    def test_no_header_outline_rejected(
        self,
        vault: Path,
        skills_root: Path,
    ) -> None:
        """Acceptance: a header-less outline is rejected with a clear error."""
        from creek.generate.outline import OutlineParseError

        gen = DraftGenerator(llm=lambda _p: "x", skills_root=skills_root)
        with pytest.raises(OutlineParseError, match="no markdown headers"):
            gen.generate_outline_draft(
                "Just a paragraph, no headers at all.\n",
                vault_path=vault,
                detect_ontology=_empty_detector(),
            )

    def test_stitch_pass_receives_connective_tissue_directive(
        self,
        vault: Path,
        skills_root: Path,
    ) -> None:
        """Acceptance: the stitch directive forbids paraphrase / invention."""
        outline = "## One\nFirst.\n\n## Two\nSecond.\n"
        captured: dict[str, str] = {}

        def llm(prompt: str) -> str:
            if "Stitch directive" in prompt:
                captured["stitch"] = prompt
                return prompt
            return "section body"

        gen = DraftGenerator(llm=llm, skills_root=skills_root)
        gen.generate_outline_draft(
            outline,
            vault_path=vault,
            detect_ontology=_empty_detector(),
        )

        stitch = captured["stitch"]
        lowered = stitch.lower()
        assert "transition" in lowered
        assert "do not" in lowered
        assert "invent" in lowered

    def test_empty_llm_body_raises(
        self,
        vault: Path,
        skills_root: Path,
    ) -> None:
        """An empty stitched body fails loudly rather than saving a blank draft."""
        outline = "## One\nFirst.\n"

        gen = DraftGenerator(llm=lambda _p: "   ", skills_root=skills_root)
        with pytest.raises(RuntimeError, match="empty"):
            gen.generate_outline_draft(
                outline,
                vault_path=vault,
                detect_ontology=_empty_detector(),
            )


class TestSaveOutlineDraft:
    """``save_outline_draft`` records per-section profiles in frontmatter."""

    def test_frontmatter_records_per_section_profiles(
        self,
        vault: Path,
        skills_root: Path,
    ) -> None:
        """Acceptance: per-section ontology profiles land in frontmatter."""
        outline = (
            "## The ascent\nThe rising movement.\n\n"
            "## The retreat\nThe withdrawal that follows.\n"
        )
        detector = _ontology_detector_by_phase(
            {"ascent": Phase.RISING, "retreat": Phase.WITHDRAWAL},
        )

        gen = DraftGenerator(llm=lambda p: p, skills_root=skills_root)
        draft = gen.generate_outline_draft(
            outline,
            vault_path=vault,
            detect_ontology=detector,
        )
        path = gen.save_outline_draft(draft, vault)

        post = frontmatter.load(str(path))
        sections_meta = post.metadata["sections"]
        assert isinstance(sections_meta, list)
        assert len(sections_meta) == 2
        assert sections_meta[0]["heading"] == "The ascent"
        assert sections_meta[0]["target_profile"]["phase"] == "rising"
        assert sections_meta[1]["target_profile"]["phase"] == "withdrawal"

    def test_saved_draft_is_typed_and_titled(
        self,
        vault: Path,
        skills_root: Path,
    ) -> None:
        """The saved file carries the draft type and the outline title."""
        outline = "# My Essay Title\n\n## Body\nContent here.\n"

        gen = DraftGenerator(llm=lambda _p: "stitched body", skills_root=skills_root)
        draft = gen.generate_outline_draft(
            outline,
            vault_path=vault,
            detect_ontology=_empty_detector(),
        )
        path = gen.save_outline_draft(draft, vault)

        post = frontmatter.load(str(path))
        assert post.metadata["type"] == "draft"
        assert post.metadata["title"] == "My Essay Title"
        assert (vault / DRAFTS_SUBDIR) in path.parents


class TestDraftTruncation:
    """Drafts surface a ``max_tokens`` cutoff instead of truncating silently."""

    def _seed_one_fragment(self, vault: Path) -> Fragment:
        """Seed and return a single fragment for the single-topic draft path."""
        fragment = _build_fragment()
        _write_fragment(vault, fragment, "A line.")
        return fragment

    def test_truncated_response_flags_draft(
        self,
        vault: Path,
        skills_root: Path,
    ) -> None:
        """A ``max_tokens`` stop reason marks the returned draft truncated."""
        from creek.generate.drafts import DraftLLMResponse

        self._seed_one_fragment(vault)
        gen = DraftGenerator(
            llm=lambda _p: DraftLLMResponse(
                text="Cut off here", stop_reason="max_tokens"
            ),
            skills_root=skills_root,
        )
        seed = _build_seed()
        draft = gen.generate_draft(seed, vault_path=vault)
        assert draft.truncated is True

    def test_clean_response_is_not_truncated(
        self,
        vault: Path,
        skills_root: Path,
    ) -> None:
        """A clean ``end_turn`` completion leaves the draft un-flagged."""
        from creek.generate.drafts import DraftLLMResponse

        self._seed_one_fragment(vault)
        gen = DraftGenerator(
            llm=lambda _p: DraftLLMResponse(text="All done.", stop_reason="end_turn"),
            skills_root=skills_root,
        )
        draft = gen.generate_draft(_build_seed(), vault_path=vault)
        assert draft.truncated is False

    def test_plain_string_response_defaults_to_not_truncated(
        self,
        vault: Path,
        skills_root: Path,
    ) -> None:
        """A legacy ``str`` return is treated as a clean completion."""
        self._seed_one_fragment(vault)
        gen = DraftGenerator(llm=lambda _p: "Plain body.", skills_root=skills_root)
        draft = gen.generate_draft(_build_seed(), vault_path=vault)
        assert draft.truncated is False

    def test_truncation_warns_on_stderr(
        self,
        vault: Path,
        skills_root: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A truncated draft prints a clear warning to stderr."""
        from creek.generate.drafts import DraftLLMResponse

        self._seed_one_fragment(vault)
        gen = DraftGenerator(
            llm=lambda _p: DraftLLMResponse(text="Cut", stop_reason="max_tokens"),
            skills_root=skills_root,
        )
        gen.generate_draft(_build_seed(), vault_path=vault)
        captured = capsys.readouterr()
        assert "truncated at max_tokens" in captured.err
        assert "--max-tokens" in captured.err

    def test_saved_draft_records_truncation_and_footer(
        self,
        vault: Path,
        skills_root: Path,
    ) -> None:
        """A truncated draft writes ``truncated: true`` plus a body footer."""
        from creek.generate.drafts import DraftLLMResponse

        self._seed_one_fragment(vault)
        gen = DraftGenerator(
            llm=lambda _p: DraftLLMResponse(text="Cut off", stop_reason="max_tokens"),
            skills_root=skills_root,
        )
        draft = gen.generate_draft(_build_seed(), vault_path=vault)
        path = gen.save_draft(draft, vault)
        post = frontmatter.load(str(path))
        assert post.metadata["truncated"] is True
        assert "truncated — see frontmatter" in post.content

    def test_saved_clean_draft_has_no_footer(
        self,
        vault: Path,
        skills_root: Path,
    ) -> None:
        """A clean draft records ``truncated: false`` and no footer."""
        self._seed_one_fragment(vault)
        gen = DraftGenerator(llm=lambda _p: "All done.", skills_root=skills_root)
        draft = gen.generate_draft(_build_seed(), vault_path=vault)
        path = gen.save_draft(draft, vault)
        post = frontmatter.load(str(path))
        assert post.metadata["truncated"] is False
        assert "truncated — see frontmatter" not in post.content

    def test_outline_draft_truncation_propagates(
        self,
        vault: Path,
        skills_root: Path,
    ) -> None:
        """A truncated stitch pass flags the whole outline draft."""
        from creek.generate.drafts import DraftLLMResponse

        outline = "## One\nFirst.\n"

        def llm(prompt: str) -> DraftLLMResponse:
            if "Stitch directive" in prompt:
                return DraftLLMResponse(text="Stitched", stop_reason="max_tokens")
            return DraftLLMResponse(text="section body")

        gen = DraftGenerator(llm=llm, skills_root=skills_root)
        draft = gen.generate_outline_draft(
            outline,
            vault_path=vault,
            detect_ontology=_empty_detector(),
        )
        assert draft.truncated is True
        path = gen.save_outline_draft(draft, vault)
        post = frontmatter.load(str(path))
        assert post.metadata["truncated"] is True
        assert "truncated — see frontmatter" in post.content

    def test_outline_section_truncation_propagates(
        self,
        vault: Path,
        skills_root: Path,
    ) -> None:
        """A truncated section body flags the outline draft even if stitch is clean."""
        from creek.generate.drafts import DraftLLMResponse

        outline = "## One\nFirst.\n"

        def llm(prompt: str) -> DraftLLMResponse:
            if "Stitch directive" in prompt:
                return DraftLLMResponse(text="Stitched", stop_reason="end_turn")
            return DraftLLMResponse(text="section body", stop_reason="max_tokens")

        gen = DraftGenerator(llm=llm, skills_root=skills_root)
        draft = gen.generate_outline_draft(
            outline,
            vault_path=vault,
            detect_ontology=_empty_detector(),
        )
        assert draft.truncated is True
        assert draft.sections[0].truncated is True


class TestStitchPromptNotPersistedVerbatim:
    """``save_outline_draft`` stores a stitch-prompt hash, not the full text."""

    def test_frontmatter_size_independent_of_body_word_count(
        self,
        vault: Path,
        skills_root: Path,
    ) -> None:
        """Frontmatter size does not grow with section-body length."""
        outline = "## One\nFirst.\n\n## Two\nSecond.\n"

        def _frontmatter_size(section_words: int) -> int:
            body = " ".join(["word"] * section_words)

            def llm(prompt: str) -> str:
                if "Stitch directive" in prompt:
                    return "stitched essay body"
                return body

            gen = DraftGenerator(llm=llm, skills_root=skills_root)
            draft = gen.generate_outline_draft(
                outline,
                vault_path=vault,
                detect_ontology=_empty_detector(),
            )
            path = gen.save_outline_draft(draft, vault)
            post = frontmatter.load(str(path))
            # Re-dump only the metadata so body length is excluded.
            return len(yaml.safe_dump(post.metadata))

        small = _frontmatter_size(5)
        large = _frontmatter_size(5000)
        assert small == large

    def test_stitch_prompt_stored_as_hash(
        self,
        vault: Path,
        skills_root: Path,
    ) -> None:
        """The full stitch prompt is not persisted; only its sha256 hash is."""
        outline = "## One\nFirst.\n"

        def llm(prompt: str) -> str:
            if "Stitch directive" in prompt:
                return "stitched"
            return "section body"

        gen = DraftGenerator(llm=llm, skills_root=skills_root)
        draft = gen.generate_outline_draft(
            outline,
            vault_path=vault,
            detect_ontology=_empty_detector(),
        )
        path = gen.save_outline_draft(draft, vault)
        post = frontmatter.load(str(path))
        assert "stitch_prompt" not in post.metadata
        digest = post.metadata["stitch_prompt_sha256"]
        assert (
            digest
            == hashlib.sha256(
                draft.stitch_prompt.encode("utf-8"),
            ).hexdigest()
        )


class TestSourceProfileComputedOncePerSection:
    """A resolved section aggregates its source profile exactly once."""

    def test_source_profile_not_recomputed_under_twist(
        self,
        vault: Path,
        skills_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``source_profile_from_fragments`` runs once per resolved section."""
        import creek.generate.drafts as drafts_module

        # A header-only section makes its seed text just the heading, so
        # the per-dimension topic-substring filter has a phrase that the
        # fragment bodies contain — the section then resolves to source
        # material and reaches the deduplicated profile aggregation.
        topic = "peaking inhabit"
        _write_fragment(
            vault,
            _build_fragment(
                frag_id="frag-A",
                primary=Frequency.F7,
                phase=Phase.PEAKING,
                mode=Mode.INHABIT,
                dosage=Dosage.MEDICINE,
            ),
            f"A {topic} body about integration.",
        )
        _write_fragment(
            vault,
            _build_fragment(
                frag_id="frag-B",
                primary=Frequency.F7,
                phase=Phase.PEAKING,
                mode=Mode.INHABIT,
                dosage=Dosage.MEDICINE,
            ),
            f"A {topic} body about empathy.",
        )

        real = drafts_module.source_profile_from_fragments
        calls = {"n": 0}

        def _counting(fragments: object) -> object:
            calls["n"] += 1
            return real(fragments)

        monkeypatch.setattr(
            drafts_module,
            "source_profile_from_fragments",
            _counting,
        )

        # One header-only section (no body) so seed_text == heading.
        outline = "## peaking inhabit\n"
        detector = _ontology_detector_by_phase({"peaking": Phase.PEAKING})
        gen = DraftGenerator(
            llm=lambda _p: "body",
            skills_root=skills_root,
            ontology_twist=True,
        )
        gen.generate_outline_draft(
            outline,
            vault_path=vault,
            detect_ontology=detector,
        )
        assert calls["n"] == 1


_PREAMBLE = "## Voice targets\nThis writer's voice: uses em-dashes freely"


class TestStylePreambleInjection:
    """The FEAT-040.8 preamble reaches all three prompt paths intact."""

    def test_compose_prompt_injects_after_voice_core(
        self,
        skills_root: Path,
        llm_echo: Callable[[str], str],
    ) -> None:
        """The single-prompt path keeps voice core, preamble, and source."""
        gen = DraftGenerator(
            llm=llm_echo,
            skills_root=skills_root,
            voice_core="My baseline voice.",
            style_preamble=_PREAMBLE,
        )
        prompt = gen._compose_prompt(_build_seed(), [], "A source paragraph.")
        assert "## Voice core" in prompt
        assert "## Voice targets" in prompt
        # No FEAT-032 regression: the source material survives unchanged.
        assert "A source paragraph." in prompt
        assert (
            prompt.index("## Voice core")
            < prompt.index("## Voice targets")
            < prompt.index("## Source material")
        )

    def test_compose_prompt_omits_preamble_when_empty(
        self,
        skills_root: Path,
        llm_echo: Callable[[str], str],
    ) -> None:
        """With no preamble the prompt is unchanged from the legacy shape."""
        gen = DraftGenerator(
            llm=llm_echo,
            skills_root=skills_root,
            voice_core="My baseline voice.",
        )
        prompt = gen._compose_prompt(_build_seed(), [], "A source paragraph.")
        assert "## Voice targets" not in prompt

    def test_bare_section_injects_preamble(
        self,
        skills_root: Path,
    ) -> None:
        """The source-less bare-section path also carries the preamble."""
        captured: dict[str, str] = {}

        def _capture(prompt: str) -> str:
            captured["prompt"] = prompt
            return "Section body."

        gen = DraftGenerator(
            llm=_capture,
            skills_root=skills_root,
            voice_core="My baseline voice.",
            style_preamble=_PREAMBLE,
        )
        section = OutlineSection(heading="A heading", level=2, body="A directive.")
        gen._compose_bare_section(section, None)
        assert "## Voice core" in captured["prompt"]
        assert "## Voice targets" in captured["prompt"]

    def test_stitch_prompt_injects_preamble(self) -> None:
        """The stitch smoothing pass carries the preamble after voice core."""
        prompt = build_stitch_prompt(
            [("One", "First section."), ("Two", "Second section.")],
            voice_core="My baseline voice.",
            voice_targets=_PREAMBLE,
        )
        assert "## Voice core" in prompt
        assert "## Voice targets" in prompt
        # Section bodies survive intact through the stitch pass.
        assert "First section." in prompt
        assert prompt.index("## Voice core") < prompt.index("## Voice targets")


_TROPEY_BODY = (
    "Additionally, this delves into the rich tapestry of the subject. "
    "Moreover, it is important to note the vibrant and multifaceted nuances. "
    "Furthermore, the landscape stands as a testament to its significance."
)


def _voice_fingerprint() -> VoiceFingerprint:
    """A non-thin fingerprint with near-zero AI-ish rates."""
    return VoiceFingerprint(
        features={"em_dash_density": FeatureStat(rate=0.0, support=20)},
        fragment_count=20,
    )


def _eager_ai_style(**overrides: object) -> AIStyleConfig:
    """AI-style config that wants to rewrite almost any divergence."""
    base: dict[str, object] = {"voice_distance_upper": 0.001, "max_revision_passes": 1}
    base.update(overrides)
    return AIStyleConfig(**base)


def _tropey_draft() -> Draft:
    """A draft whose body is dense with AI tells."""
    return Draft(
        title="On the tapestry",
        body=_TROPEY_BODY,
        idea_strategy=MiningStrategy.THREAD_TERMINUS.value,
        source_fragments=(),
        threads=(),
        eddies=(),
        skill_stack=(),
        prompt="p",
        generated_date=datetime(2026, 4, 20, tzinfo=UTC),
    )


class TestVoiceFidelityGuardWiring:
    """The FEAT-040.9 guard runs at save time and stamps frontmatter."""

    def test_save_draft_stamps_voice_fields(
        self,
        vault: Path,
        skills_root: Path,
        llm_echo: Callable[[str], str],
    ) -> None:
        """A guarded save records voice_distance and voice_findings."""
        gen = DraftGenerator(
            llm=llm_echo,
            skills_root=skills_root,
            fingerprint=_voice_fingerprint(),
            ai_style_config=_eager_ai_style(),
            voice_guard_no_llm=True,
        )
        path = gen.save_draft(_tropey_draft(), vault)
        meta = frontmatter.load(str(path)).metadata
        assert isinstance(meta["voice_distance"], float)
        assert meta["voice_distance"] > 0.0
        assert isinstance(meta["voice_findings"], list)
        assert meta["voice_findings"]

    def test_save_draft_without_config_stamps_nothing(
        self,
        vault: Path,
        skills_root: Path,
        llm_echo: Callable[[str], str],
    ) -> None:
        """No fingerprint/config means the guard is skipped entirely."""
        gen = DraftGenerator(llm=llm_echo, skills_root=skills_root)
        path = gen.save_draft(_tropey_draft(), vault)
        meta = frontmatter.load(str(path)).metadata
        assert "voice_distance" not in meta
        assert "voice_findings" not in meta

    def test_save_draft_disabled_subsystem_stamps_nothing(
        self,
        vault: Path,
        skills_root: Path,
        llm_echo: Callable[[str], str],
    ) -> None:
        """An explicitly disabled subsystem stamps no voice fields."""
        gen = DraftGenerator(
            llm=llm_echo,
            skills_root=skills_root,
            fingerprint=_voice_fingerprint(),
            ai_style_config=AIStyleConfig(enabled=False),
        )
        path = gen.save_draft(_tropey_draft(), vault)
        assert "voice_distance" not in frontmatter.load(str(path)).metadata

    def test_save_draft_rewrites_body_toward_voice(
        self,
        vault: Path,
        skills_root: Path,
    ) -> None:
        """A lower-distance rewrite replaces the saved body."""
        plain = "The cat sat by the window. Rain fell. I made tea and read."
        gen = DraftGenerator(
            llm=lambda _p: plain,
            skills_root=skills_root,
            fingerprint=_voice_fingerprint(),
            ai_style_config=_eager_ai_style(),
        )
        path = gen.save_draft(_tropey_draft(), vault)
        content = frontmatter.load(str(path)).content
        assert "tapestry" not in content
        assert "cat sat" in content

    def test_save_draft_no_llm_keeps_body_but_measures(
        self,
        vault: Path,
        skills_root: Path,
    ) -> None:
        """``--no-llm`` measures and stamps but does not rewrite the body."""
        called = {"n": 0}

        def _llm(_prompt: str) -> str:
            called["n"] += 1
            return "should not be used"

        gen = DraftGenerator(
            llm=_llm,
            skills_root=skills_root,
            fingerprint=_voice_fingerprint(),
            ai_style_config=_eager_ai_style(),
            voice_guard_no_llm=True,
        )
        path = gen.save_draft(_tropey_draft(), vault)
        post = frontmatter.load(str(path))
        assert called["n"] == 0
        assert "tapestry" in post.content
        assert isinstance(post.metadata["voice_distance"], float)

    def test_save_outline_draft_stamps_voice_fields(
        self,
        vault: Path,
        skills_root: Path,
    ) -> None:
        """The symmetric ``save_outline_draft`` path also runs the guard."""
        gen = DraftGenerator(
            llm=lambda _p: _TROPEY_BODY,
            skills_root=skills_root,
            fingerprint=_voice_fingerprint(),
            ai_style_config=_eager_ai_style(),
            voice_guard_no_llm=True,
        )
        draft = gen.generate_outline_draft(
            "## One\nFirst section.\n\n## Two\nSecond section.\n",
            vault_path=vault,
            detect_ontology=_empty_detector(),
        )
        path = gen.save_outline_draft(draft, vault)
        meta = frontmatter.load(str(path)).metadata
        assert isinstance(meta["voice_distance"], float)
        assert meta["voice_distance"] > 0.0
        assert isinstance(meta["voice_findings"], list)
        assert meta["voice_findings"]

    def test_grounding_check_short_circuits_on_empty_fragments(
        self,
        vault: Path,
        skills_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Empty source fragments skip the vault scan and yield no grounding veto."""
        called = {"n": 0}

        def _fake_loader(*_args: object, **_kwargs: object) -> dict[str, object]:
            called["n"] += 1
            return {}

        monkeypatch.setattr(
            "creek.generate.drafts._load_fragments_by_id",
            _fake_loader,
        )
        gen = DraftGenerator(
            llm=lambda _p: "x",
            skills_root=skills_root,
            embedding_fn=lambda _t: [1.0, 0.0],
            grounding_thresholds=GroundingThresholds(
                derivative_upper=0.85,
                grounding_lower=0.3,
                grounding_fraction_lower=0.3,
            ),
        )
        # Even with grounding fully wired, an empty source list means there is
        # nothing to ground against — so no vault scan and no veto closure.
        result = gen._voice_grounding_check(vault, ())
        assert result is None
        assert called["n"] == 0
