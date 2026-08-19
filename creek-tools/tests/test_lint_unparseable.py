"""``creek lint`` must surface the files every scan silently skips (#926).

A corrupt fragment is invisible today: guarded loads in the reader, purge,
hygiene and reflect each turn it into a ``DEBUG``-level skip, and ``DEBUG``
is off at the default level. It is in no scan and no report, and the tool
surface deliberately cannot say so — ``entry_ref`` answers it with the
ordinary "not found" refusal, because a distinct reason would be an
existence oracle (#847). Lint is the only place left.

The sharpest test here is not "does it find the file" but **"does it find
the file without quoting it"** — the frontmatter of a file that would not
parse has, by definition, no known ``privacy_tier``.

#926 asserts that ``yaml.MarkedYAMLError.__str__`` embeds the offending
source snippet. It does not, on this path: PyYAML renders position only,
because the snippet needs a buffer ``frontmatter.load`` has released. A
canary string planted in the malformed frontmatter is therefore **never**
reproduced, and a test asserting its absence would pass against a check that
rendered the full message — vacuous, and dressed as a security test.

So this pins the contract that can actually be violated: the finding carries
the exception **class name** and none of the message's own text. That fires
the moment anyone renders ``str(exc)``, which is the change the rule exists
to stop.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import pytest

from creek.lint import ALL_CHECKS
from creek.lint.checks import unparseable as unparseable_check
from creek.vault.reader import CORPUS_SUBDIRS

if TYPE_CHECKING:
    from pathlib import Path

# Distinctive text from PyYAML's own message for this shape. Asserting its
# ABSENCE is what catches a check that renders str(exc); a canary planted in
# the frontmatter would not, because the message never reproduces content.
PARSER_MESSAGE_FRAGMENT: Final[str] = "did not find expected"

MALFORMED: Final[str] = """---
title: [unclosed
note: a private detail
---

Body text.
"""

GOOD: Final[str] = """---
id: frag-ok
title: A readable note
privacy_tier: open
---

Body text.
"""


def _vault(tmp_path: Path) -> Path:
    """Build a vault with every corpus subtree present but empty.

    Args:
        tmp_path: pytest temporary directory.

    Returns:
        The vault root.
    """
    vault = tmp_path / "vault"
    for subdir in CORPUS_SUBDIRS:
        (vault / subdir).mkdir(parents=True)
    return vault


def test_a_clean_vault_reports_nothing(tmp_path: Path) -> None:
    """A vault whose corpus all parses produces no findings.

    The negative control. Without it a check that reported every file it saw
    would still pass every positive assertion below.
    """
    vault = _vault(tmp_path)
    (vault / "01-Fragments" / "fine.md").write_text(GOOD, encoding="utf-8")

    result = unparseable_check.run(vault)

    assert result.findings == []
    assert "No unreadable" in result.summary


def test_an_unparseable_fragment_is_reported_with_its_path(tmp_path: Path) -> None:
    """The corrupt file is named, and the readable one is not.

    Both halves matter: naming the corrupt file is the feature, and *not*
    naming the healthy one is what keeps the report actionable rather than a
    list of the whole vault.
    """
    vault = _vault(tmp_path)
    (vault / "01-Fragments" / "broken.md").write_text(MALFORMED, encoding="utf-8")
    (vault / "01-Fragments" / "fine.md").write_text(GOOD, encoding="utf-8")

    result = unparseable_check.run(vault)

    assert len(result.findings) == 1, result.findings
    assert "broken.md" in result.findings[0]
    assert "fine.md" not in result.findings[0]


def test_the_report_carries_a_class_name_not_the_parser_message(
    tmp_path: Path,
) -> None:
    """The finding names the exception class and quotes none of its message.

    An exception message is not a stable contract: PyYAML renders position
    today, may render a snippet tomorrow, and a different loader may already.
    Pinning "class name, nothing else" means that change cannot ship silently.

    Mutation check: swapping ``type(exc).__name__`` for ``exc`` in the check
    turns this red while every other test here stays green. Verified, not
    assumed.
    """
    vault = _vault(tmp_path)
    (vault / "01-Fragments" / "broken.md").write_text(MALFORMED, encoding="utf-8")

    result = unparseable_check.run(vault)
    rendered = result.summary + "\n" + "\n".join(result.findings)

    assert "ParserError" in rendered, rendered
    assert PARSER_MESSAGE_FRAGMENT not in rendered, "lint rendered str(exc)"
    assert "line 3" not in rendered, "lint leaked a position into the report"
    assert "a private detail" not in rendered, "lint leaked frontmatter content"


@pytest.mark.parametrize("subdir", CORPUS_SUBDIRS)
def test_every_corpus_subtree_is_scanned(tmp_path: Path, subdir: str) -> None:
    """A corrupt note is found wherever the reader would have walked.

    The issue named ``01-Fragments`` only, but the reader's corpus is three
    subtrees and a corrupt note under any of them is equally invisible.
    Parametrised over the shared constant so adding a fourth subtree to
    :data:`~creek.vault.reader.CORPUS_SUBDIRS` cannot silently leave this
    check behind.

    Args:
        tmp_path: pytest temporary directory.
        subdir: One corpus subtree.
    """
    vault = _vault(tmp_path)
    (vault / subdir / "broken.md").write_text(MALFORMED, encoding="utf-8")

    result = unparseable_check.run(vault)

    assert len(result.findings) == 1, result.findings
    assert subdir in result.findings[0]


def test_an_unreadable_file_does_not_crash_the_check(tmp_path: Path) -> None:
    """The check survives the failure mode it exists to report.

    A check that raised on a corrupt file would take down ``creek lint``
    itself, which is the #847 regression in a new costume.
    """
    vault = _vault(tmp_path)
    target = vault / "01-Fragments" / "unreadable.md"
    target.write_bytes(b"---\n\xff\xfe not utf-8 \xff\n---\nbody\n")

    result = unparseable_check.run(vault)

    assert len(result.findings) == 1, result.findings


def test_a_missing_corpus_subtree_is_not_an_error(tmp_path: Path) -> None:
    """A vault without ``11-Other-Authors`` scans the subtrees it does have.

    Not every vault has borrowed fragments, and a check that required the
    directory would fail on a perfectly healthy vault.
    """
    vault = tmp_path / "vault"
    (vault / "01-Fragments").mkdir(parents=True)
    (vault / "01-Fragments" / "broken.md").write_text(MALFORMED, encoding="utf-8")

    result = unparseable_check.run(vault)

    assert len(result.findings) == 1, result.findings


def test_the_check_is_registered() -> None:
    """``unparseable`` is in the registry, so ``creek lint`` actually runs it.

    A check nobody dispatches reports nothing, and the defect this closes is
    precisely "nothing tells the operator".
    """
    assert "unparseable" in ALL_CHECKS
