"""The four-reader contract for ``10-Liminal/Synchronicities/`` (#1416).

One folder, four readers, one hostile-fixture set. The readers are:

* ``creek.generate.synchronicity._existing_synchronicity_pairs`` — the dedup
  read-back scan behind ``creek report --type synchronicity``;
* ``creek.lint.checks.synchronicity.run`` — the ``creek lint`` check;
* ``creek.generate.state._load_synchronicities`` — ``creek state``;
* ``creek.generate.mining._load_synchronicities`` — ``creek mine``.

They were written at four different times and grew four different ideas of
what "a note that will not parse" means, so an operator's hand edit took out
whichever of them happened to run next. This module is the forcing function
that stops that recurring: a fifth reader either joins ``_READERS`` and
survives the same fixtures, or it fails the arity guard below.

It is also the guard against drifting back to ``frontmatter.load*``. That API
ends in ``Post(content, handler, **metadata)``, and the splat raises
``TypeError: keywords must be strings`` on a header whose key is a date —
past any ``except (OSError, ValueError, yaml.YAMLError)``. Anybody who
reaches for it again fails these tests on the exact fixtures that motivated
the header-only reader.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from creek.cli import app
from creek.generate.synchronicity import generate_synchronicities
from tests.synchronicity_support import (
    HOSTILE_CASES,
    LEADING_BLANK_LINE,
    NON_STRING_KEY,
    plant_hostile_entry,
    scaffold_vault,
    seed_synchronicity_vault,
    sync_notes,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

runner = CliRunner()

assert NON_STRING_KEY.startswith("---\n2024-01-01:"), (
    "the non-string-key fixture must keep a date as its first header key"
)

_VALID_NEIGHBOUR = '---\ntype: synchronicity\nid: "sync-valid"\n---\n\nEcho.\n'
"""A well-formed synchronicity note planted beside a hostile one.

Its job is to prove the hostile note costs *itself* and nothing else: a
reader that aborts the folder walk loses this note too, and the assertion
that names it fails.
"""


# ---- C1: the contract pin --------------------------------------------------


def _read_generate(root: Path) -> object:
    """Invoke the dedup read-back scan in ``creek.generate.synchronicity``."""
    from creek.generate.synchronicity import _existing_synchronicity_pairs

    return _existing_synchronicity_pairs(root)


def _read_lint(root: Path) -> object:
    """Invoke the ``synchronicity`` lint check over *root*."""
    from creek.lint.checks import synchronicity as check

    return check.run(root)


def _read_state(root: Path) -> object:
    """Invoke ``creek state``'s synchronicity loader over *root*."""
    from creek.generate.state import _load_synchronicities

    return _load_synchronicities(root / "10-Liminal" / "Synchronicities")


def _read_mining(root: Path) -> object:
    """Invoke ``creek mine``'s synchronicity loader over *root*."""
    from creek.generate.mining import _load_synchronicities

    return _load_synchronicities(root / "10-Liminal" / "Synchronicities")


_READERS = (
    ("generate.synchronicity", _read_generate),
    ("lint.checks.synchronicity", _read_lint),
    ("generate.state", _read_state),
    ("generate.mining", _read_mining),
)
"""Every reader of ``10-Liminal/Synchronicities/``, as of #1416.

Each entry takes the **vault root**; the two loaders that really want the
synchronicity directory join it themselves inside their wrapper, so the
parametrised test can hand all four the same path.
"""

assert len(_READERS) == 4, "a fifth reader of this folder must join the contract"


@pytest.mark.parametrize(("name", "reader"), _READERS, ids=[n for n, _ in _READERS])
@pytest.mark.parametrize("case", HOSTILE_CASES)
def test_every_reader_survives_the_hostile_folder(
    tmp_path: Path,
    name: str,
    reader: Callable[[Path], object],
    case: str,
) -> None:
    """No reader of this folder may raise on an operator's hand edit.

    The forcing function of the module docstring, in twelve cells: three
    hostile entries against four readers. At HEAD five of those cells are
    red — ``non-string-key`` takes down all four readers and
    ``hand-broken-yaml`` takes down the dedup scan — so a reader that
    reaches back for ``frontmatter.load*`` fails here rather than in the
    operator's terminal.

    Args:
        tmp_path: pytest's per-test temporary directory, used as vault root.
        name: Dotted module name of the reader, for the failure message.
        reader: The reader wrapper under test.
        case: The hostile entry to plant, from :data:`HOSTILE_CASES`.
    """
    plant_hostile_entry(tmp_path, case)

    result = reader(tmp_path)

    assert result is not None, f"{name} returned nothing for the {case} folder"


