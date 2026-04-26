"""Draft generation — skill-activated essay drafts from idea seeds.

Section 11.5 of the Creek Ontology describes the draft workflow: when
the human selects an :class:`IdeaSeed`, the system assembles a skill
stack (frequency, phase, mode, register SKILL.md files), pulls the
source fragments and thread/eddy context, and asks an LLM to write a
draft that sounds like the human.

The draft is saved to ``07-Voice/Drafts/`` with full provenance in
frontmatter so a reader can trace every sentence back to its roots:

* ``source_fragments`` — IDs of the fragments that seeded the idea.
* ``threads`` / ``eddies`` — related narrative and topic clusters.
* ``activated_skills`` — relative paths of SKILL.md files used.
* ``prompt`` — the full LLM prompt (for reproducibility).

The LLM client is injected as a :data:`DraftLLM` callable so the module
stays deterministic and test-friendly; no network calls live in this
module.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import frontmatter
import yaml
from pydantic import ValidationError

from creek.models import (
    Eddy,
    Fragment,
    Phase,
    Thread,
)

if TYPE_CHECKING:
    from pathlib import Path

    from creek.generate.mining import IdeaSeed


logger = logging.getLogger(__name__)


DRAFTS_SUBDIR: str = "07-Voice/Drafts"
"""Relative path under the vault where drafts are stored."""

_FRAGMENTS_SUBDIR: str = "01-Fragments"
_THREADS_SUBDIR: str = "02-Threads"
_EDDIES_SUBDIR: str = "03-Eddies"

_SKILL_SUFFIX: str = ".SKILL.md"
_FREQUENCIES_DIR: str = "frequencies"
_PHASES_DIR: str = "phases"
_MODES_DIR: str = "modes"
_REGISTERS_DIR: str = "registers"

_SLUG_MAX_LENGTH: int = 80


DraftLLM = Callable[[str], str]
"""Signature for draft LLM clients: takes a prompt, returns the draft body."""


@dataclass(frozen=True)
class Draft:
    """An LLM-generated essay draft with full provenance.

    Attributes:
        title: Working title for the essay.
        body: The generated draft text.
        idea_strategy: :class:`MiningStrategy` value that surfaced the idea.
        source_fragments: Fragment IDs that seeded the idea.
        threads: Related thread IDs.
        eddies: Related eddy IDs.
        skill_stack: Relative paths of activated SKILL.md files.
        prompt: The full LLM prompt (for reproducibility).
        generated_date: When the draft was generated.
    """

    title: str
    body: str
    idea_strategy: str
    source_fragments: tuple[str, ...]
    threads: tuple[str, ...]
    eddies: tuple[str, ...]
    skill_stack: tuple[str, ...]
    prompt: str
    generated_date: datetime

    def __post_init__(self) -> None:
        """Validate draft invariants."""
        if not self.title.strip():
            msg = "Draft.title must be non-empty"
            raise ValueError(msg)
        if not self.body.strip():
            msg = "Draft.body must be non-empty"
            raise ValueError(msg)


def _safe_post(md_file: Path) -> frontmatter.Post | None:
    """Load a frontmatter post, returning ``None`` on parse errors."""
    try:
        return frontmatter.load(str(md_file))
    except (OSError, ValueError, yaml.YAMLError):
        logger.debug("Skipping unreadable markdown: %s", md_file)
        return None


def _load_fragments_by_id(root: Path) -> dict[str, tuple[Fragment, str]]:
    """Return ``{fragment.id: (fragment, body)}`` for every fragment under *root*."""
    if not root.exists():
        return {}
    collected: dict[str, tuple[Fragment, str]] = {}
    for md_file in sorted(root.rglob("*.md")):
        post = _safe_post(md_file)
        if post is None:
            continue
        metadata = dict(post.metadata)
        if metadata.get("type") != "fragment":
            continue
        try:
            fragment = Fragment.model_validate(metadata)
        except ValidationError:
            logger.debug("Skipping invalid fragment frontmatter: %s", md_file)
            continue
        collected[fragment.id] = (fragment, post.content)
    return collected


def _load_threads_by_id(root: Path) -> dict[str, Thread]:
    """Return ``{thread.id: thread}`` for every thread under *root*."""
    if not root.exists():
        return {}
    collected: dict[str, Thread] = {}
    for md_file in sorted(root.rglob("*.md")):
        post = _safe_post(md_file)
        if post is None:
            continue
        metadata = dict(post.metadata)
        if metadata.get("type") != "thread":
            continue
        try:
            thread = Thread.model_validate(metadata)
        except ValidationError:
            logger.debug("Skipping invalid thread frontmatter: %s", md_file)
            continue
        collected[thread.id] = thread
    return collected


def _load_eddies_by_id(root: Path) -> dict[str, Eddy]:
    """Return ``{eddy.id: eddy}`` for every eddy under *root*."""
    if not root.exists():
        return {}
    collected: dict[str, Eddy] = {}
    for md_file in sorted(root.rglob("*.md")):
        post = _safe_post(md_file)
        if post is None:
            continue
        metadata = dict(post.metadata)
        if metadata.get("type") != "eddy":
            continue
        try:
            eddy = Eddy.model_validate(metadata)
        except ValidationError:
            logger.debug("Skipping invalid eddy frontmatter: %s", md_file)
            continue
        collected[eddy.id] = eddy
    return collected


def _dominant(values: list[str]) -> str | None:
    """Return the most common non-empty string in *values*, or ``None``."""
    counts: dict[str, int] = {}
    for value in values:
        if not value:
            continue
        counts[value] = counts.get(value, 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda pair: (pair[1], pair[0]))[0]


def _dominant_classified(values: Iterable[object]) -> str | None:
    """Return the dominant classified (non-unclassified) value as a string."""
    stringified = [str(v) for v in values]
    filtered = [s for s in stringified if s and s != "unclassified"]
    return _dominant(filtered)


def _slugify(text: str) -> str:
    """Return a filesystem-safe slug for *text*, truncated to ``_SLUG_MAX_LENGTH``."""
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    if not cleaned:
        cleaned = "draft"
    if len(cleaned) > _SLUG_MAX_LENGTH:
        cleaned = cleaned[:_SLUG_MAX_LENGTH].rstrip("-")
    return cleaned


def _next_available_draft_path(
    drafts_dir: Path,
    date_prefix: str,
    slug: str,
) -> Path:
    """Return a non-colliding draft path under *drafts_dir*.

    Same-day drafts of the same title are otherwise silently overwritten;
    this helper appends ``-2``, ``-3``, … until an unused path is found.
    """
    base = drafts_dir / f"{date_prefix}-{slug}.md"
    if not base.exists():
        return base
    counter = 2
    while True:
        candidate = drafts_dir / f"{date_prefix}-{slug}-{counter}.md"
        if not candidate.exists():
            return candidate
        counter += 1


class DraftGenerator:
    """Generate an essay draft from an :class:`IdeaSeed` via a skill stack.

    Attributes:
        skills_root: Directory containing the SKILL.md tree.
        voice_core: Optional voice-core description prepended to prompts.
    """

    def __init__(
        self,
        *,
        llm: DraftLLM,
        skills_root: Path,
        voice_core: str = "",
    ) -> None:
        """Initialise with an LLM client and skill tree root.

        Args:
            llm: Callable ``(prompt) -> response`` for draft generation.
            skills_root: Directory containing the SKILL.md subtree
                (``frequencies/``, ``phases/``, ``modes/``, ``registers/``).
            voice_core: Optional voice-core description the prompt will
                open with (e.g. a paragraph describing the human's
                baseline voice).
        """
        self._llm = llm
        self.skills_root = skills_root
        self.voice_core = voice_core

    def present_idea(self, idea: IdeaSeed) -> str:
        """Format *idea* as an invitation rather than an imperative.

        The text mentions the idea's title, brief description, and the
        strategy that surfaced it, and gives the human the final say.

        Args:
            idea: The idea seed to present.

        Returns:
            A multi-line invitation string.
        """
        return (
            f'An idea has surfaced for you to consider: "{idea.title}".\n\n'
            f"{idea.brief_description}\n\n"
            f"(Surfaced via {idea.strategy.value.replace('_', ' ')}.) "
            "You may accept this invitation by drafting, or let it settle "
            "back into the compost."
        )

    def select_skill_stack(
        self,
        idea: IdeaSeed,
        *,
        vault_path: Path,
        current_phase: Phase | None = None,
        fragments: dict[str, tuple[Fragment, str]] | None = None,
    ) -> list[Path]:
        """Return the SKILL.md files to activate for *idea*.

        The stack is ordered: frequencies, phase, mode, register.
        Only files that exist under :attr:`skills_root` are returned;
        missing skills are skipped silently so the caller can operate
        on a partial skill tree.

        Args:
            idea: The idea seed whose attributes drive skill selection.
            vault_path: Vault root used to look up source fragments for
                dominant mode/register inference.
            current_phase: Optional current Archetypal Wavelength phase.
            fragments: Optional pre-loaded ``{id: (Fragment, body)}`` map;
                when provided the vault is not re-scanned.

        Returns:
            Ordered list of absolute paths to existing SKILL.md files.
        """
        paths: list[Path] = []
        paths.extend(self._frequency_skills(idea))
        phase_path = self._phase_skill(current_phase)
        if phase_path is not None:
            paths.append(phase_path)
        loaded = (
            fragments
            if fragments is not None
            else _load_fragments_by_id(vault_path / _FRAGMENTS_SUBDIR)
        )
        source_frags = [
            loaded[fid][0] for fid in idea.source_fragments if fid in loaded
        ]
        mode_path = self._mode_skill(source_frags)
        if mode_path is not None:
            paths.append(mode_path)
        register_path = self._register_skill(source_frags)
        if register_path is not None:
            paths.append(register_path)
        return [p for p in paths if p.exists()]

    def _frequency_skills(self, idea: IdeaSeed) -> list[Path]:
        """Return the frequency SKILL.md paths declared by *idea*."""
        return [
            self.skills_root / _FREQUENCIES_DIR / f"{freq.value}{_SKILL_SUFFIX}"
            for freq in idea.frequency_affinity
        ]

    def _phase_skill(self, current_phase: Phase | None) -> Path | None:
        """Return the phase SKILL.md for *current_phase*, if classified."""
        if current_phase is None or current_phase == Phase.UNCLASSIFIED:
            return None
        return self.skills_root / _PHASES_DIR / f"{current_phase.value}{_SKILL_SUFFIX}"

    def _mode_skill(self, fragments: list[Fragment]) -> Path | None:
        """Return the mode-orientation SKILL.md dominant in *fragments*."""
        mode = _dominant_classified(f.wavelength.mode for f in fragments)
        orientation = _dominant_classified(f.wavelength.orientation for f in fragments)
        if mode is None or orientation is None:
            return None
        key = f"{mode}-{orientation}"
        return self.skills_root / _MODES_DIR / f"{key}{_SKILL_SUFFIX}"

    def _register_skill(self, fragments: list[Fragment]) -> Path | None:
        """Return the voice register SKILL.md dominant in *fragments*."""
        registers = [
            str(f.voice.voice_register)
            for f in fragments
            if f.voice.voice_register is not None
        ]
        register = _dominant([r for r in registers if r])
        if register is None:
            return None
        return self.skills_root / _REGISTERS_DIR / f"{register}{_SKILL_SUFFIX}"

    def gather_source_material(
        self,
        idea: IdeaSeed,
        *,
        vault_path: Path,
        fragments: dict[str, tuple[Fragment, str]] | None = None,
    ) -> str:
        """Collect fragment bodies and thread/eddy descriptions for *idea*.

        Returns a markdown-shaped block the LLM can cite from. Missing
        IDs are skipped quietly. The sections are:

        * ``## Source fragments`` — ``### {id}: {title}`` followed by body.
        * ``## Threads`` — ``### {id}: {title}`` followed by description.
        * ``## Eddies`` — ``### {id}: {title}`` followed by description.

        Args:
            idea: Idea seed whose IDs drive the lookup.
            vault_path: Vault root used to load fragments/threads/eddies.
            fragments: Optional pre-loaded ``{id: (Fragment, body)}`` map;
                when provided the vault is not re-scanned for fragments.

        Returns:
            A newline-joined markdown string.
        """
        sections: list[str] = []
        loaded = (
            fragments
            if fragments is not None
            else _load_fragments_by_id(vault_path / _FRAGMENTS_SUBDIR)
        )
        frag_block = _render_fragment_section(idea.source_fragments, loaded)
        if frag_block:
            sections.append(frag_block)
        threads = _load_threads_by_id(vault_path / _THREADS_SUBDIR)
        thread_block = _render_thread_section(idea.threads, threads)
        if thread_block:
            sections.append(thread_block)
        eddies = _load_eddies_by_id(vault_path / _EDDIES_SUBDIR)
        eddy_block = _render_eddy_section(idea.eddies, eddies)
        if eddy_block:
            sections.append(eddy_block)
        return "\n\n".join(sections)

    def _compose_prompt(
        self,
        idea: IdeaSeed,
        skill_stack: list[Path],
        source_material: str,
    ) -> str:
        """Assemble the LLM prompt from voice-core + skills + source + ask."""
        parts: list[str] = []
        if self.voice_core:
            parts.append(f"## Voice core\n{self.voice_core.strip()}")
        if skill_stack:
            skill_sections = [_render_skill_section(path) for path in skill_stack]
            parts.append("## Activated skills\n\n" + "\n\n".join(skill_sections))
        if source_material:
            parts.append(f"## Source material\n{source_material}")
        parts.append(
            "## Ask\n"
            f'Write a draft essay titled "{idea.title}". '
            f"{idea.brief_description} "
            "Write as the human — not as an AI. Honour the activated "
            "skills. Cite source fragments by their IDs where relevant.",
        )
        return "\n\n".join(parts)

    def generate_draft(
        self,
        idea: IdeaSeed,
        *,
        vault_path: Path,
        current_phase: Phase | None = None,
    ) -> Draft:
        """Compose a prompt and call the LLM to generate a :class:`Draft`.

        Args:
            idea: The idea seed to draft from.
            vault_path: Vault root used for skill-stack inference and
                source-material gathering.
            current_phase: Optional current phase for the phase skill.

        Returns:
            A populated :class:`Draft` — the body comes from the LLM
            callable; everything else is provenance.
        """
        fragments = _load_fragments_by_id(vault_path / _FRAGMENTS_SUBDIR)
        skill_stack = self.select_skill_stack(
            idea,
            vault_path=vault_path,
            current_phase=current_phase,
            fragments=fragments,
        )
        source_material = self.gather_source_material(
            idea,
            vault_path=vault_path,
            fragments=fragments,
        )
        prompt = self._compose_prompt(idea, skill_stack, source_material)
        body = self._llm(prompt).strip()
        if not body:
            msg = "LLM returned an empty draft body"
            raise RuntimeError(msg)
        return Draft(
            title=idea.title,
            body=body,
            idea_strategy=idea.strategy.value,
            source_fragments=idea.source_fragments,
            threads=idea.threads,
            eddies=idea.eddies,
            skill_stack=tuple(
                str(p.relative_to(self.skills_root)) for p in skill_stack
            ),
            prompt=prompt,
            generated_date=datetime.now(tz=UTC),
        )

    def save_draft(self, draft: Draft, vault_path: Path) -> Path:
        """Write *draft* to ``07-Voice/Drafts/YYYY-MM-DD-{slug}.md``.

        The frontmatter captures every provenance field so the draft
        can be traced back to its sources and regenerated.

        Args:
            draft: The draft to persist.
            vault_path: Vault root under which ``07-Voice/Drafts`` lives.

        Returns:
            The absolute path of the written markdown file.
        """
        drafts_dir = vault_path / DRAFTS_SUBDIR
        drafts_dir.mkdir(parents=True, exist_ok=True)
        slug = _slugify(draft.title)
        date_prefix = draft.generated_date.strftime("%Y-%m-%d")
        target = _next_available_draft_path(drafts_dir, date_prefix, slug)
        post = frontmatter.Post(
            content=draft.body.strip() + "\n",
            type="draft",
            title=draft.title,
            status="draft",
            idea_strategy=draft.idea_strategy,
            source_fragments=list(draft.source_fragments),
            threads=list(draft.threads),
            eddies=list(draft.eddies),
            activated_skills=list(draft.skill_stack),
            generated_date=draft.generated_date.isoformat(),
            prompt=draft.prompt,
        )
        target.write_text(frontmatter.dumps(post), encoding="utf-8")
        return target


def _render_skill_section(path: Path) -> str:
    """Return a ``### {skill name}`` block with the file contents inlined.

    Args:
        path: Absolute path to the SKILL.md file to read.

    Returns:
        A markdown block with the skill heading and the file body; if the
        file cannot be read the block falls back to just the heading so
        the prompt still records which skill was activated.
    """
    try:
        body = path.read_text(encoding="utf-8").strip()
    except OSError:
        logger.warning("Could not read skill file %s; inlining name only.", path)
        return f"### {path.name}"
    if not body:
        return f"### {path.name}"
    return f"### {path.name}\n{body}"


def _render_fragment_section(
    ids: tuple[str, ...],
    fragments: dict[str, tuple[Fragment, str]],
) -> str:
    """Return the ``## Source fragments`` block for the given IDs."""
    entries: list[str] = []
    for fid in ids:
        if fid not in fragments:
            continue
        fragment, body = fragments[fid]
        entries.append(f"### {fid}: {fragment.title}\n{body.strip()}")
    if not entries:
        return ""
    return "## Source fragments\n\n" + "\n\n".join(entries)


def _render_thread_section(
    ids: tuple[str, ...],
    threads: dict[str, Thread],
) -> str:
    """Return the ``## Threads`` block for the given IDs."""
    entries: list[str] = []
    for tid in ids:
        if tid not in threads:
            continue
        thread = threads[tid]
        desc = thread.description.strip() or "(no description)"
        entries.append(f"### {tid}: {thread.title}\n{desc}")
    if not entries:
        return ""
    return "## Threads\n\n" + "\n\n".join(entries)


def _render_eddy_section(
    ids: tuple[str, ...],
    eddies: dict[str, Eddy],
) -> str:
    """Return the ``## Eddies`` block for the given IDs."""
    entries: list[str] = []
    for eid in ids:
        if eid not in eddies:
            continue
        eddy = eddies[eid]
        desc = eddy.description.strip() or "(no description)"
        entries.append(f"### {eid}: {eddy.title}\n{desc}")
    if not entries:
        return ""
    return "## Eddies\n\n" + "\n\n".join(entries)
