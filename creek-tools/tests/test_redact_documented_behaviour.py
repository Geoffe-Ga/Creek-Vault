"""Pins for what the three ``creek redact`` modes actually do.

Issue #1338. The redaction documentation describes behaviour the code does
not have, so before the prose can be rewritten the true behaviour has to be
nailed down — otherwise the rewrite is one more unverified claim and the
next reader has no way to tell which of the two is lying.

Four properties are pinned here:

* ``--scan`` writes **nothing**. Not a queue, not a report, not a directory.
  ``--report`` renders the markdown summary to the console.
* ``generate_markdown_summary`` never reproduces matched secret text. This
  is the ``creek/redact/__init__.py`` "sensitive matched text is never
  stored" invariant, observed at the rendering layer.
* ``generate_review_queue`` **does** quote the operator's own source lines,
  verbatim and deliberately. Stated here so the doc rewrite cannot
  over-correct into a new misconception that redaction output is
  excerpt-free everywhere.
* ``--review`` selects fragments by *findings*, not by any
  ``pending_review`` marker. The documented filter is inverted.

The documentation-side guard lives in ``tests/test_redaction_docs_drift.py``.
A new file rather than an append to ``tests/test_redact.py`` so that not one
line of that 3927-line suite is disturbed.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest
from typer.testing import CliRunner

from creek.cli import app
from creek.config import RedactionConfig
from creek.redact import RedactionScanner
from creek.redact import scanner as scanner_module

if TYPE_CHECKING:
    from creek.redact import ScanSummary

runner = CliRunner()

EXPECTED_TABLE_HEADER: Final[str] = "| Line | Type | Severity |"
"""The per-file table header this test requires, spelled independently.

Deliberately *not* imported from :data:`creek.redact.scanner`. The
production constant and ``docs/redaction.md`` must agree with each other,
and :func:`test_the_exported_header_constant_matches_the_documented_one`
below asserts they do. If this test simply imported the constant it would
assert only that the renderer is self-consistent — which stays true when
someone widens the header to reintroduce an ``Excerpt`` column, the exact
regression issue #1338 removed from the docs.
"""

EMAIL_LINE: Final[str] = "Contact sgsg@example.com for details."
"""One seeded source line, asserted verbatim in the review queue."""

SEEDED_SECRETS: Final[tuple[str, ...]] = (
    "AKIAIOSFODNN7EXAMPLE",
    "sgsg@example.com",
    "123-45-6789",
    "4111111111111111",
)
"""Four distinct literals, one per pattern family, none of which may be
reproduced by the markdown summary."""

SEEDED_DOCUMENT: Final[str] = (
    f"{EMAIL_LINE}\n"
    "AWS key: AKIAIOSFODNN7EXAMPLE\n"
    "SSN: 123-45-6789\n"
    "Card number: 4111111111111111\n"
)
"""The scanned file's content — every entry of :data:`SEEDED_SECRETS`."""

PENDING_REVIEW_FRAGMENT: Final[str] = (
    "---\nredaction:\n  status: pending_review\n---\n\nNothing sensitive.\n"
)
"""A fragment carrying the marker the docs claim ``--review`` filters on,
and carrying no findings whatsoever."""


