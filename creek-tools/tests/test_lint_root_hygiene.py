"""Nothing writes to the vault root, and lint says so when something did (#883).

Two halves, and the second is the point.

**The fix.** ``creek.classify.review.ReviewQueueGenerator.generate_queue``
writes ``review-queue-<timestamp>.md`` to ``vault_path`` itself
(``review.py:101-103``), littering the root an operator opens in Obsidian
every day. It moves under ``00-Creek-Meta/Processing-Log/`` beside the other
machine-written logs.

**The detective work.** The stray zero-byte ``frag-f8cd9208e113.md`` that
prompted #883 has **no writer in today's tree** — it is not reproducible from
current code. A targeted fix therefore cannot be verified, which is the
argument for a standing check rather than a patch: whatever wrote it either no
longer exists or is not in ``creek/``, and the next one will be found the same
way this one was — by an operator noticing, months later.

Worth recording because the issue is wrong about its own evidence: the sweep
it proposes, ``grep 'vault_path / f"'``, returns **zero hits** across
``creek/`` and ``creek_mcp/``. The f-string is bound to a variable first, so
the issue's own grep would have missed the issue's own defect. The sweep that
does work — all 188 ``vault_path /``, ``vault_path.joinpath`` and
``self.vault_path /`` join sites — finds ``review.py:103`` as the only
root-level bare-filename *write*. Root-level reads
(``skill_size_budget.py:55`` reading ``AGENTS.md``) are correct, and every
config-driven relpath is subdirectory-scoped.

The check REPORTS. It never deletes: :meth:`TestTheCheckNeverMutates` pins
that, because a check that tidies the root would be a check that can delete an
operator's own note filed there on purpose.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from creek.classify.review import ReviewQueueGenerator
from creek.clean.hygiene import StaleReviewScanner

if TYPE_CHECKING:
    from pathlib import Path
    from types import ModuleType

    from creek.lint._result import CheckResult

RULE_ROOT_STRAY = "root-stray"
RULE_ZERO_BYTE = "zero-byte"
"""Stable finding tokens for the two root-hygiene rules."""

SCAFFOLD_DIRS: tuple[str, ...] = (
    "00-Creek-Meta",
    "01-Fragments",
    "02-Threads",
    "03-Eddies",
    "04-Praxis",
    "05-Wavelength",
    "06-Frequencies",
    "07-Voice",
    "08-Decisions",
    "09-Reference",
    "10-Liminal",
    "11-Other-Authors",
)
"""The twelve numbered directories ``creek init`` deploys.

Verified against ``creek/templates/vault/``. Alongside them the root
legitimately holds ``AGENTS.md``, ``creek-skills/`` and ``.obsidian/``.
"""

REVIEW_QUEUE_RELPARTS: tuple[str, ...] = ("00-Creek-Meta", "Processing-Log")
"""Where review queues belong after #883."""


def _root_hygiene() -> ModuleType:
    """Import the root-hygiene check module (added by the fix for #883).

    Imported per-test so a missing module fails each behaviour separately
    rather than collapsing the file into one collection error.
    """
    from creek.lint.checks import root_hygiene

    return root_hygiene


def _run(vault: Path) -> CheckResult:
    """Run the root-hygiene check against *vault*."""
    return _root_hygiene().run(vault)


def _findings_for(result: CheckResult, rule: str) -> list[str]:
    """Return the findings carrying *rule*'s token."""
    return [line for line in result.findings if rule in line]


@pytest.fixture
def scaffold(tmp_path: Path) -> Path:
    """A clean ``creek init`` vault: every legitimate root entry, nothing else."""
    root = tmp_path / "vault"
    for name in SCAFFOLD_DIRS:
        (root / name).mkdir(parents=True)
    (root / "creek-skills").mkdir()
    (root / ".obsidian").mkdir()
    (root / "AGENTS.md").write_text("# Agent contract\n", encoding="utf-8")
    (root / ".DS_Store").write_bytes(b"\x00")
    return root


