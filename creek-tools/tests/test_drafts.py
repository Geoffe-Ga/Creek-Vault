"""Tests for the draft generation workflow (issue #53)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

import frontmatter
import pytest

from creek.generate.drafts import (
    DRAFTS_SUBDIR,
    Draft,
    DraftGenerator,
)
from creek.generate.mining import IdeaSeed, MiningStrategy
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
    PraxisPotential,
    SourcePlatform,
    Thread,
    ThreadStatus,
    VoiceClassification,
    VoiceRegister,
    WavelengthClassification,
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
        wavelength=WavelengthClassification(mode=mode, orientation=orientation),
        voice=VoiceClassification(
            voice_register=register,
            confidence=Confidence.SETTLED if register else None,
        ),
        praxis_potential=PraxisPotential.LATENT,
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