# ---- C2: the lint surface --------------------------------------------------


def test_lint_check_reports_the_valid_note_beside_a_non_string_key(
    tmp_path: Path,
) -> None:
    """``creek lint`` keeps counting after a date-keyed header (#1416).

    At HEAD ``frontmatter.load`` raises ``TypeError: keywords must be
    strings`` on ``hostile-key.md`` — not caught by ``except (OSError,
    ValueError, yaml.YAMLError)`` — and the whole check dies, taking the
    perfectly good ``sync-valid`` note beside it. Under the header-only
    reader the hostile note also reads back **intact** (its ``type`` and
    ``id`` survive, because the mapping is returned whole rather than
    splatted into keyword arguments), so the assertion here is the presence
    of ``sync-valid`` rather than a findings count that would depend on
    whether the hostile note is counted.

    Args:
        tmp_path: pytest's per-test temporary directory, used as vault root.
    """
    from creek.lint.checks import synchronicity as check

    plant_hostile_entry(tmp_path, "non-string-key")
    sync_dir = tmp_path / "10-Liminal" / "Synchronicities"
    (sync_dir / "sync-valid.md").write_text(_VALID_NEIGHBOUR, encoding="utf-8")

    result = check.run(tmp_path)

    assert result.name == "synchronicity"
    assert any("sync-valid" in line for line in result.findings)


# ---- C3: the state and mine surfaces ---------------------------------------
#
# ``creek state`` and ``creek mine`` are the two surfaces the issue body never
# names, and both of them read this folder on every run. Each test asserts at
# the loader (exact rows, so the assertion survives a mutant that returns an
# empty list) and again at the CLI (exit 0, so the operator-visible surface is
# pinned too).


def test_state_reads_the_non_string_key_note_and_the_cli_exits_zero(
    tmp_path: Path,
) -> None:
    """``creek state`` survives a date-keyed synchronicity header (#1416).

    The loader returns the note's row rather than skipping it: a
    header-only reader hands back the whole mapping, so ``id``,
    ``fragment_a_id``, ``fragment_b_id``, ``similarity`` and
    ``time_gap_days`` all validate. At HEAD the same call raises
    ``TypeError`` and the audit report never renders.

    Args:
        tmp_path: pytest's per-test temporary directory.
    """
    from creek.generate.state import _load_synchronicities as state_load

    vault = scaffold_vault(tmp_path)
    plant_hostile_entry(vault, "non-string-key")

    rows = state_load(vault / "10-Liminal" / "Synchronicities")

    assert [row.sync_id for row in rows] == ["sync-key"]
    assert [row.fragment_a_id for row in rows] == ["p"]

    result = runner.invoke(app, ["state", "--vault", str(vault)])

    assert result.exit_code == 0, result.output


def test_mine_reads_the_non_string_key_note_and_the_cli_exits_zero(
    tmp_path: Path,
) -> None:
    """``creek mine`` survives a date-keyed synchronicity header (#1416).

    Same fixture, same header-only reader, different consumer: the miner
    wants ``(fragment_a_id, fragment_b_id, similarity)``, and all three
    survive the mapping that ``frontmatter.load`` cannot splat.

    Args:
        tmp_path: pytest's per-test temporary directory.
    """
    from creek.generate.mining import _load_synchronicities as mining_load

    vault = scaffold_vault(tmp_path)
    plant_hostile_entry(vault, "non-string-key")

    pairs = mining_load(vault / "10-Liminal" / "Synchronicities")

    assert pairs == [("p", "q", 0.95)]

    result = runner.invoke(app, ["mine", "--vault", str(vault)])

    assert result.exit_code == 0, result.output


# ---- C4: the over-broad-catch guard ----------------------------------------


class _Explosive:
    """A fragment entry whose stringification raises.

    Stands in for a genuine programming error inside the extraction loop,
    so a fix that reaches for ``except Exception:`` is caught doing it.
    """

    def __str__(self) -> str:
        """Raise, standing in for a genuine programming error in the loop.

        Raises:
            TypeError: Always. Never returns, despite the annotation
                ``__str__`` is obliged to carry.
        """
        msg = "boom"
        raise TypeError(msg)


