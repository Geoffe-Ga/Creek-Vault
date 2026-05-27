"""Regression tests for INC-019 documentation drift.

The user-facing docs in ``creek-tools/docs/`` and any user-facing
strings in ``creek-tools/creek/`` must use the canonical taxonomy
verbatim. These tests grep the relevant files and assert the drifted
phase / mode / frequency strings do not reappear.

Drift values originated in the INC-019 spec; the long-form
``plans/git-issues/`` directory was retired in #243, so use
``git log --grep='INC-019'`` to locate the originating commits. The
canonical names are pinned in
``tests/fixtures/canonical_taxonomy.yaml``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

DOCS_DIR = Path(__file__).resolve().parents[1] / "docs"
COMPOST_PY = Path(__file__).resolve().parents[1] / "creek" / "generate" / "compost.py"

# Drift words that must not appear in user-facing copy. Word boundaries
# protect substrings like ``analytical`` (a VoiceRegister, unrelated to
# the drifted ``analytic`` mode) and ``compost.py`` filenames. Both
# title-case (prose) and lowercase (YAML frontmatter / skill-tree
# directory) forms are listed for the phase-name drift, since either
# could leak back into the docs.
DRIFT_PATTERNS = [
    r"\bOrigins\b",
    r"\borigins\b",
    r"\bCresting\b",
    r"\bcresting\b",
    r"\bReceding\b",
    r"\breceding\b",
    r"\bComposting\b",
    r"\bcomposting\b",
    # ``amplitude`` and ``pitch`` are common English words; anchor to
    # the contexts where they appeared as drift (frontmatter values
    # and skill-tree directory paths) so prose like "the pitch of the
    # voice" doesn't trip the gate as the docs grow.
    r"frequency:\s*(?:amplitude|pitch)\b",
    r"frequencies/(?:amplitude|pitch)\b",
    # Phrase-level drift that matches the INC's own grep recipes.
    r"solo\s*\|\s*dialogue\s*\|\s*reflective\s*\|\s*analytic",
    r"\{\s*solo\s*,\s*dialogue\s*,\s*reflective\s*,\s*analytic\s*\}",
]


@pytest.mark.parametrize("pattern", DRIFT_PATTERNS)
def test_classification_doc_has_no_drift(pattern: str) -> None:
    """``docs/classification.md`` uses the canonical taxonomy verbatim."""
    text = (DOCS_DIR / "classification.md").read_text(encoding="utf-8")
    assert not re.search(pattern, text), (
        f"INC-019 drift pattern {pattern!r} reappeared in classification.md"
    )


@pytest.mark.parametrize("pattern", DRIFT_PATTERNS)
def test_generation_doc_has_no_drift(pattern: str) -> None:
    """``docs/generation.md`` uses the canonical taxonomy verbatim."""
    text = (DOCS_DIR / "generation.md").read_text(encoding="utf-8")
    assert not re.search(pattern, text), (
        f"INC-019 drift pattern {pattern!r} reappeared in generation.md"
    )


def test_compost_module_has_no_drifted_phase_header() -> None:
    """``creek/generate/compost.py`` does not emit a ``Composting`` header."""
    text = COMPOST_PY.read_text(encoding="utf-8")
    # User-facing strings — match the INC author's grep recipe but
    # restricted to inside ``"..."`` literals so the module name and
    # docstrings are not falsely flagged.
    string_literals = re.findall(r'"([^"]*)"', text)
    offenders = [literal for literal in string_literals if "Composting" in literal]
    assert offenders == [], (
        "compost.py emits a 'Composting' header that mirrors the drift phase "
        f"name; offenders: {offenders!r}"
    )
