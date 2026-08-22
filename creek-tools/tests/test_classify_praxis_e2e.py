"""End-to-end reachability of ``08-Decisions/`` via praxis (issue #877).

The unit suites pin each half of the fix in isolation. This suite pins the
thing the operator actually cares about: that a fragment carrying real
deliberation signals, run through the shipped CLI, ends up producing a
Decision note — a folder that was **structurally unreachable** before #877.

Why it was unreachable: ``DecisionDetector._detect_pattern``
(``creek/generate/decisions.py``) requires all three of
``praxis_potential == "explicit"``, ``voice.confidence == "exploring"``, and
an ``{F1, F4}`` or ``{F1, F5}`` frequency overlap. Nothing in the pipeline
ever wrote ``praxis_potential``, so the first condition was false for 100%
of the 35,330-fragment demo vault and the pattern strategy could never
fire. Only the *keyword* strategy — a title-substring match — could ever
produce a Decision note.

Which is exactly the trap this test is built to avoid. The fixture title
contains **none** of ``DECISION_KEYWORDS``, so the keyword strategy cannot
fire; the note asserted below can only exist if the pattern strategy ran,
and the pattern strategy can only run if ``praxis_potential`` was actually
written. Assertions on ``detection_method`` make that explicit rather than
implicit.

Everything here is deterministic and local: the rules classifier only, no
LLM, no network, no mocks.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import frontmatter
import pytest
from typer.testing import CliRunner

from creek.cli import app
from creek.generate.decisions import DECISION_KEYWORDS
from creek.models import Authorship, Fragment, FragmentSource, SourcePlatform
from tests.helpers import write_fragment_file

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()

_FRAGMENT_ID = "frag-praxis-e2e01"

_TITLE = "Winter boiler notes"
"""A title that matches no ``DECISION_KEYWORDS`` — pinned by a test below.

``DecisionDetector._detect_keywords`` matches against the *title only*. A
title carrying "considering" or "should i" would produce a Decision note
through the keyword strategy alone, false-greening this suite even with the
#877 bug fully present.
"""

_DELIBERATION_BODY = (
    "Exploring the survival math again: safety first, "
    "then a strategy for the winter.\n"
    "- [ ] book the boiler service before October"
)
"""A body engineered to trip all three ``_detect_pattern`` conditions.

Scoring is deliberate, not incidental (``RuleClassifier``: title x3, first
paragraph x2, body x1; there is no blank line, so the whole thing is the
first paragraph and every match counts double):

* **F1 primary** — ``survival`` + ``safety`` = 2 matches x2 = 4, clearing
  ``PRIMARY_THRESHOLD`` (3) and uniquely top-scoring.
* **F5 secondary** — ``strategy`` = 1 match x2 = 2, clearing
  ``SECONDARY_THRESHOLD`` (2). Together: the ``{F1, F5}`` deliberation pair.
* **``exploring`` confidence** — ``exploring`` = 1 match x2 = 2, clearing
  ``SECONDARY_THRESHOLD``, with every other confidence bucket at 0 (note
  the careful absence of "this is", "maybe", "might", "clearly", … which
  would otherwise outrank or tie it).
* **``explicit`` praxis** — the line-initial task checkbox is a single
  strong (weight-2) marker, which reaches ``_EXPLICIT_AT`` on its own.