@pytest.fixture(autouse=True)
def _isolate_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run each test in its own directory with no ambient Creek config.

    ``chdir`` keeps every relative path in this module — and anything a
    command might unexpectedly write — inside ``tmp_path`` rather than the
    source tree, which matters most for the ``--scan`` test, whose whole
    claim is that nothing is written. Clearing ``CREEK_CONFIG`` stops a
    developer's own exported config from being loaded in place of the
    defaults these expectations are written against.

    Args:
        tmp_path: Pytest-provided temporary directory.
        monkeypatch: Pytest monkeypatch fixture.
    """
    monkeypatch.delenv("CREEK_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def seeded_summary(tmp_path: Path) -> ScanSummary:
    """Scan one file containing every literal in :data:`SEEDED_SECRETS`.

    Args:
        tmp_path: Pytest-provided temporary directory.

    Returns:
        The summary of that scan.

    Raises:
        AssertionError: If the scan found nothing. Every "the secret is
            absent from the output" assertion downstream would then hold
            trivially, because there would be no findings to render.
    """
    source = tmp_path / "seeded"
    source.mkdir()
    (source / "leaky.md").write_text(SEEDED_DOCUMENT, encoding="utf-8")

    summary = RedactionScanner(config=RedactionConfig()).scan_batch(source)

    assert summary.matches, (
        "the scanner found nothing in a file seeded with an AWS key, an "
        "email address, an SSN and a card number; the absence assertions "
        "in this module would pass over an empty report."
    )
    return summary


def squashed(text: str) -> str:
    """Return *text* with every run of whitespace removed.

    Console assertions go through this. Rich wraps, centres and folds its
    output to the terminal width, so a path or heading can be split across
    lines at any column; removing whitespace restores the contiguity the
    substring checks need without weakening them into "some of the
    characters appear somewhere".

    Args:
        text: Captured console output.

    Returns:
        The same text with all whitespace stripped out.
    """
    return "".join(text.split())


def relative_tree(root: Path) -> set[str]:
    """Return every path under *root*, relative to *root*.

    Directories and dotfiles included: the artifact the documentation
    promises is a hidden *directory*, so a listing that skipped either
    would miss the very thing being looked for.

    Args:
        root: Directory to enumerate.

    Returns:
        Repo-relative POSIX strings for every descendant of *root*.
    """
    entries: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(root):
        base = Path(dirpath)
        for name in (*dirnames, *filenames):
            entries.add((base / name).relative_to(root).as_posix())
    return entries


# ---------------------------------------------------------------------------
# (a) generate_markdown_summary never reproduces matched text
# ---------------------------------------------------------------------------


def test_every_seeded_secret_appears_in_the_document_that_gets_scanned() -> None:
    """The seeds must be in the file, or "absent from the output" is free.

    The cheapest way for the invariant below to rot into a tautology is
    for a literal to stop being seeded. Pinned as its own test so that
    failure reads as "the fixture is wrong", not "the scanner leaked".
    """
    assert len(SEEDED_SECRETS) == 4, (
        f"expected four seeded literals, found {len(SEEDED_SECRETS)}; "
        "the parametrised absence checks cover only what is listed here."
    )
    for secret in SEEDED_SECRETS:
        assert secret in SEEDED_DOCUMENT, (
            f"{secret!r} is asserted absent from the rendered summary but "
            "was never written into the scanned document."
        )


def test_the_exported_header_constant_matches_the_documented_one() -> None:
    """``scanner.FINDINGS_TABLE_HEADER`` is the header the docs may quote.

    #1338 exported the header as a constant so the renderer and
    ``docs/redaction.md`` stop being two independent copies of one string —
    the same reasoning as :data:`creek.redact.scanner.SYMLINK_SKIP_LABEL`.
    A constant only prevents drift while something checks it against an
    independently-spelled expectation, which is what this asserts.
    """
    assert scanner_module.FINDINGS_TABLE_HEADER == EXPECTED_TABLE_HEADER, (
        "creek.redact.scanner.FINDINGS_TABLE_HEADER is "
        f"{scanner_module.FINDINGS_TABLE_HEADER!r}, but the documented "
        f"header is {EXPECTED_TABLE_HEADER!r}. Whichever moved, the sample "
        "table in docs/redaction.md now describes output nobody emits."
    )


def test_markdown_summary_renders_the_findings_table_header(
    seeded_summary: ScanSummary,
) -> None:
    """The per-file table is rendered, with its exact documented header.

    The positive half of the never-store invariant: the summary describes
    each finding by line, pattern name and severity. Any documentation of
    this output must quote this header and no other.

    Asserted as **whole-line equality**, not containment. A substring check
    passes against ``| Line | Type | Severity | Excerpt |`` — the mutant
    that reintroduces the very column issue #1338 removed from the docs —
    because the true header is a prefix of it. Only the seeded-secret
    assertions below caught that mutant; this one now catches it too, so
    the widening is refused at the header rather than only at the leak.

    Args:
        seeded_summary: Scan of the file holding the seeded literals.
    """
    scanner = RedactionScanner(config=RedactionConfig())

    markdown = scanner.generate_markdown_summary(seeded_summary)

    header_lines = [
        line for line in markdown.splitlines() if line.startswith("| Line ")
    ]
    assert header_lines, (
        f"the summary rendered no findings-table header at all.\n\n{markdown}"
    )
    assert set(header_lines) == {EXPECTED_TABLE_HEADER}, (
        "the per-file findings table header is not exactly "
        f"{EXPECTED_TABLE_HEADER!r}; got {sorted(set(header_lines))!r}. The "
        "docs quote it verbatim, and a widened header is how an Excerpt "
        f"column of secret text gets back in.\n\n{markdown}"
    )
    assert "### `" in markdown, (
        f"the summary rendered no per-file section at all.\n\n{markdown}"
    )


@pytest.mark.parametrize("secret", SEEDED_SECRETS)
def test_markdown_summary_never_renders_a_seeded_secret_literal(
    secret: str,
    seeded_summary: ScanSummary,
) -> None:
    """The markdown summary reproduces none of the text it matched on.

    ``creek/redact/__init__.py`` promises that sensitive matched text is
    never stored; a :class:`RedactionMatch` carries only a salted hash.
    This is that promise observed where it is easiest to break by
    accident — the document a human is handed, pastes into a ticket, or
    (via CrawDad) posts into a Discord channel.

    Asserted per literal rather than by looking for a column named
    "Excerpt": renaming a column would defeat that, and the property is
    the absence of the *text*, not of a heading.

    Args:
        secret: One seeded literal.
        seeded_summary: Scan of the file holding the seeded literals.
    """
    scanner = RedactionScanner(config=RedactionConfig())

    markdown = scanner.generate_markdown_summary(seeded_summary)

    assert secret not in markdown, (
        f"generate_markdown_summary reproduced the matched literal "
        f"{secret!r} in its output. The summary may name the file, the "
        "line, the pattern and the severity — never the matched "
        f"text.\n\n{markdown}"
    )


# ---------------------------------------------------------------------------
# (a2) generate_review_queue deliberately DOES quote the source
# ---------------------------------------------------------------------------


def test_review_queue_does_quote_the_operators_own_source_line(
    seeded_summary: ScanSummary,
) -> None:
    """The review queue quotes source lines verbatim, and that is correct.

    The complement to the test above, and the reason the never-store
    invariant must not be documented as "redaction output never shows
    sensitive text". A reviewer cannot classify a finding as a false
    positive without seeing it, so
    :meth:`RedactionScanner.generate_review_queue` calls
    :meth:`RedactionScanner.extract_context`, which **re-reads the
    operator's own file** at review time. Nothing is retained: the excerpt
    is never carried on a :class:`RedactionMatch`, never written to the
    audit log, and never survives the call.

    Anyone tempted to strip the excerpt to satisfy the invariant is
    breaking review, not fixing a leak.

    Args:
        seeded_summary: Scan of the file holding the seeded literals.
    """
    scanner = RedactionScanner(config=RedactionConfig())

    queue = scanner.generate_review_queue(seeded_summary)

    assert EMAIL_LINE in queue, (
        "the review queue no longer quotes the source line "
        f"{EMAIL_LINE!r}. Without the surrounding context a reviewer "
        "cannot tell a real secret from a false positive."
    )
    assert "```" in queue, (
        "the quoted context is not inside a fenced block, so the queue "
        "no longer renders as reviewable markdown."
    )


# ---------------------------------------------------------------------------
# (b) --scan writes nothing
# ---------------------------------------------------------------------------


def test_scan_with_report_creates_no_artifact_under_the_source_tree(
    tmp_path: Path,
) -> None:
    """``--scan --report`` leaves the source tree byte-for-byte unchanged.

    The docs send operators to ``<source>/.creek-redactions/queue.json``
    and to a sibling ``report.md``; ``--scan`` has never written either,
    and ``--report`` prints its markdown to the console instead.

    The whole tree is compared as a set rather than probing for the two
    documented names, because a name-only check ("the queue directory does
    not exist") would be satisfied by any future code that writes the same
    artifact under a different name. ``--vault`` is deliberately not
    passed, so the audit log is not implicated in the result either way.

    Args:
        tmp_path: Pytest-provided temporary directory (also the cwd).
    """
    source = tmp_path / "source"
    source.mkdir()
    (source / "leaky.md").write_text(SEEDED_DOCUMENT, encoding="utf-8")
    before = relative_tree(source)

    result = runner.invoke(
        app,
        ["redact", "--scan", "--source", "source", "--report"],
    )

    assert result.exit_code == 0, result.output
    rendered = squashed(result.output)
    assert "Totalfindings" in rendered, (
        f"the scan printed no statistics block.\n\n{result.output}"
    )
    assert "FindingsbyFile" in rendered, (
        "--report did not render the per-file markdown report, so this "
        f"test proves nothing about what --report writes.\n\n{result.output}"
    )
    after = relative_tree(source)
    assert after == before, (
        "`creek redact --scan --report` created "
        f"{sorted(after - before)} under the source tree (and removed "
        f"{sorted(before - after)}). The scan is read-only: it writes no "
        "queue, no report file, and no directory."
    )


# ---------------------------------------------------------------------------
# (c) --review selects on findings, not on a pending_review marker
# ---------------------------------------------------------------------------


def test_review_lists_a_fragment_with_findings_not_one_marked_pending_review() -> None:
    """``--review`` is driven by findings; the marker plays no part at all.

    The documented behaviour is exactly inverted. ``--review`` re-scans the
    vault and renders the queue for whatever the scanner finds:

    * ``a.md`` carries an email address and **no** marker — it is listed.
    * ``b.md`` carries ``redaction.status: pending_review`` in its
      frontmatter and **no** sensitive content — it is not listed.

    A ``pending_review`` marker neither adds a fragment to the queue nor
    removes one from it. Nothing in ``creek/`` or ``creek_mcp/`` reads that
    field; the only writer is ``creek/ingest/images.py``. Documenting the
    marker as the filter sends an operator hunting for a switch that is not
    wired to anything.

    The vault is named relatively so the rendered paths stay short and
    Rich has no reason to fold them; :func:`squashed` covers the residual.
    """
    vault = Path("vault")
    vault.mkdir()
    (vault / "a.md").write_text(f"{EMAIL_LINE}\n", encoding="utf-8")
    (vault / "b.md").write_text(PENDING_REVIEW_FRAGMENT, encoding="utf-8")

    result = runner.invoke(app, ["redact", "--review", "--vault", "vault"])

    assert result.exit_code == 0, result.output
    rendered = squashed(result.output)
    assert "RedactionReviewQueue" in rendered, (
        f"the review queue was not rendered at all.\n\n{result.output}"
    )
    assert "Finding1" in rendered, (
        f"the queue rendered no findings at all.\n\n{result.output}"
    )
    assert "a.md" in rendered, (
        "--review omitted a.md, which has a finding and no "
        "`pending_review` marker. Findings are what put a fragment in the "
        f"queue; the marker is not consulted.\n\n{result.output}"
    )
    assert "b.md" not in rendered, (
        "--review listed b.md, which carries the `pending_review` marker "
        "and no findings. The marker does not pull a clean fragment into "
        f"the queue — nothing in creek/ reads it.\n\n{result.output}"
    )