class TestReviewQueueRelocation:
    """``generate_queue`` stops writing to the vault root."""

    def test_the_queue_lands_under_processing_log(self, scaffold: Path) -> None:
        """The returned path's parent is ``00-Creek-Meta/Processing-Log``."""
        path = ReviewQueueGenerator().generate_queue([], scaffold)

        assert path.parent == scaffold.joinpath(*REVIEW_QUEUE_RELPARTS)

    def test_nothing_new_appears_at_the_vault_root(self, scaffold: Path) -> None:
        """The observable symptom #883 reports: the root must not grow."""
        before = {entry.name for entry in scaffold.iterdir()}

        ReviewQueueGenerator().generate_queue([], scaffold)

        assert {entry.name for entry in scaffold.iterdir()} == before

    def test_the_filename_shape_is_unchanged(self, scaffold: Path) -> None:
        """``review-queue-%Y-%m-%d_%H%M%S.md`` exactly — the name is an interface.

        ``StaleReviewScanner`` finds queues by ``rglob("review-queue-*.md")``
        and ages them by ``strptime`` on the stem (``hygiene.py:451``,
        ``:498-506``). Renaming while relocating would silently stop
        ``creek clean stale-reviews`` from ever aging a queue again — and it
        would look like it worked, because the scan would simply find nothing.
        """
        path = ReviewQueueGenerator().generate_queue([], scaffold)

        assert path.name.startswith("review-queue-")
        assert path.suffix == ".md"
        stamp = path.stem.removeprefix("review-queue-")
        assert datetime.strptime(stamp, "%Y-%m-%d_%H%M%S").replace(tzinfo=UTC)

    def test_the_directory_is_created_when_absent(self, tmp_path: Path) -> None:
        """A vault with no Processing-Log yet must not raise."""
        root = tmp_path / "bare"
        root.mkdir()

        path = ReviewQueueGenerator().generate_queue([], root)

        assert path.is_file()


class TestStaleReviewScannerStillFindsQueues:
    """``creek clean stale-reviews`` must survive the move — both locations.

    These pass before and after the relocation: ``rglob`` already reaches both
    places. They are here so a regression in the scanner shows up as a scanner
    failure rather than as a queue that quietly never expires.
    """

    def _write_queue(self, vault: Path, relpath: str, *, days_old: int) -> Path:
        """Write a review queue *days_old* days in the past at *relpath*."""
        stamp = (datetime.now(tz=UTC) - timedelta(days=days_old)).strftime(
            "%Y-%m-%d_%H%M%S",
        )
        target = vault / relpath / f"review-queue-{stamp}.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# Classification Review Queue\n", encoding="utf-8")
        return target

    def test_a_queue_in_the_new_location_is_aged(self, scaffold: Path) -> None:
        """A stale queue under Processing-Log is still reported stale."""
        target = self._write_queue(
            scaffold,
            "/".join(REVIEW_QUEUE_RELPARTS),
            days_old=90,
        )

        result = StaleReviewScanner().scan(scaffold)

        assert str(target) in result.stale_paths

    def test_a_legacy_root_level_queue_is_still_found(self, scaffold: Path) -> None:
        """Queues written before #883 must not become invisible.

        The relocation does not migrate anything, so every existing vault has
        root-level queues. Losing track of them would turn a tidying change
        into permanent litter that ``creek clean`` can no longer remove.
        """
        target = self._write_queue(scaffold, ".", days_old=90)

        result = StaleReviewScanner().scan(scaffold)

        assert str(target) in result.stale_paths

    def test_a_fresh_queue_in_either_location_is_not_stale(
        self,
        scaffold: Path,
    ) -> None:
        """The negative control, so "found" is not confused with "always stale"."""
        self._write_queue(scaffold, "/".join(REVIEW_QUEUE_RELPARTS), days_old=0)
        self._write_queue(scaffold, ".", days_old=0)

        result = StaleReviewScanner().scan(scaffold)

        assert result.stale_paths == []
        assert result.total_review_files == 2


class TestRootStrayDetection:
    """Rule R1: anything at the root that ``creek init`` did not put there."""

    def test_a_clean_scaffold_reports_nothing(self, scaffold: Path) -> None:
        """No false positives on a fresh ``creek init`` vault.

        Includes ``.DS_Store``: Creek does not own the dot-namespace, and
        flagging it produces noise the operator cannot act on.
        """
        result = _run(scaffold)

        assert result.findings == []
        assert result.name == "root-hygiene"

    def test_a_stray_root_file_is_reported(self, scaffold: Path) -> None:
        """A bare markdown file at the root is the #883 symptom."""
        (scaffold / "review-queue-2026-01-01_120000.md").write_text(
            "# Classification Review Queue\n",
            encoding="utf-8",
        )

        hits = _findings_for(_run(scaffold), RULE_ROOT_STRAY)

        assert len(hits) == 1
        assert "review-queue-2026-01-01_120000.md" in hits[0]

    def test_a_legacy_review_queue_is_reported_as_the_intended_nudge(
        self,
        scaffold: Path,
    ) -> None:
        """Pre-#883 queues are flagged until the operator moves or deletes them.

        Deliberate and worth stating in the release notes: the check does not
        exempt Creek's own historical output, because "Creek put it there" is
        exactly the excuse that let the root accumulate in the first place.
        """
        (scaffold / "review-queue-2020-01-01_000000.md").touch()
        (scaffold / "review-queue-2020-01-01_000000.md").write_text(
            "# old\n",
            encoding="utf-8",
        )

        assert _findings_for(_run(scaffold), RULE_ROOT_STRAY)

    def test_a_stray_root_directory_is_reported(self, scaffold: Path) -> None:
        """A stray *directory* is the more alarming of the two and must not slip.

        A check scoped to files would miss a whole mis-rooted tree — the
        failure with the largest blast radius — while reporting a single
        stray note.
        """
        (scaffold / "12-Somewhere-Else").mkdir()

        hits = _findings_for(_run(scaffold), RULE_ROOT_STRAY)

        assert len(hits) == 1
        assert "12-Somewhere-Else" in hits[0]

    def test_a_file_inside_a_scaffold_directory_is_not_a_root_stray(
        self,
        scaffold: Path,
    ) -> None:
        """R1 is about the root only; the rest of the vault is other checks' job."""
        note = scaffold / "01-Fragments" / "Notes" / "frag-a.md"
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text("---\nid: frag-a\n---\n\nBody.\n", encoding="utf-8")

        assert _findings_for(_run(scaffold), RULE_ROOT_STRAY) == []


