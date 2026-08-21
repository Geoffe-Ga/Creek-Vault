"""The compile-gap signal must survive the #881 repair — HAZARD GUARD.

#881 is a *false-report* bug: a fully compiled vault logs a ``missing``
compile gap for every eddy and thread its fragments name, because the lookup
key is a bracketed wikilink (``"[[Messages]]"``) and never equals a
``CompiledPage.target_id``. The obvious repair — stop logging — would convert
a noisy-but-honest signal into a silent-and-wrong one, which is strictly
worse: ``compile-gaps.jsonl`` is the operational backlog ``creek lint`` reads
to tell an operator what still needs compiling.

**Every test in this module passes before the fix and must pass after it.**
That is the point. It is the evidence that #881 corrected the signal rather
than silencing it. A run of this file that goes green only because the
assertions were relaxed, or red because gap logging was removed, is a failed
repair regardless of what the rest of the suite says.

The vehicle is the real caller — ``DraftGenerator.gather_source_material``,
which reaches ``compiled.eddy(...)`` / ``compiled.thread(...)`` and the gap
log through ``creek/generate/drafts.py`` — not the logging primitive. Testing
``log_compile_gap`` directly would keep passing even if every call site
stopped invoking it.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from creek.generate.compile_routing import COMPILE_GAPS_RELPATH
from creek.generate.drafts import DraftGenerator
from creek.generate.mining import IdeaSeed, MiningStrategy
from creek.models import Frequency

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


def _seed(
    *,
    threads: tuple[str, ...] = (),
    eddies: tuple[str, ...] = (),
) -> IdeaSeed:
    """Return a minimal :class:`IdeaSeed` naming *threads* / *eddies*."""
    return IdeaSeed(
        strategy=MiningStrategy.LIMINAL_CROSS_EDDY,
        title="Naming what orbits",
        source_fragments=("frag-001",),
        threads=threads,
        eddies=eddies,
        frequency_affinity=(Frequency.F1,),
        brief_description="An essay waits here.",
        score=0.8,
    )


def _gap_records(vault: Path) -> list[dict[str, str]]:
    """Return every JSONL record in ``compile-gaps.jsonl`` (empty when absent)."""
    log_path = vault / COMPILE_GAPS_RELPATH
    if not log_path.is_file():
        return []
    return [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    """A minimal vault whose compiled directories exist but are empty."""
    for sub in ("01-Fragments", "02-Threads", "03-Eddies", "07-Voice/Drafts"):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture
def skills_root(tmp_path: Path) -> Path:
    """The skill tree ``DraftGenerator`` expects."""
    root = tmp_path / "skills"
    for sub in ("frequencies", "phases", "modes", "registers"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def llm_echo() -> Callable[[str], str]:
    """An LLM stub that echoes the prompt length."""

    def _call(prompt: str) -> str:
        return f"DRAFT({len(prompt)} chars)"

    return _call


class TestUnresolvableTargetsStillRecordAGap:
    """An eddy/thread that exists nowhere must still reach the backlog."""

    @pytest.mark.parametrize(
        "target",
        ["eddy-ghost", "[[Ghost Eddy]]", "Ghost Eddy"],
        ids=["bare-id", "wikilink", "bare-title"],
    )
    def test_an_unresolvable_eddy_appends_a_missing_gap(
        self,
        vault: Path,
        skills_root: Path,
        llm_echo: Callable[[str], str],
        target: str,
    ) -> None:
        """No page of any kind names *target*, so ``reason="missing"`` is honest.

        All three spellings are covered because the #881 repair normalises the
        lookup key: a fix that resolves ``[[Ghost Eddy]]`` to ``Ghost Eddy``
        and then declines to log is the silencing failure this guard exists to
        catch.
        """
        gen = DraftGenerator(llm=llm_echo, skills_root=skills_root)

        gen.gather_source_material(_seed(eddies=(target,)), vault_path=vault)

        records = _gap_records(vault)
        assert [r["reason"] for r in records] == ["missing"]
        assert records[0]["target_kind"] == "eddy"
        assert records[0]["surfaced_by"] == "draft"

    def test_an_unresolvable_thread_appends_a_missing_gap(
        self,
        vault: Path,
        skills_root: Path,
        llm_echo: Callable[[str], str],
    ) -> None:
        """Threads take the same path through ``_compose_thread_section``."""
        gen = DraftGenerator(llm=llm_echo, skills_root=skills_root)

        gen.gather_source_material(
            _seed(threads=("[[Ghost Thread]]",)),
            vault_path=vault,
        )

        records = _gap_records(vault)
        assert [r["reason"] for r in records] == ["missing"]
        assert records[0]["target_kind"] == "thread"

    def test_the_log_grows_by_one_line_per_run(
        self,
        vault: Path,
        skills_root: Path,
        llm_echo: Callable[[str], str],
    ) -> None:
        """The log is append-only: a second run adds a second record.

        Pins that the file is appended to rather than rewritten, so a repair
        cannot pass the single-record assertions above by truncating.
        """
        gen = DraftGenerator(llm=llm_echo, skills_root=skills_root)
        seed = _seed(eddies=("eddy-ghost",))

        gen.gather_source_material(seed, vault_path=vault)
        first = _gap_records(vault)
        gen.gather_source_material(seed, vault_path=vault)
        second = _gap_records(vault)

        assert len(first) == 1
        assert len(second) == 2
        assert {r["reason"] for r in second} == {"missing"}

    def test_every_unresolvable_target_is_recorded_not_just_the_first(
        self,
        vault: Path,
        skills_root: Path,
        llm_echo: Callable[[str], str],
    ) -> None:
        """Three ghosts, three records — no de-duplication, no short-circuit."""
        gen = DraftGenerator(llm=llm_echo, skills_root=skills_root)

        gen.gather_source_material(
            _seed(eddies=("eddy-g1", "eddy-g2", "eddy-g3")),
            vault_path=vault,
        )

        records = _gap_records(vault)
        assert sorted(r["target_id"] for r in records) == [
            "eddy-g1",
            "eddy-g2",
            "eddy-g3",
        ]

    def test_bypass_mode_still_suppresses_the_log(
        self,
        vault: Path,
        skills_root: Path,
        llm_echo: Callable[[str], str],
    ) -> None:
        """``--bypass-compiled`` is the one documented silence, and stays silent.

        Included so the guard cannot be satisfied by making *every* lookup log:
        the bypass contract is the boundary of "always append".
        """
        gen = DraftGenerator(
            llm=llm_echo,
            skills_root=skills_root,
            bypass_compiled=True,
        )

        gen.gather_source_material(_seed(eddies=("eddy-ghost",)), vault_path=vault)

        assert _gap_records(vault) == []