def test_the_scan_does_not_swallow_a_real_type_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``TypeError`` from the extraction loop must still propagate.

    The cheap fix for #1416 is to widen the except tuple until nothing can
    escape it; this test is what that fix fails. A ``TypeError`` raised by
    the loop's own work is a bug in creek, not a bad note, and burying it
    turns "one operator note is malformed" into "dedup silently stopped
    working".

    It is deliberately **not** written by monkeypatching ``re.sub`` on the
    module under test. ``re`` is the shared stdlib module object, so
    patching ``creek.generate.synchronicity.re.sub`` fires process-wide —
    pytest's own assertion rewriting calls ``re.sub`` — and
    ``pytest.raises`` would then be satisfied by a ``TypeError`` raised
    nowhere near the code under test. Patching the module-level
    ``read_header_meta`` name targets the extraction loop precisely.

    The patched name only exists once the fix lands, so at HEAD this is red
    with ``AttributeError`` from ``monkeypatch.setattr``. That is the
    intended red: the contract is that the loop reads through a
    module-level ``read_header_meta``.

    Args:
        tmp_path: pytest's per-test temporary directory, used as vault root.
        monkeypatch: Fixture used to replace the header reader.
    """
    from creek.generate.synchronicity import _existing_synchronicity_pairs

    plant_hostile_entry(tmp_path, "hand-broken-yaml")
    monkeypatch.setattr(
        "creek.generate.synchronicity.read_header_meta",
        lambda _path: {"fragments": [_Explosive(), "b"]},
    )

    with pytest.raises(TypeError, match="boom"):
        _existing_synchronicity_pairs(tmp_path)


# ---- C5: the dedup parity lock (GREEN before and after) --------------------


def test_dedup_still_suppresses_the_second_run(tmp_path: Path) -> None:
    """Re-running writes nothing new — the idempotency the fix must keep.

    A parity lock, **not** a red-before-green case: this passes at HEAD and
    must still pass afterwards. It is the guard an ``except Exception:``
    fix fails, and the guard a "return ``{}`` on every note" reader fails —
    either one turns the read-back scan into a no-op, and a no-op scan
    duplicates the note on run two while every "did not crash" assertion
    stays green.

    Args:
        tmp_path: pytest's per-test temporary directory.
    """
    vault, config = seed_synchronicity_vault(tmp_path)

    first = generate_synchronicities(vault, config)
    second = generate_synchronicities(vault, config)

    assert len(first) == 1
    assert second == []
    assert len([p for p in sync_notes(vault) if p.is_file()]) == 1


# ---- C6: the leading-blank-line pin (the one deliberate narrowing) ---------


def test_a_note_fenced_below_a_blank_line_carries_no_frontmatter(
    tmp_path: Path,
) -> None:
    """A ``---`` on line 2 is not frontmatter, and does not suppress a run.

    This is a **reviewed behaviour deviation**, recorded in the PR body, not
    a silent narrowing. ``frontmatter.loads`` strips leading whitespace
    before hunting the fence, so at HEAD this note reads back as the pair
    ``{frag-synx-aaaa, frag-synx-bbbb}`` and suppresses the very
    synchronicity the seeded vault exists to produce (``written == []``).
    ``creek.vault.links.read_header_meta`` requires ``---`` on line 1 and so
    measures ``{}``, and the run writes its note.

    Justified because Obsidian does not recognise frontmatter after a blank
    line either, and creek's own writer always emits ``---`` at line 1
    (``SynchronicityDetector.create_synchronicity_note`` writes
    ``frontmatter.dumps(post)``, whose output always opens with the fence),
    so the only notes exposed to the difference are hand-edited ones — where
    matching Obsidian is the more defensible reading of the two.

    Args:
        tmp_path: pytest's per-test temporary directory.
    """
    vault, config = seed_synchronicity_vault(tmp_path)
    blank_first = vault / "10-Liminal" / "Synchronicities" / "blank-first-line.md"
    blank_first.write_text(LEADING_BLANK_LINE, encoding="utf-8")

    written = generate_synchronicities(vault, config)

    assert len(written) == 1
    assert written[0].name != "blank-first-line.md"
