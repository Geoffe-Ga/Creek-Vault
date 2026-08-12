"""Guard against docs that name redaction artifacts the code never creates.

Issue #1338. ``creek redact`` has three modes and the prose describing them
drifted from the code. The two most concrete drifts are filesystem paths:
``docs/redaction.md`` and ``README.md`` tell operators that ``--scan``
deposits a queue at ``<source>/.creek-redactions/queue.json`` and that
``--apply`` consumes it. Neither name has ever been produced by anything
under ``creek/`` or ``creek_mcp/`` — ``--scan`` writes no files at all, and
``--report`` renders its summary to the console — so an operator who follows
the documentation goes looking for a file that cannot exist, and concludes
the scan silently failed.

Only those two literals are pinned. Wordings such as "reversible" or
"applied: true" are ordinary English with legitimate uses elsewhere in the
tree; a grep gate over them would be brittle and could be satisfied by
rephrasing rather than by correcting the claim. A filesystem path that the
production code has never created is a defect by construction, which is what
makes it safe to forbid outright.

The behavioural complements — what the three modes actually do — live in
``tests/test_redact_documented_behaviour.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest

from tests.markdown_integrity_support import tracked_markdown_files

if TYPE_CHECKING:
    from collections.abc import Sequence

# ``Path(__file__)`` is ``<root>/creek-tools/tests/test_redaction_docs_drift.py``:
# parents[0] is ``tests``, parents[1] is ``creek-tools``, parents[2] is the
# repository root. The sweep must run from the root — the CI ``quality`` job
# works from ``creek-tools/``, and a root-relative sweep is the only way the
# gate sees the repository-root ``README.md`` and ``docs/`` trees too.
REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]

PHANTOM_REDACTION_ARTIFACTS: Final[tuple[str, str]] = (
    ".creek-redactions",
    "queue.json",
)
"""Path names the documentation promises and the code has never written.

Deliberately exactly two entries, and deliberately both filesystem paths.
See the module docstring for why the vaguer doc claims are not pinned here.
"""

_CREEK_TOOLS_PREFIX: Final[str] = "creek-tools/"
"""Repo-relative prefix of the Python subproject, for the span check."""


def documents_naming(
    literal: str,
    documents: Sequence[tuple[str, str]],
) -> list[str]:
    """Return the labels of the documents whose text contains *literal*.

    Factored out so the repository-wide sweep and its non-vacuity control
    run the *same* matcher. A detector that quietly stopped matching
    anything would otherwise turn the sweep into a pass over nothing.

    Args:
        literal: The exact substring to look for.
        documents: ``(label, text)`` pairs, where the label is what a
            failure message should name.

    Returns:
        The labels of the matching documents, in the order supplied.
    """
    return [label for label, text in documents if literal in text]


@pytest.fixture(scope="module")
def tracked_documents() -> tuple[tuple[str, str], ...]:
    """Read every git-tracked Markdown file in the repository.

    Module-scoped because the sweep is run once per forbidden literal and
    re-reading a few hundred files per parameter buys nothing.

    Returns:
        ``(repo-relative POSIX path, file text)`` for each tracked
        ``*.md`` file. ``errors="replace"`` so one undecodable byte
        somewhere in the tree cannot disable the whole gate.
    """
    return tuple(
        (
            path.relative_to(REPO_ROOT).as_posix(),
            path.read_text(encoding="utf-8", errors="replace"),
        )
        for path in tracked_markdown_files(REPO_ROOT)
    )


def test_the_tracked_markdown_sweep_spans_the_whole_repository(
    tracked_documents: tuple[tuple[str, str], ...],
) -> None:
    """Discovery must reach both halves of the monorepo, not one subtree.

    ``tracked_markdown_files`` already raises on a totally empty sweep.
    The failure mode left over is a sweep that collapses to a single
    subtree — which is exactly what a bare ``git ls-files`` run from
    ``creek-tools/`` produces — and that would let the assertions below
    pass while never looking at half the documentation. No filename is
    pinned, so an ordinary rename cannot break this.

    Args:
        tracked_documents: The gathered ``(path, text)`` pairs.
    """
    labels = [label for label, _ in tracked_documents]

    assert labels, (
        f"the tracked-Markdown sweep under {REPO_ROOT} came back empty; "
        "every assertion in this module would pass over nothing."
    )
    assert any(label.startswith(_CREEK_TOOLS_PREFIX) for label in labels), (
        f"the sweep found no Markdown under {_CREEK_TOOLS_PREFIX}; "
        f"discovery has collapsed to a subtree. Found: {labels[:10]}"
    )
    assert any(not label.startswith(_CREEK_TOOLS_PREFIX) for label in labels), (
        "the sweep found Markdown only under "
        f"{_CREEK_TOOLS_PREFIX}; the repository-root documentation is "
        f"not being checked. Found: {labels[:10]}"
    )


@pytest.mark.parametrize("literal", PHANTOM_REDACTION_ARTIFACTS)
def test_the_phantom_artifact_detector_fires_on_a_synthetic_document(
    literal: str,
) -> None:
    """The matcher really does flag the literal — and only where it appears.

    The non-vacuity control for the repository sweep. Without it, a
    matcher that had stopped matching (or a sweep that had stopped
    sweeping) would report a clean repository forever.

    Args:
        literal: One forbidden artifact name.
    """
    named = f"# Redaction\n\nThe queue lands at `<source>/{literal}` here.\n"
    clean = "# Redaction\n\nThe scan writes nothing; the report is printed.\n"

    flagged = documents_naming(
        literal,
        [("synthetic-names-it.md", named), ("synthetic-clean.md", clean)],
    )

    assert flagged == ["synthetic-names-it.md"], (
        f"the detector for {literal!r} did not behave: it should flag the "
        "one synthetic document that names the artifact and leave the "
        f"clean one alone, but it returned {flagged!r}."
    )


@pytest.mark.parametrize("literal", PHANTOM_REDACTION_ARTIFACTS)
def test_no_tracked_markdown_names_a_phantom_redaction_artifact(
    literal: str,
    tracked_documents: tuple[tuple[str, str], ...],
) -> None:
    """No documentation may promise a redaction path the code never writes.

    Args:
        literal: One forbidden artifact name.
        tracked_documents: The gathered ``(path, text)`` pairs.
    """
    offenders = documents_naming(literal, tracked_documents)

    assert offenders == [], (
        f"{len(offenders)} tracked Markdown file(s) still document "
        f"{literal!r} as a redaction artifact: {', '.join(offenders)}. "
        "Nothing under creek/ or creek_mcp/ has ever created that path: "
        "`creek redact --scan` writes no files at all, and `--report` "
        "renders the summary to the console. Correct the prose to "
        "describe what the code does — do not create the file to match "
        "the prose."
    )
