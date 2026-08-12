"""Tests for :class:`creek.purge.dryrun.DryRunLedger`.

The ledger is the bookkeeping that lets a dry run predict its own apply
(#1340): it records the files an apply run *would* have removed and the
text an apply run *would* have written, so a later pass in the same
operation sees the world the real run would have left behind.

Its whole job is answering "have I already handled this path?", which
makes path identity the only interesting property it has. Two spellings
of one file — ``/var/folders/...`` and ``/private/var/folders/...`` on
macOS, or any path containing ``..`` — must be the same key, or the
ledger silently never matches and every dry run quietly diverges again.
These tests pin that, plus the two degenerate cases: a path that cannot
be resolved at all, and a ledger that has been told nothing.

Behaviour under ``dry_run=False`` is not this module's subject: the
engine is required never to *query* the ledger in apply mode, and that
invariant is pinned in ``tests/test_purge.py`` against the filesystem.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from creek.purge.dryrun import DryRunLedger

if TYPE_CHECKING:
    from pathlib import Path


_LEDGER_TEXT = "the text an apply run would have written"
"""Stand-in for a scrubbed file body handed to the ledger."""

_NUL_NAME = "bad\x00name.md"
"""A filename whose embedded NUL makes ``Path.resolve`` raise ``ValueError``.

The same hazard ``PurgeEngine._resolve_pointer_in_vault`` already
tolerates: frontmatter is operator-controlled text, so an unresolvable
path is a real input, not a hypothetical one.
"""


def _two_spellings(tmp_path: Path) -> tuple[Path, Path]:
    """Return two textually different paths naming one file.

    The round trip through ``sub/..`` differs from the direct form on
    every platform, so the test does not depend on macOS's
    ``/var`` → ``/private/var`` symlink to have something to prove.

    Args:
        tmp_path: Pytest temporary directory.

    Returns:
        Tuple of ``(direct, indirect)`` paths for the same file.
    """
    (tmp_path / "sub").mkdir(exist_ok=True)
    return tmp_path / "note.md", tmp_path / "sub" / ".." / "note.md"


def test_a_path_marked_removed_is_removed_under_its_resolved_spelling(
    tmp_path: Path,
) -> None:
    """Marking the unresolved form answers for the resolved one."""
    ledger = DryRunLedger()
    direct, indirect = _two_spellings(tmp_path)

    ledger.mark_removed(indirect)

    assert ledger.is_removed(indirect) is True
    assert ledger.is_removed(direct) is True


def test_a_path_marked_under_its_resolved_spelling_answers_for_the_other(
    tmp_path: Path,
) -> None:
    """And the round trip the other way: resolved in, unresolved out.

    A ledger that normalises only on write, or only on read, passes one
    of these two tests and fails the other. Both directions are asserted
    so the normalisation has to happen on both.
    """
    ledger = DryRunLedger()
    direct, indirect = _two_spellings(tmp_path)

    ledger.mark_removed(direct)

    assert ledger.is_removed(direct) is True
    assert ledger.is_removed(indirect) is True


def test_recorded_text_round_trips_through_either_path_spelling(
    tmp_path: Path,
) -> None:
    """``set_text`` / ``text_for`` share the ledger's path identity rule."""
    ledger = DryRunLedger()
    direct, indirect = _two_spellings(tmp_path)

    ledger.set_text(indirect, _LEDGER_TEXT)

    assert ledger.text_for(indirect) == _LEDGER_TEXT
    assert ledger.text_for(direct) == _LEDGER_TEXT


def test_recorded_text_set_by_the_resolved_spelling_reads_back_by_the_other(
    tmp_path: Path,
) -> None:
    """The reverse round trip for the text side of the ledger."""
    ledger = DryRunLedger()
    direct, indirect = _two_spellings(tmp_path)

    ledger.set_text(direct, _LEDGER_TEXT)

    assert ledger.text_for(direct) == _LEDGER_TEXT
    assert ledger.text_for(indirect) == _LEDGER_TEXT


def test_an_unresolvable_path_is_recorded_under_its_literal_form(
    tmp_path: Path,
) -> None:
    """A path that cannot be resolved is still tracked, and never raises.

    Falling back to the literal spelling is the safe direction: the
    ledger is consulted only in a dry run, so the worst case of a
    fallback key that never matches is a preview that over-counts. An
    exception, by contrast, would abort the preview of an erasure.
    """
    ledger = DryRunLedger()
    hostile = tmp_path / _NUL_NAME

    ledger.mark_removed(hostile)
    ledger.set_text(hostile, _LEDGER_TEXT)

    assert ledger.is_removed(hostile) is True
    assert ledger.text_for(hostile) == _LEDGER_TEXT


def test_an_unresolvable_path_does_not_match_an_unrelated_one(
    tmp_path: Path,
) -> None:
    """The literal-form fallback must not collapse distinct paths together."""
    ledger = DryRunLedger()

    ledger.mark_removed(tmp_path / _NUL_NAME)

    assert ledger.is_removed(tmp_path / "other\x00name.md") is False


def test_a_fresh_ledger_knows_nothing(tmp_path: Path) -> None:
    """An empty ledger reports no removals and no recorded text.

    The default answers matter as much as the recorded ones: an
    ``is_removed`` that defaulted to ``True`` would make a dry run skip
    the whole vault and report a flawless, empty preview.
    """
    ledger = DryRunLedger()
    unknown = tmp_path / "never-seen.md"

    assert ledger.is_removed(unknown) is False
    assert ledger.text_for(unknown) is None


def test_two_ledgers_do_not_share_state(tmp_path: Path) -> None:
    """Each ledger owns its own records — no class-level mutable default.

    The engine builds a ledger per operation. A shared ``set`` or
    ``dict`` living on the class would leak one purge's bookkeeping into
    the next — the same defect ``tests/test_purge.py`` catches from the
    outside by running two operations on one engine.
    """
    first = DryRunLedger()
    second = DryRunLedger()
    target = tmp_path / "note.md"

    first.mark_removed(target)
    first.set_text(target, _LEDGER_TEXT)

    assert second.is_removed(target) is False
    assert second.text_for(target) is None