class TestZeroByteDetection:
    """Rule R2: an empty ``*.md`` anywhere is the ``frag-f8cd9208e113`` evidence."""

    def test_a_zero_byte_markdown_file_in_a_subdirectory_is_reported(
        self,
        scaffold: Path,
    ) -> None:
        """The original artifact was deep in the tree, not at the root."""
        stray = scaffold / "01-Fragments" / "Notes" / "frag-f8cd9208e113.md"
        stray.parent.mkdir(parents=True, exist_ok=True)
        stray.touch()

        hits = _findings_for(_run(scaffold), RULE_ZERO_BYTE)

        assert len(hits) == 1
        assert "frag-f8cd9208e113.md" in hits[0]

    def test_a_non_markdown_empty_file_is_not_reported(
        self,
        scaffold: Path,
    ) -> None:
        """A zero-byte ``.gitkeep`` or ``.jsonl`` sentinel is legitimate."""
        (scaffold / "01-Fragments" / ".gitkeep").touch()
        log = scaffold / "00-Creek-Meta" / "Processing-Log" / "compile-gaps.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.touch()

        assert _findings_for(_run(scaffold), RULE_ZERO_BYTE) == []

    def test_a_non_empty_markdown_file_is_not_reported(
        self,
        scaffold: Path,
    ) -> None:
        """Negative control: content means healthy."""
        note = scaffold / "02-Threads" / "t.md"
        note.write_text("---\ntype: thread\n---\n\nA current.\n", encoding="utf-8")

        assert _findings_for(_run(scaffold), RULE_ZERO_BYTE) == []

    def test_the_check_does_not_open_the_file(
        self,
        scaffold: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Emptiness is measured with ``stat().st_size``, never ``read_text``.

        Reading every ``*.md`` in a 35k-fragment vault to learn which are
        empty is both slow and an unnecessary invitation to put body text
        somewhere it can leak. ``st_size`` answers the question exactly.
        """
        (scaffold / "02-Threads" / "empty.md").touch()
        big = scaffold / "02-Threads" / "big.md"
        big.write_text("XYZZYSECRET body\n", encoding="utf-8")

        opened: list[str] = []
        real_read = type(big).read_text

        def _spy(self: Path, *args: object, **kwargs: object) -> str:
            opened.append(str(self))
            return real_read(self, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(type(big), "read_text", _spy)

        _run(scaffold)

        assert opened == []


class TestTheCheckNeverMutates:
    """REPORTS, never deletes. A tidier here can destroy an operator's own note."""

    def test_no_file_is_removed_added_or_rewritten(self, scaffold: Path) -> None:
        """Byte-for-byte identical tree before and after, plus identical listing."""
        (scaffold / "stray-note.md").write_text("Mine, on purpose.\n", encoding="utf-8")
        (scaffold / "Scratch").mkdir()
        (scaffold / "02-Threads" / "empty.md").touch()
        before_listing = sorted(scaffold.rglob("*"))
        before_bytes = {p: p.read_bytes() for p in before_listing if p.is_file()}

        _run(scaffold)

        assert sorted(scaffold.rglob("*")) == before_listing
        assert {p: p.read_bytes() for p in before_listing if p.is_file()} == (
            before_bytes
        )

    def test_a_second_run_reports_the_same_thing(self, scaffold: Path) -> None:
        """Idempotent: the check does not consume what it reports."""
        (scaffold / "stray-note.md").write_text("Mine.\n", encoding="utf-8")

        first = _run(scaffold).findings
        second = _run(scaffold).findings

        assert first == second
        assert first, "fixture produced no findings; equality above is vacuous"


class TestRegistration:
    """The check must run by default or an operator will never see it."""

    def test_root_hygiene_runs_in_the_default_set(self) -> None:
        """Registered and deterministic — no ``--full``, no ``--since``."""
        from creek.lint import runner as runner_module

        assert "root-hygiene" in runner_module.DETERMINISTIC_CHECKS
        assert "root-hygiene" in runner_module._REGISTRY