"""


def _seed(vault: Path) -> Path:
    """Write the single deliberation fragment into *vault*.

    Written with **no** ``classification_method`` stamp, so the OPS-001 /
    issue-#321 resume short-circuit does not preserve it — otherwise the
    run would skip the fragment entirely and the praxis pass would never
    see it.

    Args:
        vault: Vault root.

    Returns:
        Path to the seeded fragment file.
    """
    return write_fragment_file(
        vault=vault,
        fragment=Fragment(
            id=_FRAGMENT_ID,
            title=_TITLE,
            source=FragmentSource(
                platform=SourcePlatform.MARKDOWN,
                author=Authorship.SELF,
            ),
        ),
        body=_DELIBERATION_BODY,
    )


def _active_decision_notes(vault: Path) -> list[Path]:
    """Return every markdown note under ``08-Decisions/Active/``.

    Sorted so the list is stable; ``Path.glob`` yields in filesystem
    order, which is hash-ordered on APFS.

    Args:
        vault: Vault root.

    Returns:
        Sorted list of decision-note paths (empty when the folder is
        absent).
    """
    active = vault / "08-Decisions" / "Active"
    if not active.exists():
        return []
    return sorted(active.glob("*.md"))


def test_fixture_title_matches_no_decision_keyword() -> None:
    """The fixture title must not trip the keyword detection strategy.

    Guards the whole suite: if a future edit renames the fixture to
    something containing "considering" or "the question is", every
    assertion below would still pass while testing nothing about #877.
    """
    lowered = _TITLE.lower()
    assert not [keyword for keyword in DECISION_KEYWORDS if keyword in lowered]


@pytest.mark.integration
def test_classify_then_report_produces_a_pattern_detected_decision(
    tmp_path: Path,
) -> None:
    """``creek classify`` → ``creek report`` yields one pattern Decision note.

    The full #877 payload, through the shipped CLI, with the rules
    classifier only:

    1. Before the run the fragment reads ``praxis_potential: none`` and no
       Decision note exists — the pre-#877 steady state of a whole vault.
    2. ``creek classify --method rules`` writes ``explicit`` to disk (plus
       the F1/F5 frequencies and ``exploring`` confidence the pattern
       strategy also needs).
    3. ``creek report --type decisions`` now produces exactly one note,
       and its ``detection_method`` is ``"pattern"`` — not ``"keyword"``,
       which is the only strategy that could ever fire before the fix.
    """
    vault = tmp_path / "vault"
    path = _seed(vault)

    # 1. Pre-state: the bug's signature, and no decisions reachable.
    before = frontmatter.load(str(path)).metadata
    assert before["praxis_potential"] == "none"
    assert _active_decision_notes(vault) == []

    empty = runner.invoke(app, ["report", "--type", "decisions", "--vault", str(vault)])
    assert empty.exit_code == 0, empty.output
    assert _active_decision_notes(vault) == [], (
        "a Decision note appeared before classification — the fixture is "
        "firing through some path other than praxis_potential"
    )

    # 2. Classification stamps every axis the pattern strategy reads.
    classified = runner.invoke(
        app,
        ["classify", "--method", "rules", "--vault", str(vault)],
    )
    assert classified.exit_code == 0, classified.output

    after = frontmatter.load(str(path)).metadata
    assert after["praxis_potential"] == "explicit"
    assert after["frequency"]["primary"] == "F1"
    assert "F5" in after["frequency"]["secondary"]
    assert after["voice"]["confidence"] == "exploring"

    # 3. …and 08-Decisions is finally reachable.
    reported = runner.invoke(
        app,
        ["report", "--type", "decisions", "--vault", str(vault)],
    )
    assert reported.exit_code == 0, reported.output

    notes = _active_decision_notes(vault)
    assert len(notes) == 1, f"expected exactly one decision note, got {notes}"
    note = frontmatter.load(str(notes[0])).metadata
    assert note["detection_method"] == "pattern"
    assert note["title"] == _TITLE


@pytest.mark.integration
def test_report_decisions_is_idempotent_after_the_praxis_pass(
    tmp_path: Path,
) -> None:
    """A second ``creek report`` run adds no duplicate note.

    ``creek fill`` runs the decisions report on every invocation. Now that
    the pattern strategy can actually fire, a non-idempotent writer would
    grow one duplicate note per fill — so the existing
    already-captured-fragment guard is re-pinned against the newly-live
    code path.
    """
    vault = tmp_path / "vault"
    _seed(vault)
    runner.invoke(app, ["classify", "--method", "rules", "--vault", str(vault)])

    for _ in range(2):
        result = runner.invoke(
            app,
            ["report", "--type", "decisions", "--vault", str(vault)],
        )
        assert result.exit_code == 0, result.output

    assert len(_active_decision_notes(vault)) == 1
